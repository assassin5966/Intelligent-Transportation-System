"""GPU 批量推理引擎: 每卡 1 个模型实例 + 批内多路检测, 每路独立官方 tracker.

解决的现场问题:
  1. CUDA context 频繁切换: 原先 32 路各自 new 一个 YOLO = 32 个 context 全挤
     0 号卡, 单帧推理被拖到 1.2s. 现在每卡 1 个实例, 且模型只被该引擎自己的
     单线程 executor 触碰 -> 单卡内无跨线程 context 切换, 8 卡各一实例真并行.
  2. 异步实例化资源管理: 模型在服务启动阶段预加载 (ModelPool.initialize),
     不在推理 worker 内懒加载, 启动即完成 CUDA 初始化并预热; 释放由 stop() 负责.
  3. GPU 负载不均衡: 设备按"最少负载"分配到各卡 (ModelPool.acquire).
  4. 4K 流推理前采样: 帧在解码侧/读帧线程已降采样, 引擎只做 letterbox;
     检测坐标在出口一次性还原到流原始分辨率, 下游 (计数/事件/前端) 零感知.

批量多路的关键实现选择:
  ultralytics 的 `Model.track()` 把各路的 tracker 挂在 `predictor.trackers[i]`,
  批内索引 i 必须与"路"一一对应且稳定. 但各路是独立异步管道, 同一组批窗口内
  不一定每路都有新帧 -> 要么用"陈旧帧补齐"(浪费算力, 实测约 4 倍), 要么每次
  重建 tracker(跟踪 ID 反复重置). 两者都不可接受.
  因此这里把"检测"与"跟踪"在同一引擎内**显式两步执行**, 仍复用 ultralytics 官方
  跟踪器对象 (BOTSORT/BYTETracker), 只是改为每路一个实例、由本引擎直接调用其
  `update()` —— 完全等价于 `Model.track()` 内部对 `predictor.trackers[i].update()`
  的调用, 但批内组成可任意变化, 无需补齐/重建:
      results = model.predict(frames, ...)        # 无状态, 任意组批
      tracks  = tracker[device_id].update(det, frame)   # 每路独立、状态稳定
  另: 依据官方 `on_predict_postprocess_end` 语义, 检测为空的帧不调用 update
  (保持与 `Model.track()` 完全一致的行为), 但会推进本引擎的批次统计.
"""
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from ..common.business_rules import get_rule
from ..common.config import settings
from ..common.logger import logger

from .traj_state import RawTrack, filter_detection_class


class EngineBusy(Exception):
    """引擎待处理队列饱和 (等待批次已达上限), 调用方应跳过本帧取下一最新帧.

    语义与旧 CPU 路径的 inference_scheduler.InferBusy 一致: 丢一帧与排队多帧
    在计数语义上等价, 但丢弃可避免延迟累积与队列雪崩.
    """


class TrackerApiError(Exception):
    """ultralytics 跟踪器 API 与预期不符 (版本漂移), 由 ModelPool 降级处理."""


@dataclass
class _Req:
    device_id: str
    frame: np.ndarray
    scale: tuple  # (sx, sy): frame 尺寸 -> 流原始分辨率
    enqueued_at: float = 0.0
    future: Optional[asyncio.Future] = None


# 停止信号 (与"本批次无新请求"的超时区分开)
_STOP = object()


@dataclass
class InferOutcome:
    """一次批量推断的产出 (供 pipeline 打点与后续轨迹状态维护)."""

    raw_tracks: list = field(default_factory=list)
    batch_size: int = 0
    wait_ms: float = 0.0     # 本次请求在队列中等待组批的时长
    infer_ms: float = 0.0    # 本批次整体推理+跟踪耗时


