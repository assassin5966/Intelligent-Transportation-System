"""单设备处理管道: 拉流 -> 跟踪 -> 越线计数 -> 推送事件到后端 + WebSocket推送.

并发模型:
  - 每路设备一个 DevicePipeline; supervisor 任务守护, 管道退出后自动重启 (退避);
  - 两种推理形态 (由 app/ai/model_pool.py 在服务启动时决定):
      lease 路径 : 帧提交给本路所属批量引擎 (gpu_batch: 每卡 1 实例;
                   cpu_batch: 共享 CPU 引擎池), 跟踪状态仍由本路
                   ByteTracker 持有 (track_from_raw);
      legacy     : 推理走专用线程池 (inference_scheduler), 本地模型逐帧推理,
                   即优化前行为, 仅作最终兜底 (INFER_MODE=legacy 或槽位耗尽);
  - 帧循环只做计算与入队, 所有 HTTP 上报 (事件/异常/拥挤/心跳) 由后台任务发送;
  - WS 广播非阻塞 (有界队列, 满时丢最旧), 慢客户端不影响帧循环.
"""
import asyncio
import hashlib
import json
import math
import time
from uuid import uuid4
from zoneinfo import ZoneInfo
from collections import deque
from datetime import datetime
from typing import Callable, List, Optional

import cv2
import httpx

from ..common.business_rules import get_rule
from ..common.config import settings
from ..common.logger import logger
from ..schemas.events import CrossingEvent
from .anomaly import AnomalyEvent, AnomalyMonitor
from .counter import LineCrossingCounter, Point
from .event_outbox import event_outbox
from .gpu_engine import EngineBusy
from .inference_scheduler import InferBusy, run_inference
from .model_pool import Lease
from .stream import FramePacket, stream_frames
from .tracker import ByteTracker

_ws_clients: set = set()

# 运行时统计输出周期 (秒): 有效处理帧率/推理耗时/丢帧率/事件数/各环节拒绝次数.
# 在线管道此前无任何性能与环节指标, CPU 无 GPU 场景下漏计无法定位是"处理慢丢帧"
# 还是"计数逻辑拒绝", 故周期汇总一行 INFO 日志. 周期由 COUNT_STATS_INTERVAL 配置.
_STATS_INTERVAL = settings.count_stats_interval

# 慢客户端防护: WS 客户端队列上限 (条). 每帧广播 tracks, 前端卡顿时无界队列会
# 无限积压内存. 队列满时丢弃最旧消息 (丢的是实时画面轨迹, 不影响计数事件链路).
_WS_QUEUE_MAX = 256

# 热重载参数重读周期 (秒): imgsz 等改后由下一次重读生效, 不必每帧 stat 配置文件
_RULE_REFRESH_S = 5.0

# 有效处理帧率的健康下限: 低于该值越线计数会因采样缝隙漏计 (现场曾掉到 0.1fps)
_MIN_HEALTHY_FPS = 5.0

# WS 轨迹广播频率上限 (秒): 每帧广播在 32 路下约 200 msg/s, 与计数无关且吃带宽
_TRACKS_WS_INTERVAL = 0.2

# 推理等待超过该值 (ms) 判定引擎过载, 主动跳帧降载 (避免请求无界排队变陈旧帧)
_OVERLOAD_WAIT_MS = 1000.0


def make_infer_preprocess() -> Callable:
    """推理前预降采样钩子 (在 stream 的读帧线程内执行, 不占用推理/事件循环线程).

    4K 流直接送进推理会带来三个浪费: 解码端颜色转换/拷贝开销按原始像素计、
    letterbox 前的大图 CPU 缩放、以及大帧在单卡推理线程里串行处理. 这里先把
    短边降到推理尺寸 (INTER_AREA, 质量优于 ultralytics 内部 letterbox 所用插值),
    引擎侧再 letterbox 几乎是无损的等尺寸操作.

    目标尺寸按热重载规则读取 (5s 缓存), 与引擎每批读取的 inference.imgsz 收敛;
    即使短暂不一致也仅影响输入质量, 不影响坐标正确性 (引擎出口按实际帧尺寸还原).
    """
    state = {"size": 0, "at": 0.0}

    def _preprocess(frame):
        now = time.monotonic()
        if now - state["at"] > _RULE_REFRESH_S:
            state["size"] = int(
                get_rule("inference", "imgsz", default=settings.infer_imgsz) or 0
            )
            state["at"] = now
        target = state["size"]
        if target <= 0:
            return frame
        h, w = frame.shape[:2]
        short = min(w, h)
        if short <= target:
            return frame
        f = target / float(short)
        return cv2.resize(
            frame, (int(round(w * f)), int(round(h * f))), interpolation=cv2.INTER_AREA
        )

    return _preprocess


def register_ws_client(queue: asyncio.Queue):
    _ws_clients.add(queue)


def unregister_ws_client(queue: asyncio.Queue):
    _ws_clients.discard(queue)


async def broadcast_ws_message(message: dict):
    # 遍历快照, 避免 WebSocket 断开修改 set 时 RuntimeError;
    # put_nowait + 满时丢最旧: 广播永不阻塞帧循环, 慢客户端不拖垮计数管道
    for queue in list(_ws_clients):
        try:
            if queue.full():
                try:
                    queue.get_nowait()  # 丢弃最旧, 为最新消息腾位
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(message)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"WebSocket 广播失败: {e}")


