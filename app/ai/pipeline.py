"""单设备处理管道: 拉流 -> 跟踪 -> 越线计数 -> 推送事件到后端 + WebSocket推送.

并发模型:
  - 每路设备一个 DevicePipeline; supervisor 任务守护, 管道退出后自动重启 (退避);
  - 推理 (CPU 密集) 走专用线程池 (inference_scheduler), 多路并发受限且池满丢帧;
  - 帧循环只做计算与入队, 所有 HTTP 上报 (事件/异常/拥挤/心跳) 由后台任务发送;
  - WS 广播非阻塞 (有界队列, 满时丢最旧), 慢客户端不影响帧循环.
"""
import asyncio
import time
from collections import deque
from datetime import datetime
from typing import List, Optional

import httpx

from ..common.business_rules import get_rule
from ..common.config import settings
from ..common.logger import logger
from ..schemas.events import EventIn, CrossingEvent
from .anomaly import AnomalyEvent, AnomalyMonitor
from .counter import LineCrossingCounter, Point
from .inference_scheduler import InferBusy, run_inference
from .stream import stream_frames
from .tracker import ByteTracker

_ws_clients: set = set()

# 运行时统计输出周期 (秒): 有效处理帧率/推理耗时/丢帧率/事件数/各环节拒绝次数.
# 在线管道此前无任何性能与环节指标, CPU 无 GPU 场景下漏计无法定位是"处理慢丢帧"
# 还是"计数逻辑拒绝", 故周期汇总一行 INFO 日志. 周期由 COUNT_STATS_INTERVAL 配置.
_STATS_INTERVAL = settings.count_stats_interval

