"""RTSP / GB28181 视频流拉取 (异步, 断流自动重连).

读帧与处理解耦: 读帧在独立后台线程持续消费, 处理侧只取最新帧 (处理慢时丢弃
积压帧). 若读帧与处理串行, 消费速率低于源帧率时 FLV socket 会持续积压, 最终被
ZLM 判定 socket send timeout 而周期性断流.

两种解码后端 (settings.decode_backend):
  - ffmpeg (默认): 解码与缩放都在 ffmpeg 子进程内完成, Python 只拿到已缩到推理
    尺寸的 bgr24 小帧. 4K 流降分辨率在 ffmpeg 侧完成 (cv2 的
    CAP_PROP_FRAME_WIDTH 对网络流通常无效)，减少输出拷贝和 Python resize；
    滤镜位于解码之后，不能免除原始分辨率的视频解码。
  - opencv: 旧行为 (grab() 节流 + 可能的解码端缩放 + preprocess 钩子),
    保留作兼容/回退, INFER_MODE=legacy 亦强制走此路径.

两种后端对上层暴露相同接口: snapshot() -> (采样帧序号, 最新帧, 是否读失败,
preprocess 耗时ms, grab 总帧数) 与 close(); stream_size 仍是流原始分辨率,
作为计数线/事件/前端叠加的统一坐标基准.
"""
import asyncio
import re
import shutil
import subprocess
import threading
import time
from collections import deque
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

# ffmpeg 首帧等待上限 (秒): 超时/进程退出即判定该路 ffmpeg 不可用, 回退 opencv
_FFMPEG_START_TIMEOUT = 10.0

# ffmpeg 打开失败的流地址 -> 允许重试的时刻 (避免断流重连时反复白等首帧超时)
_ffmpeg_disabled: dict = {}
_FFMPEG_DISABLED_MAX = 256
_FFMPEG_RETRY_INTERVAL = 300.0  # 秒; 到时后重新尝试 ffmpeg (排除瞬时网络抖动)
_ffmpeg_fps_option_cache: dict[str, Optional[tuple[str, str]]] = {}
_ffmpeg_fps_option_failed_at: dict[str, float] = {}
_ffmpeg_fps_option_lock = threading.Lock()
_rtsp_timeout_option_cache: dict[str, tuple[Optional[str], float]] = {}
_rtsp_timeout_option_lock = threading.Lock()



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
    received_mono: Optional[float] = None  # 完整解码帧到达本机的单调时间, 非相机 PTS
    received_wall: Optional[float] = None  # 同一时刻的本机 Unix 时间, 非相机 PTS
    stream_generation: int = 0  # 本次生成器内连接代次
    decode_backend: str = "unknown"  # 实际读帧器，回退时与配置可能不同
    decode_max_fps: Optional[float] = None  # 建连时生效值，不是实际输出帧率


def _bounded_capture(url: str) -> cv2.VideoCapture:
    return cv2.VideoCapture(url, cv2.CAP_FFMPEG, [
        cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 10000,
        cv2.CAP_PROP_READ_TIMEOUT_MSEC, 10000,
    ])


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type(StreamError),
    reraise=True,
)
def _open(url: str) -> cv2.VideoCapture:
    # FFmpeg backend 的超时必须在打开时传入，不能随后 cap.set；避免断网后
    # 读线程永久阻塞。关闭时仅由读线程释放 capture，避免跨线程 release 崩溃。
    cap = _bounded_capture(url)
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
        self._received_mono = None
        self._received_wall = None
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        try:
            self._read_loop()
        finally:
            self._cap.release()

    def _read_loop(self) -> None:
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
            received_mono = time.monotonic()
            received_wall = time.time()
            pre_ms = 0.0
            if self._preprocess is not None:
                t0 = time.monotonic()
                frame = self._preprocess(frame)
                pre_ms = (time.monotonic() - t0) * 1000.0
            with self._lock:
                self._frame = frame
                self._seq += 1
                self._preprocess_ms = pre_ms
                self._received_mono = received_mono
                self._received_wall = received_wall

    def snapshot(self) -> tuple:
        """返回 (采样帧序号, 最新帧, 是否读失败, preprocess 耗时ms, grab 总帧数)."""
        with self._lock:
            return self._seq, self._frame, self._failed, self._preprocess_ms, self._grabbed

    def close(self) -> None:
        """等待读线程自行释放 capture，禁止与 grab/retrieve 并发 release。"""
        self._stop = True
        # 读超时为 10s；给线程留出超时和释放的时间，避免进程退出时仍在调用 OpenCV。
        self._thread.join(timeout=12)
        if self._thread.is_alive():
            logger.warning("OpenCV 读帧线程尚未退出，将在读超时后自行释放连接")

    def snapshot_with_time(self) -> tuple:
        with self._lock:
            return (self._seq, self._frame, self._failed, self._preprocess_ms,
                    self._grabbed, self._received_mono, self._received_wall)


