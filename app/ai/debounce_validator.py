"""防抖验证器：确认跨线事件的真实性，过滤抖动误判."""
import math
from typing import Dict, List, Optional, Tuple

from .geometry_engine import GeometryEngine


class DebounceValidator:
    """防抖验证器.

    负责越过线检测后的防抖确认，包括：
    - 远离线检测 (min_distance_threshold)
    - 端点误判过滤 (endpoint_sensitivity)
    - 滞留确认 (hold_frames)
    - 滞回防抖 (hysteresis_threshold)
    """

    def __init__(
        self,
        geometry: GeometryEngine,
        min_distance_threshold: float = 50,
        hold_frames: int = 3,
        hysteresis_threshold: float = 30,
        endpoint_sensitivity: float = 0.05,
    ):
        self.geometry = geometry
        self.min_distance_threshold = min_distance_threshold
        self.hold_frames = hold_frames
        self.hysteresis_threshold = hysteresis_threshold
        self.endpoint_sensitivity = endpoint_sensitivity

        # 计数线参数 (由 LineCrossingCounter 通过 set_line_params 设置)
        self.line_start: List[float] = [0.0, 0.0]
        self.line_end: List[float] = [0.0, 0.0]
        self.normal: List[float] = [0.0, 0.0]

        # 滞留状态: track_id -> 确认侧 (+1/-1)
        self.track_confirm_side: Dict[str, int] = {}
        # 滞留已确认帧数: track_id -> int
        self.track_hold_count: Dict[str, int] = {}

    def set_line_params(
        self,
        line_start: List[float],
        line_end: List[float],
        normal: List[float],
    ):
        """设置计数线参数 (由 LineCrossingCounter 在预计算时调用)."""
        self.line_start = line_start
        self.line_end = line_end
        self.normal = normal

    def is_clear_of_line(self, point: List[float]) -> bool:
        """轨迹已远离计数线 (防抖距离确认)."""
        dist = self.geometry.point_to_line_distance(point, self.line_start, self.line_end)
        return dist >= self.min_distance_threshold

    def filter_endpoint_false_positive(
        self,
        current_center: List[float],
        prev_center: Optional[List[float]],
        line_points_norm: List[List[float]],
        frame_width: int,
        frame_height: int,
    ) -> bool:
        """端点附近误判过滤: 仅当轨迹连续两帧都停滞在线段端点附近时过滤.

        Args:
            current_center: 当前帧中心点
            prev_center: 上一帧中心点 (None 则不过滤)
            line_points_norm: 计数线端点 (归一化坐标)
            frame_width: 帧宽度
            frame_height: 帧高度

        Returns:
            True 表示通过 (有效跨线), False 表示应过滤
        """
        if prev_center is None:
            return True

        sensitivity_pixel = self.endpoint_sensitivity * max(frame_width, frame_height)

        for endpoint in line_points_norm:
            endpoint_pixel = self.geometry.normalize_to_pixel(endpoint)
            cur_dist = math.hypot(
                current_center[0] - endpoint_pixel[0],
                current_center[1] - endpoint_pixel[1],
            )
            if cur_dist >= sensitivity_pixel:
                continue
            # 当前帧靠近端点: 仅当前一帧也靠近同一端点时才过滤
            prev_dist = math.hypot(
                prev_center[0] - endpoint_pixel[0],
                prev_center[1] - endpoint_pixel[1],
            )
            if prev_dist < sensitivity_pixel:
                return False
        return True

    def confirm_holding(
        self,
        track_id: str,
        curr_off: float,
        hysteresis_offset: float,
        hold_frames: int,
    ) -> str:
        """滞留确认: 连续 hold_frames 帧保持在跨线后侧.

        滞回防抖: 侧别反转需超过 hysteresis_offset, 带内抖动不触发反转.

        Args:
            track_id: 轨迹 ID
            curr_off: 当前帧 offset 值
            hysteresis_offset: 滞回阈值 (像素阈值 * 线长)
            hold_frames: 滞留确认帧数

        Returns:
            "pending": 未确认, 需继续等待
            "side_changed": 侧别反转, 需更新跨线起点
            "confirmed": 确认!
        """
        confirm_side = self.track_confirm_side.get(track_id, 0)

        if curr_off * confirm_side < 0:
            # 目标在确认侧的对侧
            if abs(curr_off) < hysteresis_offset:
                # 滞回带内: 线附近抖动, 跳过本帧
                return "pending"

            # 超过滞回阈值: 真实侧别反转, 更新确认侧
            self.track_hold_count[track_id] = 0
            self.track_confirm_side[track_id] = 1 if curr_off > 0 else -1
            return "side_changed"

        # 递增滞留计数
        self.track_hold_count[track_id] = self.track_hold_count.get(track_id, 0) + 1
        if self.track_hold_count[track_id] < hold_frames:
            return "pending"

        return "confirmed"

    def reset_track(self, track_id: str):
        """清除指定轨迹的滞留状态."""
        self.track_confirm_side.pop(track_id, None)
        self.track_hold_count[track_id] = 0

    def reset(self):
        """重置所有滞留状态."""
        self.track_confirm_side.clear()
        self.track_hold_count.clear()