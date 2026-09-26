"""RTSP / GB28181 视频流拉取 (异步, 断流自动重连).

读帧与处理解耦: cv2 读帧在独立后台线程持续消费, 处理侧只取最新帧 (处理慢时丢弃
积压帧). 若读帧与处理串行, 消费速率低于源帧率时 FLV socket 会持续积压, 最终被
ZLM 判定 socket send timeout 而周期性断流.

GPU 场景的三点优化 (解决"4K 流推理前缺少采样"与"无效帧被白白解码"的问题):
  1. 帧率节流: grab() 只解码头不做 yuv→BGR 转换, 仅按 decode_max_fps 采样时刻
     retrieve(); 实测 4K 流颜色转换开销降约 65%, 且仍持续 grab 不积压 socket.
  2. 解码端降分辨率: 首帧拿流原始分辨率 (全链路坐标基准) 后, 请求解码器按比例
     把长边缩到 decode_max_short_side, 减少后续每帧的转换/拷贝开销; 未生效自动
     回退 (仍由 preprocess 在 CPU 侧缩放, 不影响正确性).
  3. preprocess 钩子: 在 reader 线程内完成"推理前 resize", CPU 开销分散到 32 路
     各自的读帧线程, 不占用事件循环线程、也不占用单卡推理线程.
"""
import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import cv2
import numpy as np
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..common.business_rules import get_rule
from ..common.config import settings
from ..common.logger import logger

# 处理侧取帧轮询间隔 (秒); 仅在无新帧时短暂让出事件循环
_POLL_INTERVAL = 0.005


class StreamError(Exception):
    pass


@dataclass
class FramePacket:
    """一帧及其坐标基准信息 (取代裸 frame 元组)."""

    frame: np.ndarray          # 已完成推理前 resize (若配置了 preprocess)
    idx: int                   # 已产出(被处理)帧序号
    sampled: int               # 本连接内累计采样帧数 (retrieve 成功后产出, 含 preprocess)
    grabbed: int               # 本连接内累计 grab 帧数 (含被节流丢弃、未 retrieve 的帧)
    stream_size: tuple         # 流原始分辨率 (w, h): 全链路坐标基准
    preprocess_ms: float = 0.0  # 本帧 preprocess (CPU resize) 耗时


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


def _read_size(cap: cv2.VideoCapture) -> Optional[tuple]:
    ok, frame = cap.read()
    if not ok or frame is None:
        return None
    h, w = frame.shape[:2]
    return (w, h)


def _tune_decode(cap: cv2.VideoCapture, url: str, allow_scale: bool = True) -> tuple:
    """按配置请求解码端降分辨率; 返回 stream_size=(w, h).

    stream_size 取首帧的原始分辨率 —— 它是计数器/事件/前端全链路的坐标基准,
    即使解码端做了缩放也不改变该基准: 逐轴还原系数由处理侧按"实际帧尺寸 -> 该基准"
    现算 (见 pipeline._scale_to_stream), 否则前端按原始分辨率视频叠加轨迹框会错位.

    allow_scale=False (legacy 模式) 时只探测分辨率, 不请求缩放: legacy 的坐标还原
    发生在跟踪器内部, 若这里缩放会让轨迹历史/速度阈值与计数器基准错位.
    """
    ok, frame = cap.read()
    if not ok or frame is None:
        raise StreamError(f"首帧读取失败: {url}")
    h, w = frame.shape[:2]
    stream_size = (w, h)

    # decode_max_short_side 是结构性参数 (config.py / .env), 非热重载项:
    # 只在连接建立时读一次, 且 business_rules.yaml 的 inference 段不承载它
    target = int(settings.decode_max_short_side or 0)
    if not allow_scale:
        target = 0
    if target <= 0 or min(w, h) <= target:
        return stream_size

    f = target / float(min(w, h))
    # 按比例同时设置宽高: 只设一边会被解码器按原宽高比拉伸, 导致坐标失真
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(round(w * f)))
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(round(h * f)))
    got = _read_size(cap)
    if got is not None and got != (w, h):
        logger.info(f"解码端缩放已生效: {w}x{h} -> {got[0]}x{got[1]}")
        return stream_size

    # 未生效 (源不支持) 或读帧失败: 还原原始分辨率, 保证坐标基准不变
    logger.info(f"解码端缩放未生效, 回退原始分辨率 {w}x{h}")
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
    got = _read_size(cap)
    if got is None:
        raise StreamError(f"降分辨率回退后读帧失败: {url}")
    if got != (w, h):
        logger.warning(
            f"解码器未恢复原始分辨率 ({got[0]}x{got[1]}, 期望 {w}x{h}), 按实际帧逐轴还原"
        )
    return stream_size


