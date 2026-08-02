"""警力分配定时调度.

每 N 分钟 (与预测间隔同步) 运行一次分配算法, 推送 WebSocket.
"""
import asyncio

from ...common.config import settings
from ...common.logger import logger
from .allocator import optimize_allocation

_task: asyncio.Task | None = None


async def _run_once() -> None:
    """执行一轮警力分配."""
    try:
        plan = await optimize_allocation()
        if plan is not None:
            # 推送到 WebSocket
            try:
                from ..api.ws import broadcast_police

                await broadcast_police(plan)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"警力方案 WebSocket 推送失败: {e}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"警力分配调度失败: {e}")


async def _loop() -> None:
    """定时循环: 每 N 分钟执行一次."""
    interval = settings.prediction_interval_minutes * 60
    # 启动后等待一个间隔再首次执行 (让预测先产出缓存)
    await asyncio.sleep(interval)
    while True:
        await _run_once()
        await asyncio.sleep(interval)


async def start_scheduler() -> None:
    """启动警力分配调度器."""
    global _task
    if _task is not None:
        return
    _task = asyncio.create_task(_loop())
    logger.info(
        f"警力分配调度器已启动 (每 {settings.prediction_interval_minutes} 分钟一次)"
    )


async def stop_scheduler() -> None:
    """停止警力分配调度器."""
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
        logger.info("警力分配调度器已停止")