class DevicePipeline:
    """一路摄像头的完整处理流水线."""

    def __init__(
        self,
        device_id: str,
        stream_url: str,
        line: tuple[Point, Point],
        anchor: Optional[Point] = None,
        count_only: Optional[str] = None,
        camera_type: Optional[str] = None,
        roi: Optional[List[Point]] = None,
        enable_url_refresh: bool = False,
        lease: Optional[Lease] = None,
    ):
        self.device_id = device_id
        self.stream_url = stream_url
        config_data = {"line": line, "anchor": anchor, "roi": roi,
                       "count_only": count_only, "camera_type": camera_type}
        self._config_data = config_data
        self._config_hash = hashlib.sha256(json.dumps(config_data, sort_keys=True, separators=(',', ':'),
                                                       allow_nan=False).encode()).hexdigest()
        self._stream_epoch = uuid4().hex
        self.tracker = ByteTracker(camera_type=camera_type)
        # lease 非空 => 模型由共享批量引擎持有 (gpu_batch/cpu_batch); 为空 => legacy 独立实例
        self.lease = lease
        self.mode = lease.mode if lease is not None else "legacy"
        # 预降采样钩子只在批量模式提供: legacy 的坐标还原在跟踪器内部完成,
        # 帧尺寸必须与计数器基准 (流原始分辨率) 一致
        self.decoder = make_infer_preprocess() if lease is not None else None
        self.counter = LineCrossingCounter(line, anchor)
        if count_only is not None:
            self.counter.count_only = count_only
        if roi is not None:
            # 归一化 ROI; 首帧 set_frame_size 后像素坐标自动重算
            self.counter.set_roi(roi)
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._client = httpx.AsyncClient(timeout=10.0)
        # 事件/异常/拥挤上报队列 + 后台发送任务: 上报从帧循环剥离, 后端变慢时
        # 帧循环不再被 10s 超时阻塞 (阻塞会丢帧 -> 漏计); 队列满时丢最旧
        self._outbox: asyncio.Queue = asyncio.Queue(maxsize=512)
        self._frame_size: Optional[tuple[int, int]] = None  # 跟踪当前帧尺寸, 变化时更新 (含流重连)
        self._anomaly_monitor = AnomalyMonitor()  # 视频异常监测 (黑屏/花屏)
        # WVP 同步设备启用流地址刷新: 断流重连失败时回调后端 /api/devices/{id}/stream 拿新地址
        self.enable_url_refresh = enable_url_refresh
        # 车流速度统计: 最近 60 秒车辆跨线事件时间戳 (用于计算每分钟车流量, 单调时钟秒)
        self._vehicle_cross_times: deque = deque(maxlen=4096)
        # 人流速度统计: 最近 60 秒人员跨线事件时间戳 (用于计算每分钟人流量, 单调时钟秒)
        self._person_cross_times: deque = deque(maxlen=4096)
        self._last_roi_report = 0.0  # 上次 ROI 车辆数上报时刻 (monotonic 秒)
        # 运行时统计窗口 (每 _STATS_INTERVAL 秒汇总一行日志后清零)
        self._stats_window_start: Optional[float] = None
        self._stats_frames = 0  # 窗口内已处理帧数
        self._stats_infer_seconds = 0.0  # 窗口内推理累计耗时 (legacy: 含线程池排队)
        self._stats_events = 0  # 窗口内产出事件数
        self._stats_grabbed_last = 0  # 上次汇总时的连接内累计 grab 帧数 (算窗口增量)
        self._stats_sampled_last = 0  # 上次汇总时的连接内累计采样帧数 (算窗口增量)
        self._infer_busy_drops = 0  # 窗口内推理饱和丢帧数 (并入运行统计)
        # gpu_batch 专属: 批次数/组批等待/单批推理耗时/预处理, 用于对齐性能目标
        self._stats_batches = 0
        self._stats_batch_infer_seconds = 0.0
        self._stats_batch_size_total = 0
        self._stats_wait_seconds = 0.0
        self._stats_executor_wait_seconds = 0.0
        self._stats_track_seconds = 0.0
        self._stats_frame_age_seconds = 0.0
        self._stats_preprocess_seconds = 0.0
        # 帧间隔 (秒, EMA): 供轨迹状态按时间量纲换算缺失宽限期/缝合窗口
        self._last_frame_mono: Optional[float] = None
        self._last_frame_wall: Optional[float] = None
        self._frame_dt: float = 1.0 / 25.0
        # 降载/广播节流: 推理等待过长时主动跳帧, 避免请求无界排队成陈旧帧
        self._wait_ema_ms = 0.0
        self._shed_skip = 0
        self._stats_shed_drops = 0
        self._outbox_drops = 0
        self._last_tracks_ws = 0.0
        # 业务健康独立于设备心跳: supervisor 存活并不代表已取帧或完成推理.
        self._started_mono = time.monotonic()
        self._last_packet_mono: Optional[float] = None
        self._last_success_mono: Optional[float] = None
        self._success_times: deque = deque()
        self._watchdog_task: Optional[asyncio.Task] = None
        self._last_health_warning = 0.0
        self._has_processed_stream = False
        self._event_persist_started: Optional[float] = None
        # 完成时间、解码接收时间和接收至持久化完成的耗时；仅统计最近 30 秒。
        self._quality_samples: deque = deque(maxlen=4096)
        self._source_size: Optional[tuple[int, int]] = None
        self._output_size: Optional[tuple[int, int]] = None
        self._decode_backend = "unknown"
        self._decode_max_fps: Optional[float] = None

    # ---- 主处理循环 ----

    def _scale_to_stream(self, packet: FramePacket) -> tuple:
        """推理帧 -> 流原始分辨率 的逐轴还原系数 (兼容解码端横向拉伸与预降采样)."""
        h, w = packet.frame.shape[:2]
        sw, sh = packet.stream_size
        return (sw / float(w), sh / float(h))

    async def _process_frame(self, packet: FramePacket) -> None:
        """单帧处理: 尺寸自适应 -> 异常检测 -> 推理 -> 计数 -> 上报入队."""
        frame = packet.frame
        sw, sh = packet.stream_size
        if self._source_size != (sw, sh):
            self._source_size = (sw, sh)
            if (sw, sh) != (1920, 1080):
                logger.warning(f"[{self.device_id}] 实际源流 {sw}x{sh}，不符合 1080P 验收前提")
        self._output_size = (frame.shape[1], frame.shape[0])
        self._decode_backend = packet.decode_backend
        self._decode_max_fps = packet.decode_max_fps
        # 本地接收时间；坏时间或乱序帧不能推进 tracker 和计数状态。
        now_mono = packet.received_mono if packet.received_mono is not None else time.monotonic()
        if not math.isfinite(now_mono) or (self._last_frame_mono is not None and now_mono <= self._last_frame_mono):
            logger.warning(f"[{self.device_id}] 丢弃时间无效或乱序帧")
            return
        now_wall = packet.received_wall
        if now_wall is not None and not math.isfinite(now_wall):
            logger.warning(f"[{self.device_id}] 丢弃墙钟时间无效帧")
            return
        if (now_wall is not None and self._last_frame_wall is not None
                and self._last_frame_mono is not None):
            wall_step = now_wall - self._last_frame_wall
            mono_step = now_mono - self._last_frame_mono
            tolerance = max(0.0, float(settings.clock_jump_tolerance_s))
            if wall_step <= 0 or abs(wall_step - mono_step) > tolerance:
                logger.warning(f"[{self.device_id}] 墙钟跳变，重置轨迹并记录可能的计数缺口")
                await self._reset_stream_tracking()
                self._stream_epoch = uuid4().hex
                await event_outbox.record_coverage(self.device_id, self._stream_epoch, 'clock_discontinuity')
        if self._frame_size != (sw, sh):
            # 首帧或流重连后分辨率变化 -> 更新计数器帧尺寸.
            # 一律用"流原始分辨率"作为坐标基准, 与推理帧尺寸/解码端缩放解耦,
            # 保证 min_motion/比值阈值等口径与优化前完全一致.
            self.counter.set_frame_size(sw, sh)
            self._frame_size = (sw, sh)
            logger.info(f"[{self.device_id}] 计数线帧尺寸: {sw}x{sh}")
        # 视频异常检测 (周期采样 + 去抖, 仅状态转移时上报)
        anomaly_ev = self._anomaly_monitor.check(frame)
        if anomaly_ev is not None:
            anomaly_ev.timestamp = datetime.now().isoformat()
            self._enqueue_anomaly(anomaly_ev)

        if self.lease is not None:
            # 过载降载: 上一批等待已超阈值时主动跳过本帧, 让引擎队列排空.
            # 注意这只能减少"排队成陈旧帧"和后续处理开销, 不减少解码成本.
            if self._shed_skip > 0:
                self._shed_skip -= 1
                self._stats_shed_drops += 1
                return
            # gpu_batch: 提交给本卡引擎组批; 队列饱和同样丢本帧取下一最新帧
            try:
                outcome = await self.lease.engine.submit(
                    self.device_id, frame, self._scale_to_stream(packet)
                )
            except EngineBusy:
                self._infer_busy_drops += 1
                return
            self._wait_ema_ms = 0.9 * self._wait_ema_ms + 0.1 * outcome.wait_ms
            if self._wait_ema_ms > _OVERLOAD_WAIT_MS:
                self._shed_skip = 1
            track_result = self.tracker.track_from_raw(outcome.raw_tracks, current_time=now_mono)
            self._stats_batches += 1
            self._stats_batch_size_total += outcome.batch_size
            self._stats_batch_infer_seconds += outcome.infer_ms / 1000.0
            self._stats_wait_seconds += outcome.wait_ms / 1000.0
            self._stats_executor_wait_seconds += outcome.executor_wait_ms / 1000.0
            self._stats_track_seconds += outcome.track_ms / 1000.0
        else:
            # legacy: 帧尺寸 == 流原始分辨率 (无解码端缩放/预降采样, 见 decode_tuning),
            # 跟踪器内部历史/速度与计数器基准同一坐标系, 无需还原
            infer_start = time.monotonic()
            try:
                track_result = await run_inference(self.tracker.track, frame, now_mono)
            except InferBusy:
                # 推理池饱和 (多路并发超上限): 丢本帧取下一最新帧, 语义与处理慢
                # 被读帧线程覆盖丢帧等价; 不计 _stats_frames, 丢帧对账
                # (grab 帧数 - processed) 会自然体现本帧被丢
                self._infer_busy_drops += 1
                return
            # 含线程池排队耗时: 多路并发时该值明显高于纯推理耗时, 即"排队"证据
            self._stats_infer_seconds += time.monotonic() - infer_start
        # 只在帧真正被处理(提交成功)后推进时间基准: 降载/饱和丢掉的帧不计入间隔,
        # 下一次的 dt 才是"距上一帧被处理"的真实间隔
        self._last_frame_mono = now_mono
        self._last_frame_wall = now_wall

        # 接收时间并非摄像头采集时间/PTS；单调时间用于间隔，本机墙钟用于事件日期。
        events = self.counter.process_tracks(
            track_result, self.device_id, current_time=now_mono,
            current_wall_time=packet.received_wall, commit_events=False,
        )
        # 同帧多个事件一起落盘，避免部分提交成功后异常退出丢掉其余事件。
        if events:
            self._event_persist_started = time.monotonic()
            try:
                await event_outbox.enqueue_many([
                    {"event_id": event.event_id, "device_id": self.device_id,
                     "event_type": event.event_type,
                     "occurred_at": (datetime.fromtimestamp(event.timestamp_epoch, ZoneInfo(settings.business_timezone)).isoformat()
                                     if settings.event_delivery_protocol == 'journal_v2' and event.timestamp_epoch is not None
                                     else event.timestamp)}
                    for event in events
                ], evidence=[{
                    "stream_epoch": self._stream_epoch,
                    "config_sha256": self._config_hash,
                    "geometry_config": self._config_data,
                    "track_id": str(event.track_id), "class_name": event.class_name,
                    "direction": event.direction, "cross_point": event.cross_point,
                    "line": event.cross_line, "confidence": event.confidence,
                    "timestamp_basis": "local_decode_receive",
                    "business_timezone": settings.business_timezone,
                    "confirmed_wall_time": packet.received_wall,
                    "confirmation_at": datetime.now().astimezone().isoformat(),
                    "confirmed_monotonic": now_mono,
                    "frame_sequence": packet.idx, "source_size": packet.stream_size,
                    "model": settings.yolo_model,
                    "inference_imgsz": get_rule("inference", "imgsz", default=settings.infer_imgsz),
                    "counting_rules": {
                        "min_distance_ratio": self.counter.min_distance_ratio,
                        "endpoint_sensitivity": self.counter.endpoint_sensitivity,
                        "hold_frames": self.counter.hold_frames,
                        "min_motion": self.counter.min_motion,
                        "hysteresis_ratio": self.counter.hysteresis_ratio,
                        "reverse_crossing_cooldown": self.counter.reverse_crossing_cooldown,
                    },
                } for event in events])
                self.counter.commit_events(events, now_mono)
            finally:
                self._event_persist_started = None
        self._stats_frames += 1
        self._stats_events += len(events)
        self._stats_preprocess_seconds += packet.preprocess_ms / 1000.0
        self._last_success_mono = time.monotonic()
        age_seconds = max(0.0, self._last_success_mono - now_mono)
        self._stats_frame_age_seconds += age_seconds
        self._quality_samples.append((self._last_success_mono, now_mono, age_seconds))
        self._success_times.append(self._last_success_mono)
        while self._success_times and self._last_success_mono - self._success_times[0] > 30.0:
            self._success_times.popleft()
        for event in events:
            if settings.count_event_log:
                # 逐事件明细 (COUNT_EVENT_LOG=true 时): 便于核对漏计/误计的时刻与交点
                logger.info(
                    f"[{self.device_id}] 越线事件 {event.event_type} "
                    f"track={event.track_id} class={event.class_name} "
                    f"dir={event.direction} "
                    f"交点=({event.cross_point[0]:.0f},{event.cross_point[1]:.0f}) "
                    f"conf={event.confidence:.2f}"
                )
            await self._broadcast_ws(event)

        # 车流/人流速度统计: 记录跨线事件时间戳 (每分钟车/人流量计算)
        now_mono = time.monotonic()
        for event in events:
            if event.event_type in ("VehicleEnter", "VehicleExit"):
                self._vehicle_cross_times.append(now_mono)
            elif event.event_type in ("PersonEnter", "PersonExit"):
                self._person_cross_times.append(now_mono)

        # 周期上报 ROI 内车辆/人员个数 (供后端拥挤判断); 间隔热重载
        roi_interval = float(get_rule("congestion", "roi_report_interval", default=settings.roi_report_interval))
        if now_mono - self._last_roi_report >= roi_interval:
            self._last_roi_report = now_mono
            roi_vehicles = self.counter.count_roi_vehicles(track_result.tracks)
            roi_persons = self.counter.count_roi_persons(track_result.tracks)
            self._enqueue_congestion(roi_vehicles, roi_persons)

        await self._broadcast_tracks_ws(track_result)

        self._report_stats(packet)

    async def _run_once(self) -> None:
        """单次管道生命周期: 拉流循环 + 心跳/上报子任务; 供 supervisor 反复拉起."""
        logger.info(f"[{self.device_id}] 管道启动: {self.stream_url}")
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        sender_task = asyncio.create_task(self._outbox_sender())
        current_generation = None
        try:
            url_provider = self._refresh_stream_url if self.enable_url_refresh else None
            async for packet in stream_frames(
                self.stream_url, self._stop,
                url_provider=url_provider, preprocess=self.decoder,
                # 解码侧采样/缩放只在 gpu_batch 生效: legacy (含 GPU 槽位耗尽回退、
                # CPU 部署) 保持优化前的原始帧率与分辨率, 坐标基准与跟踪器内一致
                decode_tuning=self.lease is not None,
            ):
                if current_generation != packet.stream_generation:
                    if self._has_processed_stream:
                        await self._reset_stream_tracking()
                    current_generation = packet.stream_generation
                    self._stream_epoch = uuid4().hex
                    self._has_processed_stream = True
                self._last_packet_mono = packet.received_mono or time.monotonic()
                await self._process_frame(packet)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"[{self.device_id}] 管道异常: {e}")
        finally:
            for t in (heartbeat_task, sender_task):
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
            logger.info(f"[{self.device_id}] 管道停止")

    async def _supervisor(self) -> None:
        """守护循环: 管道异常退出/流重连放弃后自动重启 (指数退避).

        此前 _run 结束即静默死亡 (设备留在 _pipelines 但 running=False),
        计数永久停止且无任何恢复手段; 现在除主动 stop 外始终重启,
        退避 1s 起步、封顶 60s, 避免摄像头长时间离线时打爆 ZLM/WVP.
        """
        backoff = 1.0
        first = True
        try:
            while not self._stop.is_set():
                if not first:
                    logger.info(f"[{self.device_id}] 管道 {backoff:.0f}s 后自动重启")
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                        break
                    except asyncio.TimeoutError:
                        pass
                first = False
                await self._run_once()
                backoff = min(backoff * 2, 60.0)
        finally:
            await self._client.aclose()
            logger.info(f"[{self.device_id}] 管道守护退出")

    async def _reset_stream_tracking(self) -> None:
        """连接代次变化时不让旧轨迹/ID 跨断流延续."""
        if self.lease is not None:
            await self.lease.engine.reset_device(self.device_id)
        self.tracker.reset()
        self.counter.reset()
        self._last_frame_mono = None
        self._last_frame_wall = None
        self._frame_dt = 1.0 / 25.0
        self._stats_grabbed_last = 0
        self._stats_sampled_last = 0
        # 上一连接的累计序号无法与新连接相减；丢弃未满窗口的统计，避免误报。
        self._stats_window_start = None
        self._stats_frames = self._stats_events = self._infer_busy_drops = 0
        self._stats_batches = self._stats_batch_size_total = self._stats_shed_drops = 0
        self._stats_infer_seconds = self._stats_batch_infer_seconds = 0.0
        self._stats_wait_seconds = self._stats_executor_wait_seconds = 0.0
        self._stats_track_seconds = self._stats_frame_age_seconds = self._stats_preprocess_seconds = 0.0
        self._quality_samples.clear()
        logger.info(f"[{self.device_id}] 流连接变化, 已重置跟踪和计数状态")

    def health_snapshot(self) -> dict:
        now = time.monotonic()
        while self._success_times and now - self._success_times[0] > 30.0:
            self._success_times.popleft()
        while self._quality_samples and now - self._quality_samples[0][0] > 30.0:
            self._quality_samples.popleft()
        samples = list(self._quality_samples)
        ages_ms = sorted(s[2] * 1000.0 for s in samples)
        def percentile(p):
            return round(ages_ms[max(0, math.ceil(len(ages_ms) * p) - 1)], 1) if ages_ms else None
        gaps = [samples[i][1] - samples[i - 1][1] for i in range(1, len(samples))]
        # 包含当前仍未结束的空窗；这里的时间是本机接收时间，不是摄像头 PTS。
        if samples:
            gaps.append(max(0.0, now - samples[-1][1]))
        age_packet = None if self._last_packet_mono is None else max(0.0, now - self._last_packet_mono)
        age_success = None if self._last_success_mono is None else max(0.0, now - self._last_success_mono)
        age_start = max(0.0, now - self._started_mono)
        limit = max(1.0, float(settings.stream_stall_timeout_s or 15.0))
        if not self.running:
            state = "stopped"
        elif self._event_persist_started is not None and now - self._event_persist_started >= 5.0:
            state = "event_persist_blocked"
        elif age_packet is None:
            state = "waiting_first_frame" if age_start < limit else "no_frames"
        elif age_packet >= limit:
            state = "no_recent_frame"
        elif age_success is None or age_success >= limit:
            state = "no_recent_inference"
        elif self._anomaly_monitor.state in ("black_screen", "flower_screen"):
            state = self._anomaly_monitor.state
        elif age_start >= 30.0 and len(self._success_times) / 30.0 < _MIN_HEALTHY_FPS:
            state = "low_fps"
        else:
            state = "processing"
        return {"state": state, "current_processing_ready": state == "processing",
                "coverage_status": "unknown",
                "stream_epoch": self._stream_epoch, "config_sha256": self._config_hash,
                "fps_30s": round(len(self._success_times) / min(30.0, max(age_start, 1.0)), 2),
                "last_frame_age_s": None if age_packet is None else round(age_packet, 2),
                "last_processed_age_s": None if age_success is None else round(age_success, 2),
                "timestamp_basis": "local_decode_receive",
                "source_size": self._source_size, "output_size": self._output_size,
                "source_is_1080p": None if self._source_size is None else self._source_size == (1920, 1080),
                "decode_backend": self._decode_backend,
                "decode_max_fps_at_connect": self._decode_max_fps,
                "latency_sample_count_30s": len(samples),
                "receive_to_persist_p95_ms_30s": percentile(0.95),
                "receive_to_persist_p99_ms_30s": percentile(0.99),
                "max_processed_gap_s_30s": round(max(gaps), 3) if gaps else None}

    async def _watchdog_loop(self) -> None:
        while not self._stop.is_set():
            snap = self.health_snapshot()
            await event_outbox.record_coverage(self.device_id, self._stream_epoch, snap['state'])
            now = time.monotonic()
            if snap["state"] in {"no_frames", "no_recent_frame", "no_recent_inference", "low_fps", "event_persist_blocked", "black_screen", "flower_screen"} and now - self._last_health_warning >= 30.0:
                logger.warning(f"[{self.device_id}] AI 处理异常: {snap}")
                self._last_health_warning = now
            await asyncio.sleep(5.0)

    async def _heartbeat_loop(self) -> None:
        """每 30 秒向后端发送心跳, 供离线检测使用."""
        while not self._stop.is_set():
            try:
                await self._client.post(
                    f"{settings.backend_url}/api/devices/{self.device_id}/heartbeat",
                    json={"ai_status": self.health_snapshot()["state"]},
                )
            except Exception:  # noqa: BLE001
                pass  # 心跳失败不影响视频处理
            await asyncio.sleep(30)

    async def _refresh_stream_url(self) -> str:
        """WVP 流地址刷新回调: 调后端 /api/devices/{id}/stream 取新 flv 地址.

        后端内部转调 WVP play/start. 返回空串表示刷新失败 (stream_frames 会放弃重连).
        """
        try:
            resp = await self._client.get(
                f"{settings.backend_url}/api/devices/{self.device_id}/stream"
            )
            if resp.status_code == 200:
                url = resp.json().get("stream_url", "")
                logger.info(f"[{self.device_id}] 流地址已刷新: {url}")
                return url
            logger.warning(f"[{self.device_id}] 流地址刷新返回 HTTP {resp.status_code}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{self.device_id}] 流地址刷新失败: {e}")
        return ""

    # ---- 后台上报 (outbox): 帧循环只入队, 网络发送由独立任务串行执行 ----

    def _enqueue_anomaly(self, ev: AnomalyEvent) -> None:
        self._outbox_put({"kind": "anomaly", "anomaly_ev": ev})

    def _enqueue_congestion(self, roi_vehicles: int, roi_persons: int) -> None:
        """拥挤上报快照入队: 车流/人流速度在入队时计算 (monotonic 语义保持)."""
        now_mono = time.monotonic()
        self._outbox_put({
            "kind": "congestion",
            "roi_vehicles": roi_vehicles,
            "roi_persons": roi_persons,
            "vehicle_flow_per_min": self._vehicle_flow_per_minute(now_mono),
            "person_flow_per_min": self._person_flow_per_minute(now_mono),
        })

    def _outbox_put(self, message: dict) -> None:
        """有界入队: 满时丢最旧 (周期上报允许丢, 保最新状态).

        越线事件使用独立的持久队列，这里只承载可覆盖的快照消息。
        """
        if self._outbox.full():
            try:
                self._outbox.get_nowait()
                self._outbox_drops += 1  # 拥塞丢弃计数: 并入运行统计, 避免静默丢事件
            except asyncio.QueueEmpty:
                pass
        self._outbox.put_nowait(message)

    async def _outbox_sender(self) -> None:
        """后台串行发送 outbox 消息 (事件/异常/拥挤), 与帧循环解耦.

        失败仅记日志不重试: 越线事件重发会导致后端重复计数 (计数器去重只防
        传感器侧重复, 不防网络重发), 拥挤/异常为周期性快照, 丢一条由下一周期覆盖.
        """
        while True:
            message = await self._outbox.get()
            try:
                if message["kind"] == "congestion":
                    await self._report_congestion(
                        message["roi_vehicles"], message["roi_persons"],
                        vehicle_flow_per_min=message["vehicle_flow_per_min"],
                        person_flow_per_min=message["person_flow_per_min"],
                    )
                elif message["kind"] == "anomaly":
                    await self._handle_anomaly(message["anomaly_ev"])
            except Exception as e:  # noqa: BLE001
                logger.error(f"[{self.device_id}] 上报任务异常: {e}")

    def _report_stats(self, packet: FramePacket) -> None:
        """周期汇总运行统计 (每 _STATS_INTERVAL 秒一行 INFO).

        窗口内三类丢弃分开计数, 避免"设计内节流"与"真丢帧"混成一个数:
          - 节流丢弃 = grab增量 - 采样增量: 解码侧按 decode_max_fps 主动不 retrieve
            的帧, 属设计内行为 (省 CPU 正是优化目的); legacy 无节流时恒为 0;
          - 丢帧     = 采样增量 - 处理帧: 处理慢于上游 -> 未处理帧被最新帧覆盖,
            目标跨线动作可能整段丢失 -> 漏计。这才是验收项 (≤5%);
          - 推理饱和丢帧 = 推理池满主动丢弃 (EngineBusy), 单列定位。
        分母取采样帧数后, legacy 与 gpu_batch 口径一致 (legacy 节流丢弃=0, 旧口径
        即本口径), 优化前后的数值可直接对比。
        推理耗时含线程池排队时间, 多路并发时是"排队"的直接证据.
        拒绝次数给出事件被哪一环节拦下 (ROI/夹角过滤/ID切换/防抖/滞留/去重/单向模式).
        """
        now = time.monotonic()
        if self._stats_window_start is None:
            self._stats_window_start = now
            return
        elapsed = now - self._stats_window_start
        if elapsed < _STATS_INTERVAL:
            return

        processed = self._stats_frames
        # grabbed / sampled 都是"当前连接内累计值", 需减去上次汇总基线得到窗口增量;
        # 断流重连后两者归零 (小于基线) 时退化为按已处理帧数计, 避免窗口值为负
        window_grabbed = packet.grabbed - self._stats_grabbed_last
        if window_grabbed < 0:
            window_grabbed = processed
        window_sampled = packet.sampled - self._stats_sampled_last
        if window_sampled < 0:
            window_sampled = processed
        throttled = max(window_grabbed - window_sampled, 0)   # 解码侧节流 (设计内)
        dropped = max(window_sampled - processed, 0)          # 处理侧真丢帧 (验收项)
        dropped_pct = dropped / window_sampled * 100.0 if window_sampled else 0.0
        reject = self.counter.pop_reject_stats()
        reject_str = " ".join(f"{k}={v}" for k, v in sorted(reject.items())) or "无"

        if self.lease is not None:
            # gpu_batch: 单批耗时/组批等待/均批大小才是对齐性能目标的口径
            # (累计的是每批耗时, 不能按帧平均, 否则会按批大小放大)
            batches = self._stats_batches
            infer_str = (
                f"推理={self._stats_batch_infer_seconds / batches * 1000.0:.0f}ms/批 "
                f"均批={self._stats_batch_size_total / batches:.1f} "
                f"组批等待={self._stats_wait_seconds / batches * 1000.0:.0f}ms "
                f"执行器等待={self._stats_executor_wait_seconds / batches * 1000.0:.0f}ms "
                f"跟踪={self._stats_track_seconds / batches * 1000.0:.0f}ms "
                f"接收至完成={self._stats_frame_age_seconds / (processed or 1) * 1000.0:.0f}ms "
                f"预处理={self._stats_preprocess_seconds / (processed or 1) * 1000.0:.1f}ms/帧"
                if batches else "推理=无批次"
            )
            mode_str = f"模式={self.mode} 引擎={self.lease.engine_id}"
        else:
            infer_ms = self._stats_infer_seconds / processed * 1000.0 if processed else 0.0
            infer_str = f"推理={infer_ms:.0f}ms/帧"
            mode_str = "模式=legacy"

        fps = processed / elapsed if elapsed else 0.0
        quality = self.health_snapshot()
        logger.info(
            f"[{self.device_id}] 运行统计({elapsed:.0f}s): "
            f"处理fps={fps:.2f} "
            f"grab帧={window_grabbed} 采样帧={window_sampled} 节流丢弃={throttled} "
            f"丢帧={dropped}({dropped_pct:.0f}%) "
            f"推理饱和丢帧={self._infer_busy_drops} 降载丢帧={self._stats_shed_drops} "
            f"outbox丢弃={self._outbox_drops} "
            f"接收至持久化P95={quality['receive_to_persist_p95_ms_30s']}ms "
            f"接收至持久化P99={quality['receive_to_persist_p99_ms_30s']}ms "
            f"最大处理帧间隔={quality['max_processed_gap_s_30s']}s "
            f"{mode_str} {infer_str} "
            f"事件={self._stats_events} | 拒绝: {reject_str}"
        )
        if fps < _MIN_HEALTHY_FPS:
            # 越线计数依赖连续轨迹: 帧率过低时目标跨线动作会落在采样缝隙里
            logger.warning(
                f"[{self.device_id}] 有效帧率过低 {fps:.2f}fps (<{_MIN_HEALTHY_FPS:.0f}), "
                f"越线计数可能漏计 — 检查推理负载/在线路数/流地址"
            )

        self._stats_window_start = now
        self._stats_grabbed_last = packet.grabbed
        self._stats_sampled_last = packet.sampled
        self._stats_frames = 0
        self._stats_infer_seconds = 0.0
        self._stats_events = 0
        self._infer_busy_drops = 0
        self._stats_shed_drops = 0
        self._stats_batches = 0
        self._stats_batch_infer_seconds = 0.0
        self._stats_batch_size_total = 0
        self._stats_wait_seconds = 0.0
        self._stats_executor_wait_seconds = 0.0
        self._stats_track_seconds = 0.0
        self._stats_frame_age_seconds = 0.0
        self._stats_preprocess_seconds = 0.0

    def _vehicle_flow_per_minute(self, now_mono: float) -> float:
        """最近 60 秒内车辆跨线次数折算为每分钟车流量 (辆/分钟).

        车流速度指标: 反映车流通过计数线的速率, 停滞/缓行时趋近 0.
        """
        cutoff = now_mono - 60.0
        while self._vehicle_cross_times and self._vehicle_cross_times[0] < cutoff:
            self._vehicle_cross_times.popleft()
        return float(len(self._vehicle_cross_times))

    def _person_flow_per_minute(self, now_mono: float) -> float:
        """最近 60 秒内人员跨线次数折算为每分钟人流量 (人/分钟).

        人流速度指标: 反映人流通过计数线的速率, 停滞时趋近 0.
        """
        cutoff = now_mono - 60.0
        while self._person_cross_times and self._person_cross_times[0] < cutoff:
            self._person_cross_times.popleft()
        return float(len(self._person_cross_times))

    async def _report_congestion(
        self,
        roi_vehicles: int,
        roi_persons: int = 0,
        vehicle_flow_per_min: float = 0.0,
        person_flow_per_min: float = 0.0,
    ) -> None:
        """上报 ROI 内车辆/人员个数 + 每分钟车/人流量, 供后端拥挤判定.

        速度值由入队侧 (_enqueue_congestion) 预计算后透传, 保证统计口径
        是事件发生时刻的快照. 由后台任务调用, 不阻塞帧循环.
        """
        payload = {
            "device_id": self.device_id,
            "roi_vehicles": roi_vehicles,
            "roi_persons": roi_persons,
            "vehicle_flow_per_min": vehicle_flow_per_min,
            "person_flow_per_min": person_flow_per_min,
        }
        try:
            resp = await self._client.post(
                f"{settings.backend_url}/api/stats/congestion",
                json=payload,
            )
            logger.debug(
                f"[{self.device_id}] 拥挤上报 roi_vehicles={roi_vehicles} "
                f"roi_persons={roi_persons} flow={vehicle_flow_per_min:.1f}/min "
                f"person_flow={person_flow_per_min:.1f}/min -> HTTP {resp.status_code}"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{self.device_id}] 拥挤上报失败: {e}")

    async def _handle_anomaly(self, ev: AnomalyEvent) -> None:
        """视频异常 (黑屏/花屏) 上报: AI WS 广播 + POST 后端告警端点.

        onset -> 后端持久化 critical 告警; recovery -> info 告警 (后端冷却去重).
        失败仅记日志, 不阻塞视频处理 (由 outbox 后台任务调用).
        """
        # AI 服务 WebSocket 实时广播 (供前端画面层即时提示)
        await broadcast_ws_message({
            "type": "video_anomaly",
            "device_id": self.device_id,
            "anomaly_type": ev.anomaly_type,
            "phase": ev.phase,
            "scores": ev.scores,
            "timestamp": ev.timestamp,
        })
        # 后端告警通道 (持久化到 Redis + 后端 WS 推送, 前端告警列表可见)
        payload = {
            "device_id": self.device_id,
            "anomaly_type": ev.anomaly_type,
            "phase": ev.phase,
            "scores": ev.scores,
        }
        try:
            resp = await self._client.post(
                f"{settings.backend_url}/api/alerts/anomaly", json=payload
            )
            logger.info(
                f"[{self.device_id}] 异常上报 {ev.anomaly_type}/{ev.phase} -> HTTP {resp.status_code}"
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"[{self.device_id}] 异常上报失败: {e}")

    async def _broadcast_ws(self, event: CrossingEvent) -> None:
        message = {
            "type": "crossing_event",
            "device_id": self.device_id,
            "event_type": event.event_type,
            "track_id": event.track_id,
            "class_name": event.class_name,
            "timestamp": event.timestamp,
            "cross_point": event.cross_point,
            "cross_line": event.cross_line,
            "direction": event.direction,
            "confidence": event.confidence,
        }
        await broadcast_ws_message(message)

    async def _broadcast_tracks_ws(self, track_result) -> None:
        # 广播节流: 前端叠加标注只需 5Hz; 按帧推送在 32 路下约 200 msg/s,
        # 与计数无关却占满带宽与后端 fan-out CPU.
        now = time.monotonic()
        if now - self._last_tracks_ws < _TRACKS_WS_INTERVAL:
            return
        self._last_tracks_ws = now
        tracks_data = []
        for track in track_result.tracks:
            tracks_data.append({
                "track_id": track.track_id,
                "class_name": track.class_name,
                "bbox": track.bbox,
                "center": track.center,
                "confidence": track.confidence,
                "age": track.age,
                "velocity": track.velocity,
                # 轨迹历史中心点 (deque 不支持切片, 先转 list; 隔二取一控制包体积,
                # track_buffer=30 -> ~10 点), 坐标已还原到流原始分辨率, 前端直接可画
                "history": list(track.history)[::3],
            })

        message = {
            "type": "tracks",
            "device_id": self.device_id,
            "frame_id": track_result.frame_id,
            "timestamp": datetime.now().isoformat(),
            "tracks": tracks_data,
        }
        await broadcast_ws_message(message)

    def start(self) -> None:
        self._stop.clear()
        self._started_mono = time.monotonic()
        self._task = asyncio.create_task(self._supervisor())
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            self._watchdog_task = None
        await event_outbox.record_coverage(self.device_id, self._stream_epoch, 'stopped')
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()