class _FrameReader:
    """后台线程持续 grab/retrieve 并只保留最新帧, 避免消费慢导致 socket 积压.

    节流策略: 始终 grab() (解码头, 不产生 socket 积压), 仅当距上次采样超过
    1/decode_max_fps 时才 retrieve() (yuv→BGR + 可选 preprocess).
    grabbed 记录 grab 总次数, sampled(=_seq) 记录采样次数: 两者之差即被节流丢弃的帧.
    """

    def __init__(
        self,
        cap: cv2.VideoCapture,
        max_fps: float = 0.0,
        preprocess: Optional[Callable[[np.ndarray], np.ndarray]] = None,
    ):
        self._cap = cap
        self._interval = 1.0 / max_fps if max_fps and max_fps > 0 else 0.0
        self._preprocess = preprocess
        self._lock = threading.Lock()
        self._frame: Optional[np.ndarray] = None
        self._seq = 0
        self._grabbed = 0
        self._failed = False
        self._stop = False
        self._preprocess_ms = 0.0
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        next_keep = 0.0
        while not self._stop:
            if not self._cap.grab():  # 只解码头: 失败即认为断流
                with self._lock:
                    self._failed = True
                return
            with self._lock:
                self._grabbed += 1
            if self._interval:
                now = time.monotonic()
                if now < next_keep:
                    continue  # 未到采样时刻: 本帧丢弃, 不做颜色转换
                next_keep = now + self._interval
            ok, frame = self._cap.retrieve()
            if not ok or frame is None:
                with self._lock:
                    self._failed = True
                return
            pre_ms = 0.0
            if self._preprocess is not None:
                t0 = time.monotonic()
                frame = self._preprocess(frame)
                pre_ms = (time.monotonic() - t0) * 1000.0
            with self._lock:
                self._frame = frame
                self._seq += 1
                self._preprocess_ms = pre_ms

    def snapshot(self) -> tuple:
        """返回 (采样帧序号, 最新帧, 是否读失败, preprocess 耗时ms, grab 总帧数)."""
        with self._lock:
            return self._seq, self._frame, self._failed, self._preprocess_ms, self._grabbed

    def close(self) -> None:
        """停止读取并释放 capture (release 会中断阻塞中的 read)."""
        self._stop = True
        self._cap.release()
        self._thread.join(timeout=5)


def _decode_max_fps() -> float:
    """解码侧节流帧率 (热重载读取): >0 按此帧率 grab/retrieve 采样, 0=不限速."""
    return float(get_rule("inference", "decode_max_fps", default=settings.decode_max_fps) or 0)


async def stream_frames(
    url: str,
    stop_event: Optional[asyncio.Event] = None,
    url_provider: Optional[Callable[[], Awaitable[str]]] = None,
    preprocess: Optional[Callable[[np.ndarray], np.ndarray]] = None,
    decode_tuning: bool = True,
):
    """异步帧生成器. 拉流在线程中持续进行, 处理侧只取最新帧 (允许丢帧).

    产出 FramePacket:
      - idx: 已产出(被处理)帧序号, 从 1 递增;
      - sampled: 当前连接内累计采样帧数 (retrieve 成功并完成 preprocess 的帧);
      - grabbed: 当前连接内累计 grab 帧数 (含被节流丢弃、未 retrieve 的帧).
        grabbed/sampled 都是"当前连接内累计值", 断流重连后都从 0 重算; 处理侧统计
        必须取两者的窗口增量: 节流丢弃 = Δgrabbed - Δsampled (设计内), 真丢帧 =
        Δsampled - 已处理帧数 (idx 是跨连接的产出计数, 不能与二者直接相减);
      - stream_size: 流原始分辨率, 全链路坐标基准。

    preprocess: 在 reader 线程内对每帧执行 (例如 resize 到推理尺寸), 返回新帧。

    decode_tuning: 是否启用解码侧采样/缩放 (gpu_batch 模式启用).
      为 False 时 (legacy 回退/CPU 部署) 完全不下发解码端帧率与分辨率调整,
      行为与优化前一致 —— 一键回滚 INFER_MODE=legacy 即为旧行为.

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

    try:
        stream_size = await asyncio.to_thread(_tune_decode, cap, current_url, decode_tuning)
    except StreamError as e:
        logger.error(f"视频流初始化失败: {e}")
        await asyncio.to_thread(cap.release)
        return
    logger.info(f"视频流已连接: {current_url} ({stream_size[0]}x{stream_size[1]})")

    # 热重载: 每次(重)连接建立时读取节流帧率, 改动后重连即生效
    max_fps = _decode_max_fps() if decode_tuning else 0.0
    reader = _FrameReader(cap, max_fps=max_fps, preprocess=preprocess)
    idx = 0
    last_seq = 0
    refresh_cooldown = 10.0  # 流地址刷新冷却 (秒), 防止频繁打 WVP
    last_refresh = 0.0
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            seq, frame, failed, pre_ms, grabbed = reader.snapshot()
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
                        # 冷却等待可被 stop_event 提前打断 (删除设备时立即退出)
                        remaining = refresh_cooldown - (now - last_refresh)
                        if stop_event is not None:
                            try:
                                await asyncio.wait_for(stop_event.wait(), timeout=remaining)
                                break  # stop 触发, 正常退出
                            except asyncio.TimeoutError:
                                pass
                        else:
                            await asyncio.sleep(remaining)
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
                try:
                    stream_size = await asyncio.to_thread(
                        _tune_decode, cap, current_url, decode_tuning
                    )
                except StreamError as e:
                    logger.error(f"重连后流初始化失败, 放弃: {e}")
                    await asyncio.to_thread(cap.release)
                    break
                # 重读节流帧率: 改配置后重连即生效
                max_fps = _decode_max_fps() if decode_tuning else 0.0
                reader = _FrameReader(cap, max_fps=max_fps, preprocess=preprocess)
                last_seq = 0
                continue
            if seq == last_seq:
                await asyncio.sleep(_POLL_INTERVAL)
                continue
            last_seq = seq
            idx += 1
            yield FramePacket(
                frame=frame,
                idx=idx,
                sampled=seq,
                grabbed=grabbed,
                stream_size=stream_size,
                preprocess_ms=pre_ms,
            )
    finally:
        await asyncio.to_thread(reader.close)
        logger.info(f"视频流已释放: {current_url} (共 {idx} 帧)")

