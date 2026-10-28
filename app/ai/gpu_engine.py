"""批量推理引擎: 每设备卡/CPU 池 1 个模型实例 + 批内多路检测, 每路独立官方 tracker.

device 可为 "cuda:N" (GPU) 或 "cpu" (无 GPU 部署的 cpu_batch 模式), 检测/跟踪/组批
逻辑与设备类型无关. 以下以 GPU 场景说明动机:
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
  检测为空的帧也调用 update, 以推进 tracker 的失踪时长和轨迹清理.
  Ultralytics 版本间的空检测输入包装与普通检测保持一致.
"""
import asyncio
import itertools
import time
import threading
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

    语义与旧 CPU 路径的 inference_scheduler.InferBusy 一致；丢帧可避免
    延迟累积，但会影响轨迹连续性和计数准确率，需现场评估。
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
    generation: int = 0
    dispatched_at: float = 0.0
    cancelled: threading.Event = field(default_factory=threading.Event)
    expired: bool = False


# 停止信号 (与"本批次无新请求"的超时区分开)
_STOP = object()
_WAKE = object()


@dataclass
class InferOutcome:
    """一次批量推断的产出 (供 pipeline 打点与后续轨迹状态维护)."""

    raw_tracks: list = field(default_factory=list)
    batch_size: int = 0
    wait_ms: float = 0.0     # 本次请求在队列中等待组批的时长
    infer_ms: float = 0.0    # 模型预测耗时
    executor_wait_ms: float = 0.0
    track_ms: float = 0.0


