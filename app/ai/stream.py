"""RTSP / GB28181 视频流拉取 (异步, 断流自动重连)."""
import asyncio
from typing import Optional

import cv2
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..common.logger import logger


class StreamError(Exception):
    pass


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type(StreamError),
    reraise=True,
)
def _open(url: str) -> cv2.VideoCapture:
    cap = cv2.VideoCapture(url)
    if not cap.isOpened():
        cap.release()
        raise StreamError(f"无法打开视频流: {url}")
    return cap


async def stream_frames(url: str, stop_event: Optional[asyncio.Event] = None):
    """异步帧生成器. cv2 同步读帧用 asyncio.to_thread 包装, 不阻塞事件循环."""
    try:
        cap = await asyncio.to_thread(_open, url)
    except StreamError as e:
        logger.error(f"视频流打开失败: {e}")
        return

    logger.info(f"视频流已连接: {url}")
    idx = 0
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            ok, frame = await asyncio.to_thread(cap.read)
            if not ok or frame is None:
                logger.warning(f"读流失败, 尝试重连: {url}")
                await asyncio.to_thread(cap.release)
                try:
                    cap = await asyncio.to_thread(_open, url)
                except StreamError:
                    logger.error(f"重连失败, 放弃: {url}")
                    break
                continue
            idx += 1
            yield frame, idx
    finally:
        await asyncio.to_thread(cap.release)
        logger.info(f"视频流已释放: {url} (共 {idx} 帧)")
