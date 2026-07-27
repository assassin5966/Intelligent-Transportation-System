"""单设备处理管道: 拉流 -> 跟踪 -> 越线计数 -> 推送事件到后端 + WebSocket推送."""
import asyncio
import json
from datetime import datetime
from typing import List, Optional

import httpx

from ..common.config import settings
from ..common.logger import logger
from ..schemas.events import EventIn, CrossingEvent
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
    ):
        self.device_id = device_id
        self.stream_url = stream_url
        self.line = line
        self.tracker = ByteTracker()
        self.counter = LineCrossingCounter(line, anchor)
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._client = httpx.AsyncClient(timeout=10.0)
        self._frame_size: Optional[tuple[int, int]] = None  # 跟踪当前帧尺寸, 变化时更新 (含流重连)

    async def _run(self) -> None:
        logger.info(f"[{self.device_id}] 管道启动: {self.stream_url}")
        try:
            async for frame, _idx in stream_frames(self.stream_url, self._stop):
                if frame is not None:
                    h, w = frame.shape[:2]
                    if self._frame_size != (w, h):
                        # 首帧或流重连后分辨率变化 -> 更新计数器帧尺寸
                        self.counter.set_frame_size(w, h)
                        self._frame_size = (w, h)
                        logger.info(f"[{self.device_id}] 计数线帧尺寸: {w}x{h}")
                track_result = await asyncio.to_thread(self.tracker.track, frame)
                
                events = self.counter.process_tracks(track_result, self.device_id)
                
                for event in events:
                    await self._push(event)
                    await self._broadcast_ws(event)
                    
                await self._broadcast_tracks_ws(track_result)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            logger.error(f"[{self.device_id}] 管道异常: {e}")
        finally:
            await self._client.aclose()
            logger.info(f"[{self.device_id}] 管道停止")

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