def _decode_max_fps() -> float:
    """解码侧节流帧率 (热重载读取): >0 按此帧率 grab/retrieve 采样, 0=不限速."""
    return float(get_rule("inference", "decode_max_fps", default=settings.decode_max_fps) or 0)


def _decode_target_short_side() -> int:
    """解码端目标短边: decode_max_short_side > 0 用它, 否则跟随推理尺寸 imgsz."""
    target = int(settings.decode_max_short_side or 0)
    if target > 0:
        return target
    return int(get_rule("inference", "imgsz", default=settings.infer_imgsz) or 0)


def _fps_select_filter(max_fps: float, mode: str = "interval") -> str:
    """FFmpeg 内按输入 PTS 选帧，不复制帧；输出管道仍不携带源 PTS。"""
    if mode == "grid":
        # 每个时间桶最多选一帧；与 interval 不同，短时间可突发，须做现场 A/B。
        return f"select='isnan(t)+isnan(prev_selected_t)+lt(t,prev_selected_t)+gt(floor(t*{max_fps:.9f}),floor(prev_selected_t*{max_fps:.9f}))'"
    if mode != "interval":
        raise StreamError(f"不支持的 FFmpeg 采样模式: {mode}")
    interval = 1.0 / max_fps
    return f"select='isnan(t)+isnan(prev_selected_t)+lt(t,prev_selected_t)+gte(t-prev_selected_t,{interval:.9f})'"


def _ffmpeg_passthrough_option(exe: str) -> tuple[str, str]:
    """Probe local FFmpeg once without accessing a camera or logging its URL."""
    with _ffmpeg_fps_option_lock:
        if (_ffmpeg_fps_option_cache.get(exe, True) is None
                and time.monotonic() - _ffmpeg_fps_option_failed_at.get(exe, 0) >= _FFMPEG_RETRY_INTERVAL):
            _ffmpeg_fps_option_cache.pop(exe, None)
        if exe not in _ffmpeg_fps_option_cache:
            selected = None
            for option in (("-fps_mode", "passthrough"), ("-vsync", "0")):
                cmd = [exe, "-nostdin", "-hide_banner", "-loglevel", "error",
                       "-f", "lavfi", "-i", "color=s=32x32:r=2:d=1",
                       "-frames:v", "2", *option, "-f", "null", "-"]
                try:
                    result = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.PIPE, timeout=5, check=False)
                    if result.returncode == 0:
                        selected = option
                        break
                except (OSError, subprocess.TimeoutExpired):
                    continue
            _ffmpeg_fps_option_cache[exe] = selected
            if selected:
                logger.info(f"[ffmpeg] 帧同步参数: {selected[0]} {selected[1]}")
            else:
                _ffmpeg_fps_option_failed_at[exe] = time.monotonic()
                logger.warning("[ffmpeg] 不支持可用的帧同步参数，将回退 OpenCV")
        selected = _ffmpeg_fps_option_cache[exe]
    if selected is None:
        raise StreamError("ffmpeg 帧同步参数探测失败")
    return selected


