"""单设备处理管道: 拉流 -> 跟踪 -> 越线计数 -> 推送事件到后端 + WebSocket推送."""
import asyncio
import time
from collections import deque
from datetime import datetime
from typing import List, Optional

import httpx

from ..common.config import settings
from ..common.logger import logger
from ..schemas.events import EventIn, CrossingEvent
from .anomaly import AnomalyEvent, AnomalyMonitor
from .counter import LineCrossingCounter, Point
from .stream import stream_frames
from .tracker import ByteTracker

_ws_clients: set = set()


def register_ws_client(queue: asyncio.Queue):
    _ws_clients.add(queue)


def unregister_ws_client(queue: asyncio.Queue):
    _ws_clients.discard(queue)


async def broadcast_ws_message(message: dict):
    # 遍历快照, 避免 WebSocket 断开修改 set 时 RuntimeError
    for queue in list(_ws_clients):
        try:
            await queue.put(message)
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
        self._frame_size: Optional[tuple[int, int]] = None  # 跟踪当前帧尺寸, 变化时更新 (含流重连)
        self._anomaly_monitor = AnomalyMonitor()  # 视频异常监测 (黑屏/花屏)
        # WVP 同步设备启用流地址刷新: 断流重连失败时回调后端 /api/devices/{id}/stream 拿新地址
        self.enable_url_refresh = enable_url_refresh
        # 车流速度统计: 最近 60 秒跨线车辆事件时间戳 (用于计算每分钟车流量, 单调时钟秒)
        self._vehicle_cross_times: deque = deque(maxlen=4096)
        self._last_roi_report = 0.0  # 上次 ROI 车辆数上报时刻 (monotonic 秒)

    async def _run(self) -> None:
        logger.info(f"[{self.device_id}] 管道启动: {self.stream_url}")
        # 启动心跳任务 (每 30 秒向后端发送心跳)
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        try:
            url_provider = self._refresh_stream_url if self.enable_url_refresh else None
            async for frame, _idx in stream_frames(
                self.stream_url, self._stop, url_provider=url_provider
            ):
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
                        await self._handle_anomaly(anomaly_ev)
                track_result = await asyncio.to_thread(self.tracker.track, frame)

                events = self.counter.process_tracks(track_result, self.device_id)

                for event in events:
                    await self._push(event)
                    await self._broadcast_ws(event)

                # 车流速度统计: 记录车辆跨线事件时间戳 (每分钟车流量计算)
                now_mono = time.monotonic()
                for event in events:
                    if event.event_type in ("VehicleEnter", "VehicleExit"):
                        self._vehicle_cross_times.append(now_mono)

                # 周期上报 ROI 内车辆个数 (供后端拥挤判断)
                if now_mono - self._last_roi_report >= settings.roi_report_interval:
                    self._last_roi_report = now_mono
                    roi_vehicles = self.counter.count_roi_vehicles(track_result.tracks)
                    await self._report_congestion(roi_vehicles)

                await self._broadcast_tracks_ws(track_result)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            logger.error(f"[{self.device_id}] 管道异常: {e}")
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            await self._client.aclose()
            logger.info(f"[{self.device_id}] 管道停止")

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

    def _vehicle_flow_per_minute(self, now_mono: float) -> float:
        """最近 60 秒内车辆跨线次数折算为每分钟车流量 (辆/分钟).

        车流速度指标: 反映车流通过计数线的速率, 停滞/缓行时趋近 0.
        """
        cutoff = now_mono - 60.0
        while self._vehicle_cross_times and self._vehicle_cross_times[0] < cutoff:
            self._vehicle_cross_times.popleft()
        return float(len(self._vehicle_cross_times))

    async def _report_congestion(self, roi_vehicles: int) -> None:
        """周期上报 ROI 内车辆个数 + 每分钟车流量, 供后端拥挤判定.

        车流速度 = 最近 60 秒车辆跨线次数 (辆/分钟).
        上报失败仅记日志, 不阻塞视频处理.
        """
        now_mono = time.monotonic()
        flow_per_min = self._vehicle_flow_per_minute(now_mono)
        payload = {
            "device_id": self.device_id,
            "roi_vehicles": roi_vehicles,
            "vehicle_flow_per_min": flow_per_min,
        }
        try:
            resp = await self._client.post(
                f"{settings.backend_url}/api/stats/congestion",
                json=payload,
            )
            logger.debug(
                f"[{self.device_id}] 拥挤上报 roi_vehicles={roi_vehicles} "
                f"flow={flow_per_min:.1f}/min -> HTTP {resp.status_code}"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{self.device_id}] 拥挤上报失败: {e}")

    async def _handle_anomaly(self, ev: AnomalyEvent) -> None:
        """视频异常 (黑屏/花屏) 上报: AI WS 广播 + POST 后端告警端点.

        onset -> 后端持久化 critical 告警; recovery -> info 告警 (后端冷却去重).
        失败仅记日志, 不阻塞视频处理 (与 _push 一致).
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
        self._task = asyncio.create_task(self._run())

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