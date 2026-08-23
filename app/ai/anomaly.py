"""视频异常识别: 黑屏 (black_screen) + 花屏 (flower_screen).

设计要点:
- AnomalyDetector: 单帧纯检测, 返回异常类型 + 各信号分数. 持前一帧灰度用于时域分析.
- AnomalyMonitor: 周期采样 + 连续确认去抖 + 状态机, 仅在状态转移时产出 onset/recovery 事件.
  供 DevicePipeline (在线) 与 video_processor (离线) 复用, 行为一致.
- 纯 numpy/opencv, 无新依赖. 阈值集中在 AnomalyConfig (默认从 settings 读取, 可覆盖).

算法:
- 黑屏: 灰度均值 < brightness 且 近黑像素占比 > ratio (双条件, 避免夜间低光误报).
- 花屏: 空间噪声高 AND 噪声均匀 (1-CV) AND (通道去相关 OR 时域差分高).
  多信号复合 AND, 单信号易误报, 复合后鲁棒 (彩色雪花→去相关; 块状损坏→时域抖动).
"""
from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np

BLACK_SCREEN = "black_screen"
FLOWER_SCREEN = "flower_screen"


@dataclass
class AnomalyConfig:
    """异常检测参数 (单一来源: from_settings 从配置读取, 测试可直接构造覆盖)."""

    analysis_width: int
    black_screen_brightness: int
    black_screen_ratio: float
    black_pixel_value: int
    flower_block_grid: int
    flower_noise_std: float
    flower_uniformity: float
    flower_channel_corr: float
    flower_temporal_diff: float
    check_interval: int
    confirm_frames: int

    @classmethod
    def from_settings(cls) -> "AnomalyConfig":
        from ..common.config import settings  # 延迟导入, 避免模块级依赖 pydantic
        s = settings
        return cls(
            analysis_width=s.anomaly_analysis_width,
            black_screen_brightness=s.black_screen_brightness,
            black_screen_ratio=s.black_screen_ratio,
            black_pixel_value=s.black_pixel_value,
            flower_block_grid=s.flower_block_grid,
            flower_noise_std=s.flower_noise_std,
            flower_uniformity=s.flower_uniformity,
            flower_channel_corr=s.flower_channel_corr,
            flower_temporal_diff=s.flower_temporal_diff,
            check_interval=s.anomaly_check_interval,
            confirm_frames=s.anomaly_confirm_frames,
        )

    def reload(self) -> "AnomalyConfig":
        """热重载: 从 business_rules.yaml 刷新参数 (修改后无需重启, 未配置回落默认)."""
        from ..common.business_rules import get_rule
        from ..common.config import settings

        s = settings
        self.analysis_width = int(get_rule("anomaly", "analysis_width", default=s.anomaly_analysis_width))
        self.black_screen_brightness = int(get_rule("anomaly", "black_screen_brightness", default=s.black_screen_brightness))
        self.black_screen_ratio = float(get_rule("anomaly", "black_screen_ratio", default=s.black_screen_ratio))
        self.black_pixel_value = int(get_rule("anomaly", "black_pixel_value", default=s.black_pixel_value))
        self.flower_block_grid = int(get_rule("anomaly", "flower_block_grid", default=s.flower_block_grid))
        self.flower_noise_std = float(get_rule("anomaly", "flower_noise_std", default=s.flower_noise_std))
        self.flower_uniformity = float(get_rule("anomaly", "flower_uniformity", default=s.flower_uniformity))
        self.flower_channel_corr = float(get_rule("anomaly", "flower_channel_corr", default=s.flower_channel_corr))
        self.flower_temporal_diff = float(get_rule("anomaly", "flower_temporal_diff", default=s.flower_temporal_diff))
        self.check_interval = int(get_rule("anomaly", "check_interval", default=s.anomaly_check_interval))
        self.confirm_frames = int(get_rule("anomaly", "confirm_frames", default=s.anomaly_confirm_frames))
        return self


@dataclass
class AnomalyResult:
    """单帧检测结果."""

    anomaly_type: Optional[str]  # BLACK_SCREEN / FLOWER_SCREEN / None
    scores: dict


@dataclass
class AnomalyEvent:
    """状态转移事件 (仅 onset/recovery 时产出)."""

    anomaly_type: str
    phase: str  # "onset" | "recovery"
    scores: dict
    timestamp: Optional[str] = None