def _rtsp_timeout_option(exe: str) -> Optional[str]:
    """FFmpeg/ffprobe RTSP demuxer: old builds use stimeout, newer builds timeout."""
    with _rtsp_timeout_option_lock:
        cached = _rtsp_timeout_option_cache.get(exe)
        now = time.monotonic()
        if cached and (cached[0] is not None or now - cached[1] < _FFMPEG_RETRY_INTERVAL):
            return cached[0]
        selected = None
        try:
            result = subprocess.run([exe, '-hide_banner', '-h', 'demuxer=rtsp'],
                                    capture_output=True, timeout=5, check=False)
            if result.returncode == 0:
                output = (result.stdout + result.stderr).decode('utf-8', 'replace')
                options = set(re.findall(r'^\s+-(s?timeout)\s', output, re.MULTILINE))
                selected = '-timeout' if 'timeout' in options else '-stimeout' if 'stimeout' in options else None
        except (OSError, subprocess.TimeoutExpired):
            pass
        _rtsp_timeout_option_cache[exe] = (selected, now)
        if selected is None:
            logger.warning(f'[{exe}] RTSP 超时参数探测失败')
        return selected


class _FFmpegReader:
    """ffmpeg 子进程读帧: 解码/降帧率/降分辨率全在 C 侧完成.

    与 _FrameReader 接口一致 (snapshot/close). 由于抽帧与 scale 都在 -vf 内完成,
    被丢弃的帧在 ffmpeg 内部, Python 侧观察不到, 故 grabbed 恒等于 sampled
    (运行统计里的"节流丢弃"为 0, 真正的丢帧仍由 Δsampled - 已处理帧数 体现).
    """

    def __init__(
        self,
        proc: "subprocess.Popen",
        out_size: tuple,
        preprocess: Optional[Callable[[np.ndarray], np.ndarray]] = None,
    ):
        self._proc = proc
        self._ow, self._oh = out_size
        self._preprocess = preprocess
        self._lock = threading.Lock()
        self._frame: Optional[np.ndarray] = None
        self._seq = 0
        self._failed = False
        self._stop = False
        self._preprocess_ms = 0.0
        self._received_mono = None
        self._received_wall = None
        self._err: deque = deque(maxlen=6)
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain_stderr(self) -> None:
        """持续抽干 stderr: 不读会把管道写满导致 ffmpeg 卡死, 同时留尾部用于报错."""
        try:
            stream = self._proc.stderr
            if stream is None:
                return
            for line in iter(stream.readline, b""):
                text = line.decode("utf-8", "ignore").strip()
                if text:
                    self._err.append(text)
        except Exception:  # noqa: BLE001  (进程被 kill 时读管道会抛)
            pass

    def error_tail(self) -> str:
        return " | ".join(self._err)[:400]

    def _drain(self) -> None:
        nbytes = self._ow * self._oh * 3
        stdout = self._proc.stdout
        if stdout is None:
            with self._lock:
                self._failed = True
            return
        while not self._stop:
            buf = bytearray(nbytes)
            view = memoryview(buf)
            filled = 0
            while filled < nbytes:
                try:
                    n = stdout.readinto(view[filled:])
                except (ValueError, OSError):
                    n = 0
                if not n:
                    with self._lock:
                        if not self._stop:
                            self._failed = True
                    return
                filled += n
            frame = np.frombuffer(buf, dtype=np.uint8).reshape(self._oh, self._ow, 3)
            received_mono = time.monotonic()
            received_wall = time.time()
            pre_ms = 0.0
            if self._preprocess is not None:
                # ffmpeg 已缩到目标短边时该钩子直接返回原帧 (开销 ~0), 仅在
                # imgsz 被热改小的情况下才真正 resize
                t0 = time.monotonic()
                frame = self._preprocess(frame)
                pre_ms = (time.monotonic() - t0) * 1000.0
            with self._lock:
                self._frame = frame
                self._seq += 1
                self._preprocess_ms = pre_ms
                self._received_mono = received_mono
                self._received_wall = received_wall

    def snapshot(self) -> tuple:
        with self._lock:
            return self._seq, self._frame, self._failed, self._preprocess_ms, self._seq

    def snapshot_with_time(self) -> tuple:
        with self._lock:
            return (self._seq, self._frame, self._failed, self._preprocess_ms,
                    self._seq, self._received_mono, self._received_wall)

    def close(self) -> None:
        """先 TERM 后 KILL, 保证不残留 ffmpeg 进程."""
        self._stop = True
        proc = self._proc
        try:
            if proc.poll() is None:
                proc.terminate()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=2)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                pass
        self._thread.join(timeout=5)
        self._stderr_thread.join(timeout=2)
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:  # noqa: BLE001
                pass


