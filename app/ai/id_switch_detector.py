"""ID 切换检测器：过滤 ByteTrack ID 切换导致的误判."""
import math
from typing import List

from .geometry_engine import GeometryEngine


class IDSwitchDetector:
    """ID 切换检测器.

    负责检测因 ByteTrack ID 切换导致的轨迹突变，包括：
    - 速度突变检测：当前帧位移远超历史平均速度 (阈值随 bbox 尺度自适应)
    - 方向连续性佐证：真实高速目标运动方向与历史一致 (透视下加速亦同向);
      ID 切换后新目标运动方向通常与原轨迹明显偏转, 二者同时满足才判定切换
    - 方向一致性验证：跨线方向应与最近运动方向一致
    """

    def __init__(
        self,
        geometry: GeometryEngine,
        speed_ratio: float = 3.0,
        min_pixel: float = 30.0,
        min_avg_speed: float = 5.0,
        history_window: int = 5,
        min_pixel_scale_ratio: float = 0.3,
        direction_cos_threshold: float = 0.5,
    ):
        self.geometry = geometry
        self.speed_ratio = speed_ratio  # 速度超过历史平均 N 倍视为 ID 切换
        self.min_pixel = min_pixel  # 速度差距至少 N 像素
        self.min_avg_speed = min_avg_speed  # 历史平均速度低于此值不判 (静止误判)
        self.history_window = history_window  # 方向一致性验证的历史窗口
        # 尺度自适应: 速度差阈值下限 = bbox 最大边 × 该系数 (近景大目标位移天然更大)
        self.min_pixel_scale_ratio = min_pixel_scale_ratio
        # 方向连续性: 最近两段运动向量余弦相似度 >= 该值视为方向连续 (非 ID 切换)
        self.direction_cos_threshold = direction_cos_threshold

        # 计数线参数 (由 LineCrossingCounter 通过 set_line_params 设置)
        self.line_start: List[float] = [0.0, 0.0]
        self.normal: List[float] = [0.0, 0.0]

    def set_line_params(
        self,
        line_start: List[float],
        normal: List[float],
    ):
        """设置计数线参数 (由 LineCrossingCounter 在预计算时调用)."""
        self.line_start = line_start
        self.normal = normal

    def update_config(
        self,
        speed_ratio: float,
        min_pixel: float,
        min_avg_speed: float,
        history_window: int = None,
    ):
        """热重载更新 ID 切换检测参数 (业务规则热重载时调用)."""
        self.speed_ratio = speed_ratio
        self.min_pixel = min_pixel
        self.min_avg_speed = min_avg_speed
        if history_window is not None:
            self.history_window = history_window

    def detect_speed_anomaly(self, track) -> bool:
        """检测速度突变 (ID 切换标志).

        ID 切换时 prev_point 与 curr_point 来自不同目标, 位移远超历史速度.
        判定需同时满足:
          1. 速度突变: 超过历史平均 N 倍, 且差距超过 bbox 尺度自适应阈值
          2. 方向不连续: 当前运动方向与上一段运动方向明显偏转
             (真实高速目标——近景车辆加速/透视放大——方向连续, 不误杀)

        Returns:
            True 表示检测到 ID 切换, 应清除跨线状态
        """
        if len(track.history) < 4:
            return False

        h = track.history
        hist_speeds = [
            math.hypot(h[i][0] - h[i - 1][0], h[i][1] - h[i - 1][1])
            for i in range(1, len(h) - 1)
        ]
        if not hist_speeds:
            return False

        avg_speed = sum(hist_speeds) / len(hist_speeds)
        curr_speed = math.hypot(
            h[-1][0] - h[-2][0],
            h[-1][1] - h[-2][1],
        )

        # 速度突变: 超过历史平均 N 倍且差距足够 (阈值随 bbox 尺度自适应)
        min_pixel = self._adaptive_min_pixel(track)
        speed_anomaly = (
            avg_speed > self.min_avg_speed
            and curr_speed > avg_speed * self.speed_ratio
            and (curr_speed - avg_speed) > min_pixel
        )
        if not speed_anomaly:
            return False

        # 方向连续性佐证: 方向连续的真实加速目标不是 ID 切换
        if self._motion_direction_continuous(h):
            return False

        return True

    def _adaptive_min_pixel(self, track) -> float:
        """bbox 尺度自适应的速度差阈值.

        近景大目标 (bbox 大) 帧间位移天然更大, 固定像素阈值会误杀;
        阈值下限 = bbox 最大边 × min_pixel_scale_ratio, 且不低于 min_pixel.
        """
        bbox = getattr(track, "bbox", None)
        if not bbox or len(bbox) < 4:
            return self.min_pixel
        scale = max(bbox[2] - bbox[0], bbox[3] - bbox[1])
        if scale <= 0:
            return self.min_pixel
        return max(self.min_pixel, scale * self.min_pixel_scale_ratio)

    def _motion_direction_continuous(self, h) -> bool:
        """最近两段运动向量的方向连续性.

        h[-3]→h[-2] 与 h[-2]→h[-1] 的余弦相似度 >= direction_cos_threshold
        视为方向连续 (夹角 < 60°). 历史段近乎静止时无方向可言, 不作连续佐证.
        """
        ax = h[-2][0] - h[-3][0]
        ay = h[-2][1] - h[-3][1]
        bx = h[-1][0] - h[-2][0]
        by = h[-1][1] - h[-2][1]
        la = math.hypot(ax, ay)
        lb = math.hypot(bx, by)
        if la < 1e-6 or lb < 1e-6:
            return False
        cos_sim = (ax * bx + ay * by) / (la * lb)
        return cos_sim >= self.direction_cos_threshold

    def validate_direction_consistency(
        self,
        track,
        entry_exit: str,
        line_end: List[float],
    ) -> bool:
        """验证跨线方向与最近运动方向一致性.

        ID 切换时 start_pos 来自前一辆车, 与当前车辆运动方向矛盾.
        双向场景下来回运动也会方向反转, 需结合跨线幅度判断.

        Args:
            track: 轨迹对象
            entry_exit: "enter" 或 "exit"
            line_end: 线段终点 (用于计算线长)

        Returns:
            True 表示方向一致, False 表示可能为 ID 切换
        """
        h = track.history
        win = self.history_window
        if len(h) < win:
            return True

        recent_start_off = self.geometry.offset(h[-win], self.line_start, self.normal)
        recent_end_off = self.geometry.offset(h[-1], self.line_start, self.normal)
        recent_cross_inner = recent_end_off - recent_start_off  # >0 向内, <0 向外

        # 仅当矛盾幅度较大时才判定为 ID 切换 (避免慢速来回运动被误杀)
        recent_cross_mag = abs(recent_cross_inner)
        line_span = math.sqrt(self.geometry.line_len_sq(self.line_start, line_end))
        if line_span > 0 and recent_cross_mag > line_span * 0.5:
            if entry_exit == "exit" and recent_cross_inner > 0:
                return False
            if entry_exit == "enter" and recent_cross_inner < 0:
                return False

        return True