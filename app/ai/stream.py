"""RTSP / GB28181 视频流拉取 (异步, 断流自动重连)."""
import asyncio
import time
from typing import Awaitable, Callable, Optional

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


async def stream_frames(
    url: str,
    stop_event: Optional[asyncio.Event] = None,
    url_provider: Optional[Callable[[], Awaitable[str]]] = None,
):
    """异步帧生成器. cv2 同步读帧用 asyncio.to_thread 包装, 不阻塞事件循环.

    断流重连: 先重开同一 url (_open 内含 tenacity 5 次退避重试); 仍失败且提供了
    url_provider (WVP 流地址刷新回调) 时, 调用其获取新地址再重连. url_provider=None
    时行为与离线处理一致 (重连 5 次失败即放弃).
    """
    current_url = url
    try:
        cap = await asyncio.to_thread(_open, current_url)
    except StreamError as e:
        logger.error(f"视频流打开失败: {e}")
        return

    logger.info(f"视频流已连接: {current_url}")
    idx = 0
    refresh_cooldown = 10.0  # 流地址刷新冷却 (秒), 防止频繁打 WVP
    last_refresh = 0.0
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            ok, frame = await asyncio.to_thread(cap.read)
            if not ok or frame is None:
                logger.warning(f"读流失败, 尝试重连: {current_url}")
                await asyncio.to_thread(cap.release)
                # 1. 先用当前 url 重连 (_open 内含 5 次退避重试)
                try:
                    cap = await asyncio.to_thread(_open, current_url)
                    continue
                except StreamError:
                    pass
                # 2. 当前 url 重连失败, 尝试通过 url_provider 刷新地址
                if url_provider is not None:
                    now = time.monotonic()
                    if now - last_refresh < refresh_cooldown:
                        await asyncio.sleep(refresh_cooldown - (now - last_refresh))
                    try:
                        new_url = await url_provider()
                    except Exception as e:  # noqa: BLE001
                        logger.error(f"流地址刷新失败, 放弃: {e}")
                        break
                    if new_url and new_url != current_url:
                        logger.info(f"流地址刷新: {current_url} -> {new_url}")
                        current_url = new_url
                        last_refresh = time.monotonic()
                        try:
                            cap = await asyncio.to_thread(_open, current_url)
                            continue
                        except StreamError:
                            pass
                logger.error(f"重连失败, 放弃: {current_url}")
                break
            idx += 1
            yield frame, idx
    finally:
        await asyncio.to_thread(cap.release)
        logger.info(f"视频流已释放: {current_url} (共 {idx} 帧)")