def _probe_stream_size(url: str) -> Optional[tuple]:
    """用 ffprobe 取流原始分辨率 (坐标基准); 失败返回 None."""
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    cmd = [
        exe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "csv=p=0",
    ]
    if url.lower().startswith("rtsp://"):
        timeout_option = _rtsp_timeout_option(exe)
        if timeout_option is None:
            return None
        cmd += ["-rtsp_transport", "tcp", timeout_option, "10000000"]
    elif url.lower().startswith(("http://", "https://")):
        cmd += ["-rw_timeout", "10000000"]
    cmd.append(url)
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=20)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"ffprobe 探测分辨率失败 ({url}): {e}")
        return None
    for line in proc.stdout.decode("utf-8", "ignore").splitlines():
        parts = line.replace(" ", "").split(",")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            w, h = int(parts[0]), int(parts[1])
            if w > 0 and h > 0:
                return (w, h)
    return None


def _probe_stream_size_cv2(url: str) -> Optional[tuple]:
    """ffprobe 不可用时的兜底: 用 cv2 读一帧取分辨率 (仅连接建立时一次)."""
    cap = _bounded_capture(url)
    try:
        if not cap.isOpened():
            return None
        ok, frame = cap.read()
        if not ok or frame is None:
            return None
        h, w = frame.shape[:2]
        return (w, h) if w > 0 and h > 0 else None
    finally:
        cap.release()


# 归一化画布: 常见宽高比归类, 使不同分辨率的源缩到同一输出尺寸 (组批前提)
_CANVAS_RATIOS = ((16 / 9, "16:9"), (4 / 3, "4:3"))
_CANVAS_RATIO_TOLERANCE = 0.03  # 相对偏差 ≤3% 归入该类 (4096x2336 偏差 1.4%, 落入 16:9)


