"""CPU 推理调度器: 专用线程池 + 有界等待队列 (背压丢弃).

问题背景:
  各路管道此前用 asyncio.to_thread(默认线程池, 最多 32 worker) 并发推理,
  N 路同时推理互相争抢 CPU, 导致单路处理 fps 下降 -> 读帧线程覆盖丢帧 ->
  目标跨线动作整段丢失 (漏计).

方案:
  - 推理集中到专用线程池, worker 数 = infer_max_workers or CPU 核数
    (CPU 密集任务超过核数的并发只会增加线程切换开销);
  - 等待队列有界: 信号量容量 = workers + infer_max_queued, 池饱和时新请求
    立即抛 InferBusy, 调用方丢弃当前帧直接取下一最新帧 (计数语义上丢一帧
    与排队 10 帧等价, 但丢弃避免延迟累积与队列雪崩).
"""
import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from ..common.config import settings
from ..common.logger import logger

_executor: Optional[ThreadPoolExecutor] = None
_sem: Optional[asyncio.Semaphore] = None
# 饱和丢弃累计 (诊断用, 由 pipeline 并入丢帧统计)
queue_drop_count: int = 0


def _ensure_pool() -> None:
    """惰性初始化专用线程池 (首次推理时, 按 CPU 核数定容)."""
    global _executor, _sem
    if _executor is not None:
        return
    workers = settings.infer_max_workers or os.cpu_count() or 2
    _executor = ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="infer"
    )
    _sem = asyncio.Semaphore(workers + settings.infer_max_queued)
    logger.info(
        f"推理线程池就绪: workers={workers} max_queued={settings.infer_max_queued}"
    )


class InferBusy(Exception):
    """推理池饱和 (等待任务已达上限), 调用方应跳过本帧."""


async def run_inference(fn: Callable, /, *args) -> Any:
    """在专用推理线程池中执行 fn(*args); 池满时立即抛 InferBusy (不排队).

    注: 所有 DevicePipeline 协程同处一个事件循环, 信号量跨协程互斥,
    但 waiters 队列属于事件循环, 多协程 await 同一信号量是安全的.
    """
    global queue_drop_count
    _ensure_pool()
    assert _sem is not None
    if _sem.locked():
        queue_drop_count += 1
        raise InferBusy()
    async with _sem:
        return await asyncio.wrap_future(
            _executor.submit(fn, *args)
        )
