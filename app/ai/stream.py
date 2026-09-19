"""RTSP / GB28181 视频流拉取 (异步, 断流自动重连).

读帧与处理解耦: cv2 读帧在独立后台线程持续消费, 处理侧只取最新帧 (处理慢时丢弃
积压帧). 若读帧与处理串行, 消费速率低于源帧率时 FLV socket 会持续积压, 最终被
ZLM 判定 socket send timeout 而周期性断流.
"""
import asyncio
import threading
import time
from typing import Awaitable, Callable, Optional

import cv2
import numpy as np
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..common.logger import logger

# 处理侧取帧轮询间隔 (秒); 仅在无新帧时短暂让出事件循环
_POLL_INTERVAL = 0.005


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


class _FrameReader:
    """后台线程持续 read() 并只保留最新帧, 避免消费慢导致 socket 积压."""

    def __init__(self, cap: cv2.VideoCapture):
        self._cap = cap
        self._lock = threading.Lock()
        self._frame: Optional[np.ndarray] = None
        self._seq = 0
        self._failed = False
        self._stop = False
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        while not self._stop:
            ok, frame = self._cap.read()
            if not ok or frame is None:
                with self._lock:
                    self._failed = True
                return
            with self._lock:
                self._frame = frame
                self._seq += 1

    def snapshot(self) -> tuple:
        """返回 (最新帧序号, 最新帧, 是否读失败)."""
        with self._lock:
            return self._seq, self._frame, self._failed

    def close(self) -> None:
        """停止读取并释放 capture (release 会中断阻塞中的 read)."""
        self._stop = True
        self._cap.release()
        self._thread.join(timeout=5)


async def stream_frames(
    url: str,
    stop_event: Optional[asyncio.Event] = None,
    url_provider: Optional[Callable[[], Awaitable[str]]] = None,
):
    """异步帧生成器. 拉流在线程中持续进行, 处理侧只取最新帧 (允许丢帧).

    产出 (frame, idx, decoded):
      - idx: 已产出(被处理)帧序号, 从 1 递增;
      - decoded: 当前连接内已解码帧数 (读帧线程解出的总帧数). 与 idx 之差即
        "处理慢被覆盖丢弃"的帧数, 供处理侧统计丢帧率定位漏计. 断流重连后
        重新从 0 累计 (新连接).

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
    reader = _FrameReader(cap)
    idx = 0
    last_seq = 0
    refresh_cooldown = 10.0  # 流地址刷新冷却 (秒), 防止频繁打 WVP
    last_refresh = 0.0
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            seq, frame, failed = reader.snapshot()
            if failed:
                logger.warning(f"读流失败, 尝试重连: {current_url}")
                await asyncio.to_thread(reader.close)
                # 1. 先用当前 url 重连 (_open 内含 5 次退避重试)
                try:
                    cap = await asyncio.to_thread(_open, current_url)
                except StreamError:
                    cap = None
                # 2. 当前 url 重连失败, 尝试通过 url_provider 刷新地址
                if cap is None and url_provider is not None:
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
                        except StreamError:
                            cap = None
                if cap is None:
                    logger.error(f"重连失败, 放弃: {current_url}")
                    break
                reader = _FrameReader(cap)
                last_seq = 0
                continue
            if seq == last_seq:
                await asyncio.sleep(_POLL_INTERVAL)
                continue
            last_seq = seq
            idx += 1
            yield frame, idx, seq
    finally:
        await asyncio.to_thread(reader.close)
        logger.info(f"视频流已释放: {current_url} (共 {idx} 帧)")