# 慢客户端防护: WS 客户端队列上限 (条). 每帧广播 tracks, 前端卡顿时无界队列会
# 无限积压内存. 队列满时丢弃最旧消息 (丢的是实时画面轨迹, 不影响计数事件链路).
_WS_QUEUE_MAX = 256


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
    ):
        self.device_id = device_id
        self.stream_url = stream_url
        self.tracker = ByteTracker(camera_type=camera_type)
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
        self._stats_infer_seconds = 0.0  # 窗口内推理累计耗时 (含线程池排队)
        self._stats_events = 0  # 窗口内产出事件数
        self._stats_decoded_last = 0  # 上次汇总时的连接内累计解码帧数 (算窗口增量)
        self._infer_busy_drops = 0  # 窗口内推理池饱和丢帧数 (并入运行统计)

    # ---- 主处理循环 ----

    async def _process_frame(self, frame, decoded: int) -> None:
        """单帧处理: 尺寸自适应 -> 异常检测 -> 推理 -> 计数 -> 上报入队."""
        if frame is not None:
            h, w = frame.shape[:2]
            if self._frame_size != (w, h):
                # 首帧或流重连后分辨率变化 -> 更新计数器帧尺寸
                self.counter.set_frame_size(w, h)
                self._frame_size = (w, h)
                logger.info(f"[{self.device_id}] 计数线帧尺寸: {w}x{h}")
            # 视频异常检测 (周期采样 + 去抖, 仅状态转移时上报)
            anomaly_ev = self._anomaly_monitor.check(frame)
            if anomaly_ev is not None:
                anomaly_ev.timestamp = datetime.now().isoformat()
                self._enqueue_anomaly(anomaly_ev)

        infer_start = time.monotonic()
        try:
            track_result = await run_inference(self.tracker.track, frame)
        except InferBusy:
            # 推理池饱和 (多路并发超上限): 丢本帧取下一最新帧, 语义与处理慢
            # 被读帧线程覆盖丢帧等价; 不计 _stats_frames, 丢帧对账
            # (decoded - processed) 会自然体现本帧被丢
            self._infer_busy_drops += 1
            return
        # 含线程池排队耗时: 多路并发时该值明显高于纯推理耗时, 即"排队"证据
        self._stats_infer_seconds += time.monotonic() - infer_start
        self._stats_frames += 1

        # 单调时钟统一: 计数器内部冷却/去重/TTL 与本管道的车流统计
        # (_vehicle_cross_times) 均基于 monotonic, 不受系统对时跳变影响
        events = self.counter.process_tracks(
            track_result, self.device_id, current_time=time.monotonic(),
        )
        self._stats_events += len(events)

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
            # 事件入 outbox 由后台任务发送, 帧循环不再被 HTTP 往返阻塞
            self._enqueue_event(event)

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

        self._report_stats(decoded)

    async def _run_once(self) -> None:
        """单次管道生命周期: 拉流循环 + 心跳/上报子任务; 供 supervisor 反复拉起."""
        logger.info(f"[{self.device_id}] 管道启动: {self.stream_url}")
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        sender_task = asyncio.create_task(self._outbox_sender())
        try:
            url_provider = self._refresh_stream_url if self.enable_url_refresh else None
            async for frame, _idx, decoded in stream_frames(
                self.stream_url, self._stop, url_provider=url_provider
            ):
                await self._process_frame(frame, decoded)
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
        while not self._stop.is_set():
            if not first:
                logger.info(f"[{self.device_id}] 管道 {backoff:.0f}s 后自动重启")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                    break  # stop 被触发, 正常退出
                except asyncio.TimeoutError:
                    pass
            first = False
            await self._run_once()
            backoff = min(backoff * 2, 60.0)
        await self._client.aclose()
        logger.info(f"[{self.device_id}] 管道守护退出")

    async def _heartbeat_loop(self) -> None:
        """每 30 秒向后端发送心跳, 供离线检测使用."""
        while not self._stop.is_set():
            try:
                await self._client.post(
                    f"{settings.backend_url}/api/devices/{self.device_id}/heartbeat"
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

    def _enqueue_event(self, event: CrossingEvent) -> None:
        self._outbox_put({"kind": "event", "event": event})

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

        注: 越线事件满时同样丢最旧 —— outbox 容量 512 远大于单帧事件量,
        仅在后端长时间不可达时才可能触顶, 此时丢的也是积压最旧的历史事件.
        """
        if self._outbox.full():
            try:
                self._outbox.get_nowait()
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
                if message["kind"] == "event":
                    await self._push(message["event"])
                elif message["kind"] == "congestion":
                    await self._report_congestion(
                        message["roi_vehicles"], message["roi_persons"],
                        vehicle_flow_per_min=message["vehicle_flow_per_min"],
                        person_flow_per_min=message["person_flow_per_min"],
                    )
                elif message["kind"] == "anomaly":
                    await self._handle_anomaly(message["anomaly_ev"])
            except Exception as e:  # noqa: BLE001
                logger.error(f"[{self.device_id}] 上报任务异常: {e}")

    async def _push(self, event: CrossingEvent) -> None:
        payload = EventIn(
            device_id=self.device_id,
            event_type=event.event_type,
            occurred_at=datetime.fromisoformat(event.timestamp),
        )
        try:
            resp = await self._client.post(
                f"{settings.backend_url}/api/events",
                json=payload.model_dump(mode="json"),
            )
            logger.info(
                f"[{self.device_id}] 推送 {event.event_type} -> HTTP {resp.status_code}"
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"[{self.device_id}] 推送失败: {e}")

    def _report_stats(self, decoded: int) -> None:
        """周期汇总运行统计 (每 _STATS_INTERVAL 秒一行 INFO).

        丢帧率 = 1 - 处理帧数/解码帧数: 处理慢于上游帧率时读帧线程不断用新帧覆盖
        旧帧, 未处理帧被丢弃, 目标跨线动作可能整段丢失 -> 漏计.
        "推理饱和丢帧"单列: 推理池满主动丢弃的帧, 与读帧覆盖丢帧区分定位.
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
        # decoded 是"当前连接内累计解码帧数", 需减去上次汇总时的基线得到窗口增量;
        # 断流重连后 decoded 归零 (小于基线) 时退化为按已处理帧数计, 避免丢帧数为负
        window_decoded = decoded - self._stats_decoded_last
        if window_decoded < 0:
            window_decoded = processed
        dropped = max(window_decoded - processed, 0)
        dropped_pct = dropped / window_decoded * 100.0 if window_decoded else 0.0
        infer_ms = self._stats_infer_seconds / processed * 1000.0 if processed else 0.0
        reject = self.counter.pop_reject_stats()
        reject_str = " ".join(f"{k}={v}" for k, v in sorted(reject.items())) or "无"

        logger.info(
            f"[{self.device_id}] 运行统计({elapsed:.0f}s): "
            f"处理fps={processed / elapsed:.2f} "
            f"解码={window_decoded} 丢帧={dropped}({dropped_pct:.0f}%) "
            f"推理饱和丢帧={self._infer_busy_drops} "
            f"推理={infer_ms:.0f}ms/帧 "
            f"事件={self._stats_events} | 拒绝: {reject_str}"
        )

        self._stats_window_start = now
        self._stats_decoded_last = decoded
        self._stats_frames = 0
        self._stats_infer_seconds = 0.0
        self._stats_events = 0
        self._infer_busy_drops = 0

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
        self._task = asyncio.create_task(self._supervisor())

    async def stop(self) -> None:
        self._stop.set()
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