class GpuInferEngine:
    """单个批量推理引擎 (一个模型实例 + 若干设备槽位).

    device 既可指向 GPU ("cuda:0"...) 也可为 "cpu" (无 GPU 部署的批量模式):
    检测/跟踪/组批逻辑与设备类型无关, 差异仅在 half (CPU 强制 FP32) 与显存统计.
    """

    def __init__(
        self,
        engine_id: str,
        *,
        device: str,
        model_path: str,
        slots: int,
        batch_timeout_s: float,
        queue_max_batches: int,
        tracker_cfg: str,
        imgsz: int,
        half: bool,
    ) -> None:
        self.engine_id = engine_id
        self.device = device
        # cuda:N 的卡号 (供显存统计); CPU 引擎为 None
        self._cuda_index: Optional[int] = (
            int(device.split(":", 1)[1]) if device.startswith("cuda:") else None
        )
        self._model_path = model_path
        self._slots_total = slots
        self._batch_timeout = batch_timeout_s
        self._max_pending = max(1, slots * queue_max_batches)
        # 在途批上限: executor 是单线程且队列无界, 不设上限时推理变慢会把请求
        # 一直排队 (现场曾出现组批等待 8~9s), 这里改为"批完成才继续派发"
        self._max_inflight = max(1, int(queue_max_batches))
        self._inflight = 0
        # 队列、组批和在途统一计数，同一设备最多一个未完成请求。
        self._outstanding: dict = {}
        self._pending_count = 0
        self._max_queue_age_s = max(0.001, float(getattr(settings, "infer_max_queue_age_s", 2.0)))
        self._wake_pending = False
        self._generations: dict = {}
        self._generation_seq = 0
        self.drops_expired = 0
        self.batch_sizes: dict = {}
        # 形状 -> {device_id: 最近出现时刻}: 该形状组的提前派发阈值.
        # 不同分辨率各自成组, 用"本卡设备数"当阈值永远凑不满 -> 只能超时派发单帧.
        self._shape_devices: dict = {}
        self._shape_ttl = 60.0
        self._tracker_cfg = tracker_cfg
        self._imgsz = imgsz
        # ultralytics CPU 推理不支持 FP16, 强制关闭
        self._half = bool(half) and self._cuda_index is not None

        self._model = None
        self._tracker_args = None
        self._trackers: dict = {}          # device_id -> 官方跟踪器实例 (每路独立)
        # tracker.update() 检测输入契约 (启动自检时探测并锁定):
        #   False -> 8.3.x: 裸 (N,6) 数组 [x1,y1,x2,y2,conf,cls]
        #   True  -> 8.4.x: Boxes 包装 (update 内部访问 .conf/.cls/.xywh)
        self._det_boxed: bool = False
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
            f"[{self.engine_id}] 模型加载完成: 权重 {load_s:.1f}s / 搬设备 {move_s:.1f}s "
            f"(device={self.device}, imgsz={self._imgsz}, half={self._half})"
        )
        self._warmup()
        self._selftest()
        self._free_slots = list(range(self._slots_total))
        logger.info(
            f"[{self.engine_id}] 引擎就绪: 槽位 {self._slots_total}, 显存 {self.vram_mb()}MB, "
            f"预热耗时 {self.last_infer_ms:.0f}ms"
        )

    def attach_loop(self) -> None:
        """绑定当前事件循环并启动组批 collector (必须在事件循环线程内调用)."""
        self._loop = asyncio.get_running_loop()
        self._stopping = False
        self._queue = asyncio.Queue()
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"{self.engine_id}-infer"
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
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            except Exception:  # noqa: BLE001
                pass
        if self._executor is not None:
            executor, self._executor = self._executor, None
            await asyncio.to_thread(executor.shutdown, wait=True)
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
        logger.info(f"[{self.engine_id}] 引擎已停止 (累计批次 {self.batches})")

    @property
    def cuda_index(self) -> Optional[int]:
        """cuda:N 的卡号; CPU 引擎为 None (供压测脚本读逐卡峰值显存)."""
        return self._cuda_index

    # ---- 槽位分配 ----

    @property
    def device_count(self) -> int:
        return len(self._devices)

    @property
    def _dispatch_size(self) -> int:
        """兼容保留: 本卡已分配设备数 (形状信息缺失时的兜底阈值)."""
        return max(1, len(self._devices))

    def _note_shape(self, shape: tuple, device_id: str) -> None:
        """登记"该形状由哪些设备产生", 作为该组提前派发的阈值依据.

        组批按 frame.shape 分组, 不同源分辨率不同形状; 只有同一形状的活跃设备数
        才是该组真正能凑到的上限, 用本卡总设备数当阈值会导致永远等超时.
        """
        now = time.monotonic()
        for old_shape, old_devices in list(self._shape_devices.items()):
            if old_shape != shape:
                old_devices.pop(device_id, None)
                if not old_devices:
                    self._shape_devices.pop(old_shape, None)
        devs = self._shape_devices.setdefault(shape, {})
        devs[device_id] = now
        for d in [d for d, ts in devs.items() if now - ts > self._shape_ttl]:
            devs.pop(d, None)
        while len(self._shape_devices) > 32:
            self._shape_devices.pop(next(iter(self._shape_devices)))

    def _shape_target(self, shape: tuple) -> int:
        devs = self._shape_devices.get(shape)
        if not devs:
            return self._dispatch_size
        return max(1, len(devs))

    @property
    def free_slots(self) -> int:
        return len(self._free_slots)

    def has_capacity(self) -> bool:
        return bool(self._free_slots)

    def assign(self, device_id: str) -> int:
        if device_id in self._devices:
            return self._devices[device_id]
        tracker = _build_tracker(self._tracker_args)
        slot = self._free_slots.pop(0)
        self._devices[device_id] = slot
        self._generation_seq += 1
        self._generations[device_id] = self._generation_seq
        self._trackers[device_id] = tracker
        logger.info(
            f"[{self.engine_id}] 槽位分配: {device_id} -> slot{slot} "
            f"(本引擎设备 {len(self._devices)}/{self._slots_total})"
        )
        return slot

    def _wake_collector(self) -> None:
        if self._queue is not None and not self._wake_pending and not self._stopping:
            self._wake_pending = True
            self._queue.put_nowait(_WAKE)

    def unassign(self, device_id: str) -> None:
        slot = self._devices.pop(device_id, None)
        if slot is None:
            return
        self._generations.pop(device_id, None)
        self._trackers.pop(device_id, None)
        for shape, devices in list(self._shape_devices.items()):
            devices.pop(device_id, None)
            if not devices:
                self._shape_devices.pop(shape, None)
        self._free_slots.append(slot)
        self._free_slots.sort()
        self._wake_collector()
        logger.info(f"[{self.engine_id}] 槽位释放: {device_id} slot{slot}")

    async def reset_device(self, device_id: str) -> None:
        """流重连后重置跟踪器。旧代请求不能再更新新跟踪器或交付结果。"""
        if device_id not in self._devices:
            raise EngineBusy("device not assigned")
        tracker = _build_tracker(self._tracker_args)
        self._generation_seq += 1
        self._generations[device_id] = self._generation_seq
        self._trackers[device_id] = tracker
        self._wake_collector()

    # ---- 推理提交 ----

    async def submit(self, device_id: str, frame: np.ndarray, scale: tuple) -> InferOutcome:
        """一个设备最多一个请求；上限覆盖入口、待组批和在途。"""
        if self._queue is None or self._loop is None or self._stopping:
            raise EngineBusy("engine not started or stopping")
        if device_id not in self._devices:
            raise EngineBusy("device not assigned")
        if device_id in self._outstanding or len(self._outstanding) >= self._max_pending:
            self.drops_busy += 1
            raise EngineBusy(f"{self.engine_id} request capacity full or device busy")
        fut = self._loop.create_future()
        req = _Req(device_id, frame, scale, enqueued_at=time.monotonic(),
                   future=fut, generation=self._generations[device_id])
        self._outstanding[device_id] = req
        self._queue.put_nowait(req)
        try:
            return await fut
        except asyncio.CancelledError:
            req.cancelled.set()
            self._wake_collector()
            raise

    def _invalid_reason(self, req: _Req, now: float) -> Optional[str]:
        if req.cancelled.is_set() or req.future.cancelled():
            return "request cancelled"
        if self._generations.get(req.device_id) != req.generation:
            return "device generation changed"
        if now - req.enqueued_at > self._max_queue_age_s:
            return "request expired"
        return None

    def _release_req(self, req: _Req, err: Optional[BaseException] = None) -> None:
        if self._outstanding.get(req.device_id) is req:
            self._outstanding.pop(req.device_id, None)
        if err is not None and not req.future.done():
            req.future.set_exception(err)

    async def _collector(self) -> None:
        """按 shape 组批，容量满时等待完成通知，过期/取消请求及时清理。"""
        assert self._queue is not None and self._loop is not None
        pending: dict = {}
        deadline: dict = {}
        try:
            while True:
                self._flush_ready(pending, deadline)
                # 在途已满时忽略已过期的组批 deadline，等待批完成或新事件，
                # 只保留最早请求的过期时刻；避免零超时自旋占死事件循环。
                times = [r.enqueued_at + self._max_queue_age_s
                         for group in pending.values() for r in group]
                if self._inflight < self._max_inflight:
                    times.extend(deadline.values())
                timeout = max(0.001, min(times) - time.monotonic()) if times else None
                try:
                    if timeout is None:
                        item = await self._queue.get()
                    else:
                        item = await asyncio.wait_for(self._queue.get(), timeout)
                except asyncio.TimeoutError:
                    continue
                if item is _STOP:
                    break
                if item is _WAKE:
                    self._wake_pending = False
                    continue
                reason = self._invalid_reason(item, time.monotonic())
                if reason:
                    if reason == "request expired":
                        self.drops_expired += 1
                    self._release_req(item, EngineBusy(reason))
                    continue
                shape = item.frame.shape
                pending.setdefault(shape, []).append(item)
                self._pending_count += 1
                deadline.setdefault(shape, item.enqueued_at + self._batch_timeout)
                self._note_shape(shape, item.device_id)
        finally:
            for group in pending.values():
                for req in group:
                    self._release_req(req, EngineBusy("engine stopping"))
            self._pending_count = 0
            while not self._queue.empty():
                item = self._queue.get_nowait()
                if isinstance(item, _Req):
                    self._release_req(item, EngineBusy("engine stopping"))

    def _flush_ready(self, pending: dict, deadline: dict) -> None:
        now = time.monotonic()
        for shape, group in list(pending.items()):
            kept = []
            for req in group:
                reason = self._invalid_reason(req, now)
                if reason:
                    self._pending_count -= 1
                    if reason == "request expired":
                        self.drops_expired += 1
                    self._release_req(req, EngineBusy(reason))
                else:
                    kept.append(req)
            if kept:
                pending[shape] = kept
                deadline[shape] = kept[0].enqueued_at + self._batch_timeout
            else:
                pending.pop(shape, None)
                deadline.pop(shape, None)
        # 最旧就绪组优先，避免多种 shape 竞争时固定插入顺序造成饥饿。
        for shape in sorted(pending, key=lambda key: pending[key][0].enqueued_at):
            if self._inflight >= self._max_inflight:
                return
            group = pending[shape]
            if len(group) >= self._shape_target(shape) or deadline[shape] <= now:
                deadline.pop(shape, None)
                pending.pop(shape)
                self._pending_count -= len(group)
                self._dispatch(group)

    def _dispatch(self, batch: list) -> None:
        if not batch:
            return
        self._inflight += 1
        dispatched = time.monotonic()
        for req in batch:
            req.dispatched_at = dispatched

        def _on_done(fut, _batch=batch):
            if fut.cancelled():
                self._fail_batch(_batch, EngineBusy("batch cancelled"))
            elif fut.exception() is not None:
                self._fail_batch(_batch, fut.exception())
            else:
                self._finish_batch(_batch, fut.result())
            self._wake_collector()

        try:
            fut = asyncio.wrap_future(
                self._executor.submit(self._run_batch, batch), loop=self._loop
            )
        except Exception as e:  # noqa: BLE001
            self._fail_batch(batch, e)
            self._wake_collector()
            return
        fut.add_done_callback(_on_done)

    def _finish_batch(self, batch: list, results) -> None:
        self._inflight = max(0, self._inflight - 1)
        if len(results) != len(batch):
            for item in batch:
                self._release_req(item, RuntimeError("batch result count mismatch"))
            return
        for item, res in zip(batch, results):
            reason = self._invalid_reason(item, item.enqueued_at)  # 不对已完成推理应用排队时限
            if item.expired:
                reason = "request expired"
                self.drops_expired += 1
            if self._stopping:
                reason = "engine stopping"
            if reason:
                self._release_req(item, EngineBusy(reason))
            else:
                if not item.future.done():
                    item.future.set_result(res)
                self._release_req(item)

    def _fail_batch(self, batch: list, err: BaseException) -> None:
        self._inflight = max(0, self._inflight - 1)
        for item in batch:
            self._release_req(item, err)

    # ---- 单线程内执行 (模型/predictor/跟踪器只在本线程被触碰) ----

    def _run_batch(self, batch: list) -> list:
        started_at = time.monotonic()
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
        # 结果必须与请求一一对应: 数量不符说明 ultralytics 行为与预期不符,
        # 一旦错位会把别路的检测喂给本路 tracker (静默污染计数), 故直接报错
        if len(results) != len(batch):
            raise RuntimeError(
                f"predict 返回 {len(results)} 个结果, 与批次 {len(batch)} 不符"
            )

        outcomes = []
        for req, res in zip(batch, results):
            track_start = time.monotonic()
            req.expired = started_at - req.enqueued_at > self._max_queue_age_s
            # 先捕获旧 tracker 引用再检查代际：重连可立即替换映射，不等待
            # 运行中的 update；即使随后发生重连，本线程也只会触碰旧对象，
            # 完成回调再次检查代际并丢弃旧结果。
            tracker = self._trackers.get(req.device_id)
            valid = (tracker is not None and not req.cancelled.is_set()
                     and self._generations.get(req.device_id) == req.generation
                     and not req.expired)
            tracks = self._tracks_for(req, res, tracker) if valid else []
            outcomes.append(InferOutcome(
                raw_tracks=tracks,
                batch_size=len(batch),
                wait_ms=max(0.0, (req.dispatched_at - req.enqueued_at) * 1000.0),
                executor_wait_ms=max(0.0, (started_at - req.dispatched_at) * 1000.0),
                infer_ms=infer_ms,
                track_ms=(time.monotonic() - track_start) * 1000.0,
            ))

        self.batches += 1
        self.batch_sizes[len(batch)] = self.batch_sizes.get(len(batch), 0) + 1
        self.last_infer_ms = infer_ms
        self.total_infer_ms += infer_ms
        return outcomes

    def _tracks_for(self, req: _Req, res, tracker) -> list:
        """单路结果: 官方跟踪器 update -> 还原到流原始分辨率坐标的 RawTrack 列表."""
        boxes = res.boxes
        empty = boxes is None or len(boxes) == 0
        # 空检测也必须 update：BYTETracker 的 frame_id、失踪和淘汰逻辑都
        # 由 update 推进；跳过会让 track_buffer 失效并保留陈旧轨迹。
        if empty:
            det = np.empty((0, 6), dtype=np.float32)
        else:
            # 显式按 (xyxy, conf, cls) 拼 (N,6)，不依赖 boxes.data 额外列。
            det = np.concatenate(
                [
                    boxes.xyxy.cpu().numpy(),
                    boxes.conf.cpu().numpy()[:, None],
                    boxes.cls.cpu().numpy()[:, None],
                ],
                axis=1,
            ).astype(np.float32)
        tracks = tracker.update(self._wrap_det(det, req.frame), req.frame)
        if empty or tracks is None or len(tracks) == 0:
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

    def _wrap_det(self, det: np.ndarray, frame: np.ndarray):
        """按自检锁定的契约包装检测输入 (8.3 裸数组 / 8.4 Boxes)."""
        if self._det_boxed:
            from ultralytics.engine.results import Boxes
            return Boxes(det, orig_shape=frame.shape[:2])
        return det

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
            raise RuntimeError(f"{self.engine_id} 预热失败: {e}") from e
        self.last_infer_ms = (time.monotonic() - t0) * 1000.0

    def _selftest(self) -> None:
        """自检官方跟踪器 API 表面 (版本漂移安全阀).

        验证: 跟踪器可从配置构造、update() 返回 [x1,y1,x2,y2,id,conf,cls,idx] 形状、
        列语义正确 (第 6 列置信度/第 7 列类别)、且跨帧保持同一目标 ID.
        同时探测 update() 的检测输入契约: 8.3.x 收裸 (N,6) 数组; 8.4.x 起改为
        Boxes 包装 (内部访问 .conf/.cls/.xywh), 探测结果锁定到 _det_boxed.
        列序一旦漂移会把置信度当类别喂给下游 (静默污染计数), 故必须显式校验.
        任一不符 -> TrackerApiError -> ModelPool 降级 legacy.
        """
        try:
            dummy = np.zeros((self._imgsz, self._imgsz, 3), np.uint8)
            # 构造一个位于画面中部的稳定检测 (类 car=2, 置信度 0.9)
            det = np.array([[0.4, 0.4, 0.6, 0.6, 0.9, 2]], dtype=np.float32)

            # 输入契约探测: 8.3.x 收裸 (N,6) 数组; 8.4.x 起要求 Boxes 包装
            # (内部访问 .conf/.cls/.xywh, 裸数组会 AttributeError).
            # 用临时 tracker 探测: 失败尝试会推进 frame_id, 污染正式自检的
            # 首帧激活 (8.4 activate() 仅在 frame_id==1 时立即确认轨迹).
            self._det_boxed = False
            probe = _build_tracker(self._tracker_args)
            try:
                probe.update(self._wrap_det(det, dummy), dummy)
            except (AttributeError, TypeError, IndexError, KeyError):
                self._det_boxed = True

            # 正式自检: 全新 tracker + 锁定契约 (首帧即激活, 与在线路径一致)
            tracker = _build_tracker(self._tracker_args)

            def _update(det_arr):
                return tracker.update(self._wrap_det(det_arr, dummy), dummy)

            first = _update(det)
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
            second = _update(det2)
            if len(second) == 0 or int(np.asarray(second)[0][4]) != tid1:
                raise TrackerApiError("跨帧未保持同一 track_id")
        except TrackerApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise TrackerApiError(f"跟踪器自检异常: {e}") from e

    # ---- 可观测性 ----

    def vram_mb(self) -> float:
        """显存占用 (MB); CPU 引擎恒为 0."""
        if self._cuda_index is None:
            return 0.0
        try:
            import torch
            if not torch.cuda.is_available():
                return 0.0
            return torch.cuda.memory_allocated(self._cuda_index) / 1024 / 1024
        except Exception:  # noqa: BLE001
            return 0.0

    def snapshot(self) -> dict:
        return {
            "engine_id": self.engine_id,
            "device": self.device,
            "devices": len(self._devices),
            "slots": self._slots_total,
            "model_loaded": self._model is not None,
            "vram_mb": round(self.vram_mb(), 1),
            "queue": self._queue.qsize() if self._queue is not None else 0,
            "pending": self._pending_count,
            "outstanding": len(self._outstanding),
            "max_queue_age_s": self._max_queue_age_s,
            "drops_expired": self.drops_expired,
            "batch_sizes": dict(self.batch_sizes),
            "inflight": self._inflight,
            "shapes": len(self._shape_devices),
            "batches": self.batches,
            "drops_busy": self.drops_busy,
            "last_infer_ms": round(self.last_infer_ms, 1),
        }