class GpuInferEngine:
    """单张 GPU 上的批量推理引擎 (一个模型实例 + 若干设备槽位)."""

    def __init__(
        self,
        gpu_id: int,
        *,
        model_path: str,
        slots: int,
        batch_timeout_s: float,
        queue_max_batches: int,
        tracker_cfg: str,
        imgsz: int,
        half: bool,
    ) -> None:
        self.gpu_id = gpu_id
        self.device = f"cuda:{gpu_id}"
        self._model_path = model_path
        self._slots_total = slots
        self._batch_timeout = batch_timeout_s
        self._max_pending = max(1, slots * queue_max_batches)
        self._tracker_cfg = tracker_cfg
        self._imgsz = imgsz
        self._half = half

        self._model = None
        self._tracker_args = None
        self._trackers: dict = {}          # device_id -> 官方跟踪器实例 (每路独立)
        self._devices: dict = {}           # device_id -> slot
        self._free_slots: list = []        # 可用槽位 (小号优先)

        self._queue: Optional[asyncio.Queue] = None
        self._collector_task: Optional[asyncio.Task] = None
        self._executor: Optional[ThreadPoolExecutor] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        self._stopping = False
        # 运行统计 (供 /health 与现场定位)
        self.batches = 0
        self.drops_busy = 0
        self.last_infer_ms = 0.0
        self.total_infer_ms = 0.0

    # ---- 生命周期 ----

    def start_blocking(self) -> None:
        """同步加载模型 + 预热 + 自检 (由 ModelPool 放到线程中调用, 不阻塞事件循环)."""
        from ultralytics import YOLO

        self._tracker_args = _load_tracker_args(self._tracker_cfg)
        t0 = time.monotonic()
        model = YOLO(self._model_path)
        load_s = time.monotonic() - t0
        # 显式搬到目标卡: 避免 ultralytics 在首次 predict 时才隐式搬卡
        # (首次 predict 发生在线程内, 显存与 context 归属更清晰)
        t0 = time.monotonic()
        model.to(self.device)
        move_s = time.monotonic() - t0
        self._model = model
        logger.info(
            f"[GPU {self.gpu_id}] 模型加载完成: 权重 {load_s:.1f}s / 搬卡 {move_s:.1f}s "
            f"(device={self.device}, imgsz={self._imgsz}, half={self._half})"
        )
        self._warmup()
        self._selftest()
        self._free_slots = list(range(self._slots_total))
        logger.info(
            f"[GPU {self.gpu_id}] 引擎就绪: 槽位 {self._slots_total}, 显存 {self.vram_mb()}MB, "
            f"预热耗时 {self.last_infer_ms:.0f}ms"
        )

    def attach_loop(self) -> None:
        """绑定当前事件循环并启动组批 collector (必须在事件循环线程内调用)."""
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"gpu{self.gpu_id}-infer"
        )
        self._collector_task = self._loop.create_task(self._collector())

    async def stop(self) -> None:
        self._stopping = True
        if self._queue is not None:
            try:
                self._queue.put_nowait(_STOP)  # 唤醒 collector 收尾退出
            except Exception:  # noqa: BLE001
                pass
        if self._collector_task is not None:
            task, self._collector_task = self._collector_task, None
            # 先等 collector 收到 _STOP 自行收尾 (会失败掉未派发的请求);
            # 超时再强杀, 避免个别回调卡住整个服务关闭流程
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
            except asyncio.TimeoutError:
                task.cancel()
            except Exception:  # noqa: BLE001
                pass
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None
        # 释放模型与显存 (修复"删除设备后显存不回收")
        self._trackers.clear()
        if self._model is not None:
            try:
                self._model.to("cpu")
            except Exception:  # noqa: BLE001
                pass
            self._model = None
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass
        logger.info(f"[GPU {self.gpu_id}] 引擎已停止 (累计批次 {self.batches})")

    # ---- 槽位分配 ----

    @property
    def device_count(self) -> int:
        return len(self._devices)

    @property
    def _dispatch_size(self) -> int:
        """提前派发阈值: 本卡已分配的设备数.

        同形状分组最多只可能收到"本卡设备数"个请求 (一路一帧), 按槽位上限判定会
        在 32 路/8 卡 (每卡 4 路) 时永远凑不满, 退化成纯超时批 (均批≈1~4).
        _devices 只在事件循环线程内增删, 与 collector 同线程, 无需加锁.
        """
        return max(1, len(self._devices))

    @property
    def free_slots(self) -> int:
        return len(self._free_slots)

    def has_capacity(self) -> bool:
        return bool(self._free_slots)

    def assign(self, device_id: str) -> int:
        if device_id in self._devices:
            return self._devices[device_id]
        slot = self._free_slots.pop(0)
        self._devices[device_id] = slot
        self._trackers[device_id] = _build_tracker(self._tracker_args)
        logger.info(
            f"[GPU {self.gpu_id}] 槽位分配: {device_id} -> slot{slot} "
            f"(本卡设备 {len(self._devices)}/{self._slots_total})"
        )
        return slot

    def unassign(self, device_id: str) -> None:
        slot = self._devices.pop(device_id, None)
        if slot is None:
            return
        self._trackers.pop(device_id, None)  # 跟踪状态随设备消失, 不留给下一路
        self._free_slots.append(slot)
        self._free_slots.sort()
        logger.info(f"[GPU {self.gpu_id}] 槽位释放: {device_id} slot{slot}")

    # ---- 推理提交 ----

    async def submit(self, device_id: str, frame: np.ndarray, scale: tuple) -> InferOutcome:
        """提交一帧; 队列饱和时抛 EngineBusy (调用方丢本帧)."""
        if self._queue is None or self._loop is None:
            raise EngineBusy("engine not started")
        if self._stopping:
            raise EngineBusy("engine stopping")
        if self._queue.qsize() >= self._max_pending:
            self.drops_busy += 1
            raise EngineBusy(
                f"gpu{self.gpu_id} queue full ({self._queue.qsize()}/{self._max_pending})"
            )
        fut = self._loop.create_future()
        self._queue.put_nowait(
            _Req(device_id, frame, scale, enqueued_at=time.monotonic(), future=fut)
        )
        return await fut

    async def _collector(self) -> None:
        """组批循环: 凑够本卡设备数或等满 batch_timeout 即执行一批.

        按帧形状分组: 同一次 predict 的多图必须 letterbox 后形状一致, 否则
        ultralytics 组批时 stack 失败. 单路流分辨率固定, 因此同形状分组天然
        只聚合同分辨率的设备; 不同分辨率各自成组, 不 padding 到统一画布
        (把 16:9 塞进正方形要多算约 76% 像素, 与"高效采样"目标相悖).
        """
        assert self._queue is not None and self._loop is not None
        pending: dict = {}    # frame.shape -> [请求]
        deadline: dict = {}   # frame.shape -> 该组开始等待时刻 + 组批窗口
        while True:
            timeout = None
            if deadline:
                timeout = max(0.0, min(deadline.values()) - self._loop.time())
            item = None
            timed_out = False
            try:
                if timeout is None:
                    item = await self._queue.get()
                else:
                    item = await asyncio.wait_for(self._queue.get(), timeout)
            except asyncio.TimeoutError:
                timed_out = True

            if item is _STOP:
                for group in pending.values():
                    self._fail_batch(group, EngineBusy("engine stopping"))
                return

            if item is not None:
                shape = item.frame.shape
                group = pending.setdefault(shape, [])
                group.append(item)
                if shape not in deadline:
                    deadline[shape] = self._loop.time() + self._batch_timeout
                # 提前派发阈值 = 本卡已分配设备数 (非槽位上限)
                if len(group) >= self._dispatch_size:
                    deadline.pop(shape, None)
                    self._dispatch(pending.pop(shape))
                    continue

            if timed_out:
                now = self._loop.time()
                for shape in [s for s, d in deadline.items() if d <= now]:
                    deadline.pop(shape, None)
                    self._dispatch(pending.pop(shape))

    def _dispatch(self, batch: list) -> None:
        """把一批提交到该引擎的单线程 executor, 并在完成回调里派发结果/异常."""
        if not batch:
            return

        def _on_done(fut, _batch=batch):
            # 回调在事件循环线程执行: 结果与异常都在此派发给各请求方
            if fut.cancelled():
                self._fail_batch(_batch, EngineBusy("batch cancelled"))
                return
            err = fut.exception()
            if err is not None:
                self._fail_batch(_batch, err)
                return
            self._finish_batch(_batch, fut.result())

        try:
            fut = asyncio.wrap_future(
                self._executor.submit(self._run_batch, batch), loop=self._loop
            )
        except Exception as e:  # noqa: BLE001  (executor 已关闭等)
            self._fail_batch(batch, e)
            return
        fut.add_done_callback(_on_done)

    def _finish_batch(self, batch: list, results) -> None:
        for item, res in zip(batch, results or []):
            if item.future is not None and not item.future.done():
                item.future.set_result(res)

    def _fail_batch(self, batch: list, err: BaseException) -> None:
        for item in batch:
            if item.future is not None and not item.future.done():
                item.future.set_exception(err)

    # ---- 单线程内执行 (模型/predictor/跟踪器只在本线程被触碰) ----

    def _run_batch(self, batch: list) -> list:
        frames = [it.frame for it in batch]
        conf = float(get_rule("tracking", "yolo_conf", default=settings.yolo_conf))
        iou = float(get_rule("tracking", "yolo_iou", default=settings.yolo_iou))
        imgsz = int(get_rule("inference", "imgsz", default=self._imgsz))

        t0 = time.monotonic()
        results = self._model.predict(
            frames,
            imgsz=imgsz,
            half=self._half,
            device=self.device,
            conf=conf,
            iou=iou,
            verbose=False,
        )
        infer_ms = (time.monotonic() - t0) * 1000.0
        done_at = time.monotonic()
        # 结果必须与请求一一对应: 数量不符说明 ultralytics 行为与预期不符,
        # 一旦错位会把别路的检测喂给本路 tracker (静默污染计数), 故直接报错
        if len(results) != len(batch):
            raise RuntimeError(
                f"predict 返回 {len(results)} 个结果, 与批次 {len(batch)} 不符"
            )

        outcomes = []
        for req, res in zip(batch, results):
            outcomes.append(InferOutcome(
                raw_tracks=self._tracks_for(req, res),
                batch_size=len(batch),
                wait_ms=max(0.0, (done_at - req.enqueued_at) * 1000.0),
                infer_ms=infer_ms,
            ))

        self.batches += 1
        self.last_infer_ms = infer_ms
        self.total_infer_ms += infer_ms
        return outcomes

    def _tracks_for(self, req: _Req, res) -> list:
        """单路结果: 官方跟踪器 update -> 还原到流原始分辨率坐标的 RawTrack 列表."""
        boxes = res.boxes
        if boxes is None or len(boxes) == 0:
            # 与官方 on_predict_postprocess_end 一致: 空检测不调用 update
            return []
        tracker = self._trackers.get(req.device_id)
        if tracker is None:
            return []
        # 显式按 (xyxy, conf, cls) 拼 (N,6): 不依赖 boxes.data 是否含额外列,
        # 也正是 ultralytics 传给 tracker.update() 的格式
        det = np.concatenate(
            [
                boxes.xyxy.cpu().numpy(),
                boxes.conf.cpu().numpy()[:, None],
                boxes.cls.cpu().numpy()[:, None],
            ],
            axis=1,
        ).astype(np.float32)
        tracks = tracker.update(det, req.frame)
        if tracks is None or len(tracks) == 0:
            return []
        sx, sy = req.scale
        out = []
        for row in np.asarray(tracks):
            if len(row) < 7:
                continue
            class_name = filter_detection_class(int(row[6]))
            if class_name is None:
                continue
            out.append(RawTrack(
                track_id=int(row[4]),
                class_id=int(row[6]),
                class_name=class_name,
                bbox=[
                    float(row[0]) * sx, float(row[1]) * sy,
                    float(row[2]) * sx, float(row[3]) * sy,
                ],
                confidence=float(row[5]),
            ))
        return out

    # ---- 预热与自检 ----

    def _warmup(self) -> None:
        """首次 predict 建立 predictor 并分配 CUDA 显存, 避免首帧毛刺."""
        dummy = np.zeros((self._imgsz, self._imgsz, 3), np.uint8)
        t0 = time.monotonic()
        try:
            self._model.predict(
                [dummy], imgsz=self._imgsz, half=self._half,
                device=self.device, verbose=False,
            )
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"GPU {self.gpu_id} 预热失败: {e}") from e
        self.last_infer_ms = (time.monotonic() - t0) * 1000.0

    def _selftest(self) -> None:
        """自检官方跟踪器 API 表面 (版本漂移安全阀).

        验证: 跟踪器可从配置构造、update() 返回 [x1,y1,x2,y2,id,conf,cls,idx] 形状、
        列语义正确 (第 6 列置信度/第 7 列类别)、且跨帧保持同一目标 ID.
        列序一旦漂移会把置信度当类别喂给下游 (静默污染计数), 故必须显式校验.
        任一不符 -> TrackerApiError -> ModelPool 降级 legacy.
        """
        try:
            tracker = _build_tracker(self._tracker_args)
            dummy = np.zeros((self._imgsz, self._imgsz, 3), np.uint8)
            # 构造一个位于画面中部的稳定检测 (类 car=2, 置信度 0.9)
            det = np.array([[0.4, 0.4, 0.6, 0.6, 0.9, 2]], dtype=np.float32)
            first = tracker.update(det, dummy)
            if first is None or (len(first) and np.asarray(first).shape[1] < 7):
                raise TrackerApiError(f"update() 返回形状异常: {None if first is None else np.asarray(first).shape}")
            if len(first) == 0:
                raise TrackerApiError("update() 未产出轨迹 (首帧激活失败)")
            row = np.asarray(first)[0]
            # 列语义: 注入的检测为 cls=2 / conf=0.9, 校验第 6/7 列与之对应
            if int(row[6]) != 2:
                raise TrackerApiError(
                    f"update() 第 7 列不是类别 (期望 2, 实际 {row[6]}): 列序与预期不符"
                )
            if not 0.0 < float(row[5]) <= 1.0:
                raise TrackerApiError(
                    f"update() 第 6 列不是置信度 (实际 {row[5]}): 列序与预期不符"
                )
            tid1 = int(row[4])
            det2 = det.copy()
            det2[0, :4] += 0.01  # 轻微位移, 应关联为同一目标
            second = tracker.update(det2, dummy)
            if len(second) == 0 or int(np.asarray(second)[0][4]) != tid1:
                raise TrackerApiError("跨帧未保持同一 track_id")
        except TrackerApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise TrackerApiError(f"跟踪器自检异常: {e}") from e

    # ---- 可观测性 ----

    def vram_mb(self) -> float:
        try:
            import torch
            if not torch.cuda.is_available():
                return 0.0
            return torch.cuda.memory_allocated(self.gpu_id) / 1024 / 1024
        except Exception:  # noqa: BLE001
            return 0.0

    def snapshot(self) -> dict:
        return {
            "gpu_id": self.gpu_id,
            "devices": len(self._devices),
            "slots": self._slots_total,
            "model_loaded": self._model is not None,
            "vram_mb": round(self.vram_mb(), 1),
            "queue": self._queue.qsize() if self._queue is not None else 0,
            "batches": self.batches,
            "drops_busy": self.drops_busy,
            "last_infer_ms": round(self.last_infer_ms, 1),
        }


def _load_tracker_args(tracker_cfg: str):
    """读取跟踪器配置为 SimpleNamespace (与 ultralytics on_predict_start 同做法)."""
    from ultralytics.utils import IterableSimpleNamespace, YAML, check_yaml
    return IterableSimpleNamespace(**YAML.load(check_yaml(tracker_cfg)))


def _build_tracker(args):
    """按配置构造一路官方跟踪器实例 (botsort/bytetrack).

    frame_rate 与 ultralytics 在 numpy 列表输入下的取值一致 (dataset.fps 默认 30),
    保证与旧 `Model.track()` 路径的行为一致.
    """
    from ultralytics.trackers import BOTSORT, BYTETracker

    tracker_type = getattr(args, "tracker_type", "botsort")
    if tracker_type == "bytetrack":
        return BYTETracker(args=args, frame_rate=30)
    if tracker_type == "botsort":
        return BOTSORT(args=args, frame_rate=30)
    raise TrackerApiError(f"未知 tracker_type: {tracker_type}")
