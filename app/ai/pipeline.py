"""单设备处理管道: 拉流 -> 跟踪 -> 越线计数 -> 推送事件到后端."""
import asyncio
from datetime import datetime

import httpx

from ..common.config import settings
from ..common.logger import logger
from ..schemas.events import EventIn
from .counter import LineCrossingCounter, Point
from .stream import stream_frames
from .tracker import Tracker


class DevicePipeline:
    """一路摄像头的完整处理流水线."""

    def __init__(
        self,
        device_id: str,
        stream_url: str,
        line: tuple[Point, Point],
    ):
        self.device_id = device_id
        self.stream_url = stream_url
        self.line = line
        self.tracker = Tracker()
        self.counter = LineCrossingCounter(line)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._client = httpx.AsyncClient(timeout=10.0)

    async def _run(self) -> None:
        logger.info(f"[{self.device_id}] 管道启动: {self.stream_url}")
        try:
            async for frame, _idx in stream_frames(self.stream_url, self._stop):
                # 跟踪 (同步模型推理, 放线程池避免阻塞)
                tracks = await asyncio.to_thread(self.tracker.update, frame)
                for t in tracks:
                    event_type = self.counter.update(
                        t.track_id, t.category, t.center
                    )
                    if event_type:
                        await self._push(event_type)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            logger.error(f"[{self.device_id}] 管道异常: {e}")
        finally:
            await self._client.aclose()
            logger.info(f"[{self.device_id}] 管道停止")

    async def _push(self, event_type: str) -> None:
        payload = EventIn(
            device_id=self.device_id,
            event_type=event_type,
            occurred_at=datetime.utcnow(),
        )
        try:
            resp = await self._client.post(
                f"{settings.backend_url}/api/events",
                json=payload.model_dump(mode="json"),
            )
            logger.info(
                f"[{self.device_id}] 推送 {event_type} -> HTTP {resp.status_code}"
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"[{self.device_id}] 推送失败: {e}")

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