def _load_tracker_args(tracker_cfg: str):
    """读取跟踪器配置为 SimpleNamespace (与 ultralytics on_predict_start 同做法).

    直接用 YAML.load 解析本地路径, 不依赖 check_yaml —— 新版 ultralytics 已移除该
    工具, 而这里传入的始终是本地绝对路径 (configs/bytetrack.yaml), 无需其路径搜索.
    """
    from ultralytics.utils import IterableSimpleNamespace, YAML
    return IterableSimpleNamespace(**YAML.load(tracker_cfg))


def _build_tracker(args):
    """按配置构造一路官方跟踪器实例 (botsort/bytetrack).

    8.3.x 签名为 (args, frame_rate), frame_rate=30 时 max_time_lost == track_buffer;
    8.4.x 起移除 frame_rate 参数, 直接用 args.track_buffer (语义等价), 故先按旧签名
    构造、TypeError 时回退新签名, 兼容两个版本系列 (requirements 锁 <9 为第一道防线).
    """
    from ultralytics.trackers import BOTSORT, BYTETracker

    tracker_type = getattr(args, "tracker_type", "botsort")
    base_cls = {"bytetrack": BYTETracker, "botsort": BOTSORT}.get(tracker_type)
    if base_cls is None:
        raise TrackerApiError(f"未知 tracker_type: {tracker_type}")

    class LocalIdTracker(base_cls):
        # Ultralytics 的构造器会 reset_id()，而 BaseTrack 使用进程级共享计数器。
        # 每次新建/重连都会让其他设备的活跃 tracker 复用 ID。这里禁用全局
        # reset，并为每个 tracker 创建的轨迹绑定独立的单调 ID 分配器。
        def __init__(self, *args, **kwargs):
            self._local_track_ids = itertools.count(1)
            super().__init__(*args, **kwargs)

        @staticmethod
        def reset_id():
            pass

        def init_track(self, *args, **kwargs):
            tracks = super().init_track(*args, **kwargs)
            for track in tracks:
                track.next_id = self._next_local_id
            return tracks

        def _next_local_id(self):
            return next(self._local_track_ids)

    try:
        return LocalIdTracker(args=args, frame_rate=30)
    except TypeError:
        return LocalIdTracker(args=args)
