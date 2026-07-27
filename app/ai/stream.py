"""RTSP / GB28181 视频流拉取 (异步, 断流自动重连, 本地文件循环播放)."""
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


def _is_file_source(url: str) -> bool:
    """判断是否为本地文件源 (非网络流). 文件源播完后循环重放, 模拟持续摄像头信号."""
    return not url.lower().startswith(
        ("rtsp://", "rtsps://", "http://", "https://", "rtmp://")
    )


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
    """异步帧生成器. cv2 同步读帧用 asyncio.to_thread 包装, 不阻塞事件循环.

    本地文件源播放结束后自动循环重放; 网络流断开后自动重连.
    """
    file_source = _is_file_source(url)
    try:
        cap = await asyncio.to_thread(_open, url)
    except StreamError as e:
        logger.error(f"视频流打开失败: {e}")
        return

    logger.info(f"视频流已连接: {url}{' (文件源, 循环播放)' if file_source else ''}")
    idx = 0
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            ok, frame = await asyncio.to_thread(cap.read)
            if not ok or frame is None:
                if file_source:
                    # 本地视频播放结束, 回到首帧循环重放
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
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