def _ffmpeg_out_size(stream_size: tuple) -> tuple:
    """按目标短边计算 ffmpeg 输出尺寸, 常见宽高比归一化到同一画布.

    组批按 frame.shape 分组: 各源即使都缩到"短边 960", 只要宽高比不同仍会得到
    不同形状 (1682x960 / 1706x960 / 1280x720 ...), 组批被拆成多个小组, 提前派发
    永远凑不满. 这里把 16:9 / 4:3 源统一到固定画布 (1706x960 / 1280x960),
    32 路即可落进同一批; 轻微非等比拉伸由 pipeline 逐轴坐标还原兼容.
    """
    w, h = stream_size
    target = _decode_target_short_side()
    if target <= 0 or min(w, h) <= target:
        return (w, h)

    long_side, short_side = (w, h) if w >= h else (h, w)
    ratio = long_side / float(short_side)
    for canvas_ratio, _name in _CANVAS_RATIOS:
        if abs(ratio - canvas_ratio) <= _CANVAS_RATIO_TOLERANCE * canvas_ratio:
            out_long = max(2, int(round(target * canvas_ratio)) // 2 * 2)
            return (out_long, target) if w >= h else (target, out_long)

    # 非标准宽高比: 保持比例, 短边对齐目标 (形状仍可能与其它源不同)
    f = target / float(short_side)
    return (
        max(2, int(round(w * f)) // 2 * 2),
        max(2, int(round(h * f)) // 2 * 2),
    )


def _open_ffmpeg_reader(url: str, preprocess) -> tuple:
    """启动 ffmpeg 解码进程并等到首帧; 失败抛 StreamError (调用方回退 opencv)."""
    exe = shutil.which("ffmpeg")
    if not exe:
        raise StreamError("ffmpeg 不存在")
    passthrough = _ffmpeg_passthrough_option(exe)

    stream_size = _probe_stream_size(url) or _probe_stream_size_cv2(url)
    if stream_size is None:
        raise StreamError(f"无法探测流分辨率: {url}")
    out_size = _ffmpeg_out_size(stream_size)
    max_fps = _decode_max_fps()

    vf = []
    if max_fps and max_fps > 0:
        # select 只舍弃输入帧，避免 fps 滤镜在低速视频中复制帧、虚报处理率。
        vf.append(_fps_select_filter(max_fps, settings.decode_fps_sampling))
    if out_size != stream_size:
        vf.append(f"scale={out_size[0]}:{out_size[1]}:flags=bicubic")

    cmd = [
        exe, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-fflags", "nobuffer", "-flags", "low_delay",
        "-probesize", "1000000", "-analyzeduration", "1000000",
    ]
    if url.lower().startswith("rtsp://"):
        timeout_option = _rtsp_timeout_option(exe)
        if timeout_option is None:
            raise StreamError('ffmpeg 不支持可用的 RTSP 超时参数')
        cmd += ["-rtsp_transport", "tcp", timeout_option, "10000000"]
    elif url.lower().startswith(("http://", "https://")):
        cmd += ["-rw_timeout", "10000000"]
    cmd += ["-i", url, "-an"]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    cmd += [*passthrough, "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
        )
    except Exception as e:  # noqa: BLE001
        raise StreamError(f"启动 ffmpeg 失败: {e}") from e

    reader = _FFmpegReader(proc, out_size, preprocess)
    reader.decode_max_fps = max_fps
    deadline = time.monotonic() + _FFMPEG_START_TIMEOUT
    while time.monotonic() < deadline:
        seq, frame, failed, _pre, _g = reader.snapshot()
        if seq > 0 and frame is not None:
            src_ratio = stream_size[0] / float(stream_size[1])
            out_ratio = out_size[0] / float(out_size[1])
            stretch = ""
            if abs(out_ratio - src_ratio) / src_ratio > 0.005:
                stretch = f" (非等比拉伸 {src_ratio:.3f}->{out_ratio:.3f})"
            logger.info(
                f"[ffmpeg] 解码就绪: {stream_size[0]}x{stream_size[1]} -> "
                f"{out_size[0]}x{out_size[1]} @{max_fps:g}fps{stretch} ({url})"
            )
            return reader, stream_size
        if failed:
            break
        time.sleep(0.05)

    tail = reader.error_tail()
    reader.close()
    raise StreamError(f"ffmpeg 首帧失败{(': ' + tail) if tail else ''}")


def _open_reader(url: str, decode_tuning: bool, preprocess) -> tuple:
    """按配置打开读帧器; 返回 (reader, 流原始分辨率).

    ffmpeg 打开失败时该 url 在冷却期内直接走 opencv, 避免断流重连反复白等首帧超时;
    冷却结束会重新尝试 ffmpeg (瞬时网络抖动不应把某一路永久钉死在旧后端).
    """
    backend = (settings.decode_backend or "ffmpeg").strip().lower()
    now = time.monotonic()
    if (
        decode_tuning
        and backend == "ffmpeg"
        and now >= _ffmpeg_disabled.get(url, 0.0)
    ):
        try:
            return _open_ffmpeg_reader(url, preprocess)
        except Exception as e:  # noqa: BLE001
            if len(_ffmpeg_disabled) >= _FFMPEG_DISABLED_MAX:
                _ffmpeg_disabled.clear()
            _ffmpeg_disabled[url] = now + _FFMPEG_RETRY_INTERVAL
            logger.warning(
                f"ffmpeg 解码不可用, 该路回退 opencv ({url}), "
                f"{_FFMPEG_RETRY_INTERVAL:.0f}s 后重试: {e}"
            )

    cap = _open(url)
    try:
        stream_size = _tune_decode(cap, url, decode_tuning)
    except StreamError:
        cap.release()
        raise
    max_fps = _decode_max_fps() if decode_tuning else 0.0
    reader = _FrameReader(cap, max_fps=max_fps, preprocess=preprocess)
    reader.decode_max_fps = max_fps
    return reader, stream_size


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
      ffmpeg 后端已按目标短边输出小帧, 该钩子通常直接返回原帧 (开销 ~0)。

    decode_tuning: 是否启用解码侧采样/缩放 (批量模式 gpu_batch/cpu_batch 启用).
      为 False 时 (legacy 回退) 强制走 opencv 且不下发解码端帧率/分辨率调整,
      行为与优化前一致 —— 一键回滚 INFER_MODE=legacy 即为旧行为.

    断流重连: 先重开同一 url (_open 内含 tenacity 5 次退避重试); 仍失败且提供了
    url_provider (WVP 流地址刷新回调) 时, 调用其获取新地址再重连. url_provider=None
    时行为与离线处理一致 (重连 5 次失败即放弃).
    """
    current_url = url
    try:
        reader, stream_size = await asyncio.to_thread(
            _open_reader, current_url, decode_tuning, preprocess
        )
    except StreamError as e:
        logger.error(f"视频流打开失败: {e}")
        return
    logger.info(f"视频流已连接: {current_url} ({stream_size[0]}x{stream_size[1]})")

    idx = 0
    last_seq = 0
    generation = 0
    last_progress = time.monotonic()
    stall_timeout = max(0.0, float(settings.stream_stall_timeout_s))
    refresh_cooldown = 10.0  # 流地址刷新冷却 (秒), 防止频繁打 WVP
    last_refresh = 0.0
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            seq, frame, failed, pre_ms, grabbed, received_mono, received_wall = reader.snapshot_with_time()
            if failed and seq != last_seq and frame is not None:
                # EOF/断流前最后一帧可能已经完整读到，先交付它，再重连。
                last_seq = seq
                idx += 1
                yield FramePacket(frame=frame, idx=idx, sampled=seq, grabbed=grabbed,
                                  stream_size=stream_size, preprocess_ms=pre_ms,
                                  received_mono=received_mono, received_wall=received_wall,
                                  stream_generation=generation,
                                  decode_backend="ffmpeg" if isinstance(reader, _FFmpegReader) else "opencv",
                                  decode_max_fps=getattr(reader, "decode_max_fps", None))
                continue
            if seq != last_seq:
                last_progress = time.monotonic()
            elif stall_timeout and time.monotonic() - last_progress >= stall_timeout:
                logger.warning(f"视频流连续 {stall_timeout:g}s 无新帧, 尝试重连: {current_url}")
                failed = True
            if failed:
                logger.warning(f"读流失败, 尝试重连: {current_url}")
                await asyncio.to_thread(reader.close)
                # 1. 先用当前 url 重连 (_open 内含 5 次退避重试)
                reader = None
                try:
                    reader, stream_size = await asyncio.to_thread(
                        _open_reader, current_url, decode_tuning, preprocess
                    )
                except StreamError:
                    reader = None
                # 2. 当前 url 重连失败, 尝试通过 url_provider 刷新地址
                if reader is None and url_provider is not None:
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
                            reader, stream_size = await asyncio.to_thread(
                                _open_reader, current_url, decode_tuning, preprocess
                            )
                        except StreamError:
                            reader = None
                if reader is None:
                    logger.error(f"重连失败, 放弃: {current_url}")
                    break
                logger.info(
                    f"视频流已重连: {current_url} "
                    f"({stream_size[0]}x{stream_size[1]})"
                )
                last_seq = 0
                generation += 1
                last_progress = time.monotonic()
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
                received_mono=received_mono,
                received_wall=received_wall,
                stream_generation=generation,
                decode_backend="ffmpeg" if isinstance(reader, _FFmpegReader) else "opencv",
                decode_max_fps=getattr(reader, "decode_max_fps", None),
            )
    finally:
        if reader is not None:
            await asyncio.to_thread(reader.close)
        logger.info(f"视频流已释放: {current_url} (共 {idx} 帧)")