class AnomalyDetector:
    """单帧异常检测 (黑屏 + 花屏)."""

    def __init__(self, config: Optional[AnomalyConfig] = None):
        self.cfg = config or AnomalyConfig.from_settings()
        self._prev_gray: Optional[np.ndarray] = None

    def detect(self, frame: np.ndarray) -> AnomalyResult:
        if frame is None or frame.size == 0:
            return AnomalyResult(None, {})

        # 预处理: 等比缩放到分析宽度 (INTER_NEAREST 保留噪声, 避免 INTER_AREA 平均化削弱花屏)
        small = self._resize(frame)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

        scores: dict = {}
        # ---- 黑屏 (双条件 AND) ----
        brightness = float(gray.mean())
        black_ratio = float((gray < self.cfg.black_pixel_value).mean())
        scores["brightness"] = round(brightness, 2)
        scores["black_ratio"] = round(black_ratio, 4)
        if (
            brightness < self.cfg.black_screen_brightness
            and black_ratio > self.cfg.black_screen_ratio
        ):
            self._prev_gray = gray
            return AnomalyResult(BLACK_SCREEN, scores)

        # ---- 花屏 (多信号复合) ----
        spatial_noise, uniformity = self._spatial(gray)
        channel_corr = self._channel_corr(small)
        temporal_diff = self._temporal(gray)
        scores["spatial_noise"] = round(spatial_noise, 2)
        scores["uniformity"] = round(uniformity, 4)
        scores["channel_corr"] = round(channel_corr, 4)
        scores["temporal_diff"] = round(temporal_diff, 2) if temporal_diff is not None else None

        self._prev_gray = gray  # 更新前帧 (在计算完时域差分之后)

        is_flower = (
            spatial_noise > self.cfg.flower_noise_std
            and uniformity > self.cfg.flower_uniformity
        )
        if temporal_diff is not None:
            # 有前帧: 去相关 OR 时域高噪 (覆盖彩色雪花与块状损坏两种子类)
            is_flower = is_flower and (
                channel_corr < self.cfg.flower_channel_corr
                or temporal_diff > self.cfg.flower_temporal_diff
            )
        else:
            # 无前帧: 仅靠去相关
            is_flower = is_flower and (channel_corr < self.cfg.flower_channel_corr)

        if is_flower:
            return AnomalyResult(FLOWER_SCREEN, scores)
        return AnomalyResult(None, scores)

    def _resize(self, frame: np.ndarray) -> np.ndarray:
        h, w = frame.shape[:2]
        target_w = self.cfg.analysis_width
        if w <= target_w:
            return frame  # 已小于分析宽度, 不放大
        scale = target_w / w
        target_h = max(1, int(round(h * scale)))
        return cv2.resize(frame, (target_w, target_h), interpolation=cv2.INTER_NEAREST)

    def _spatial(self, gray: np.ndarray) -> tuple[float, float]:
        """空间噪声: NxN 块均标准差 + 噪声均匀度 (1-CV)."""
        g = self.cfg.flower_block_grid
        h, w = gray.shape
        bh, bw = h // g, w // g
        if bh < 1 or bw < 1:
            return 0.0, 0.0
        crop = gray[: bh * g, : bw * g].astype(np.float32)
        # (g, bh, g, bw) -> (g, g, bh, bw) -> (g*g, bh*bw) 每行一个块
        blocks = crop.reshape(g, bh, g, bw).swapaxes(1, 2).reshape(g * g, bh * bw)
        stds = blocks.std(axis=1)
        mean_std = float(stds.mean()) if stds.size else 0.0
        if mean_std < 1e-6:
            return mean_std, 0.0
        cv = float(stds.std() / mean_std)  # 变异系数
        uniformity = max(0.0, 1.0 - cv)
        return mean_std, uniformity

    def _channel_corr(self, small: np.ndarray) -> float:
        """通道相关性: R-G 与 G-B 相关系数均值的绝对值. 花屏随机色→低相关."""
        b = small[:, :, 0].astype(np.float32).ravel()
        g = small[:, :, 1].astype(np.float32).ravel()
        r = small[:, :, 2].astype(np.float32).ravel()

        def _corr(x: np.ndarray, y: np.ndarray) -> float:
            xm = x - x.mean()
            ym = y - y.mean()
            denom = float(np.sqrt(float(np.dot(xm, xm)) * float(np.dot(ym, ym))))
            if denom < 1e-6:
                return 1.0  # 单色帧视为高相关 (不判花屏), 避免除零
            return float(np.dot(xm, ym) / denom)

        return (abs(_corr(r, g)) + abs(_corr(g, b))) / 2.0

    def _temporal(self, gray: np.ndarray) -> Optional[float]:
        """时域差分: 与前一帧灰度均绝对差. 无前帧/分辨率变化时返回 None."""
        if self._prev_gray is None or self._prev_gray.shape != gray.shape:
            return None
        return float(cv2.absdiff(gray, self._prev_gray).mean())


class AnomalyMonitor:
    """异常监测器: 周期采样 + 连续确认去抖 + 状态机 (normal ⇄ 异常).

    仅在状态转移时产出 AnomalyEvent (onset/recovery), 避免持续告警刷屏.
    """

    def __init__(self, config: Optional[AnomalyConfig] = None):
        self.cfg = config or AnomalyConfig.from_settings()
        self._detector = AnomalyDetector(self.cfg)
        self._frame_counter = 0
        self._consecutive = 0
        self._state: Optional[str] = None  # None = 正常

    @property
    def state(self) -> Optional[str]:
        """当前异常状态 (供离线标注叠加使用)."""
        return self._state

    def check(self, frame: np.ndarray) -> Optional[AnomalyEvent]:
        """按 interval 采样检测; 返回状态转移事件或 None."""
        # 热重载业务规则 (每帧检查 mtime, 变化才生效)
        self.cfg.reload()
        self._frame_counter += 1
        if self._frame_counter % self.cfg.check_interval != 0:
            return None

        result = self._detector.detect(frame)
        atype = result.anomaly_type

        if atype is not None:
            self._consecutive += 1
            if self._consecutive >= self.cfg.confirm_frames and self._state != atype:
                # 状态转移: 正常->异常 或 异常类型切换 (类型切换直接发新 onset, 简化处理)
                self._state = atype
                return AnomalyEvent(atype, "onset", result.scores)
        else:
            self._consecutive = 0
            if self._state is not None:
                old = self._state
                self._state = None
                return AnomalyEvent(old, "recovery", result.scores)
        return None
