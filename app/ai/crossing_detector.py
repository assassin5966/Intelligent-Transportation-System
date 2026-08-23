"""跨线检测器：判断轨迹是否越过计数线."""
import math
from typing import List, Optional

from .geometry_engine import GeometryEngine
from .roi_detector import ROIDetector


class CrossingDetector:
    """跨线检测器.

    负责跨线判断、夹角过滤、线段范围检查等.
    依赖 GeometryEngine 和 ROIDetector 的几何计算能力.
    """

    def __init__(
        self,
        geometry: GeometryEngine,
        roi_detector: ROIDetector,
        min_motion: float = 2.0,
    ):
        self.geometry = geometry
        self.roi_detector = roi_detector
        self.min_motion = min_motion

        # 计数线参数 (由 LineCrossingCounter 通过 set_line_params 设置)
        self.line_start: List[float] = [0.0, 0.0]
        self.line_end: List[float] = [0.0, 0.0]
        self.normal: List[float] = [0.0, 0.0]
        self.unit_normal: List[float] = [0.0, 0.0]
        self.clip_t0: float = 0.0
        self.clip_t1: float = 1.0

    def set_line_params(
        self,
        line_start: List[float],
        line_end: List[float],
        normal: List[float],
        unit_normal: List[float],
        clip_t0: float = 0.0,
        clip_t1: float = 1.0,
    ):
        """设置计数线参数 (由 LineCrossingCounter 在预计算时调用)."""
        self.line_start = line_start
        self.line_end = line_end
        self.normal = normal
        self.unit_normal = unit_normal
        self.clip_t0 = clip_t0
        self.clip_t1 = clip_t1

    def detect_crossing(
        self,
        prev_point: List[float],
        curr_point: List[float],
    ) -> Optional[str]:
        """检测是否发生跨线.

        Args:
            prev_point: 上一帧轨迹中心点 (像素坐标)
            curr_point: 当前帧轨迹中心点 (像素坐标)

        Returns:
            "outer_to_inner" | "inner_to_outer" | None
        """
        prev_off = self.geometry.offset(prev_point, self.line_start, self.normal)
        curr_off = self.geometry.offset(curr_point, self.line_start, self.normal)

        # 跨线检测: offset 异号
        if prev_off * curr_off >= 0:
            return None

        # 夹角过滤: 排除沿线滑动
        if not self._angle_filter(prev_point, curr_point):
            return None

        # 投影范围检查: 排除延长线误判
        if not self._in_segment(curr_point):
            return None

        # 方向判定 (基于 offset, 绝对语义)
        if prev_off < 0 and curr_off > 0:
            return "outer_to_inner"
        elif prev_off > 0 and curr_off < 0:
            return "inner_to_outer"
        else:
            return None

    def _angle_filter(self, prev_point: List[float], curr_point: List[float]) -> bool:
        """夹角过滤: 运动向量在法向方向的投影 >= min_motion.

        运动向量 v 在法向 n_unit 上的投影 = v·n_unit, 即跨线方向位移.
        返回 True 表示通过 (有效跨线运动).
        """
        vx = curr_point[0] - prev_point[0]
        vy = curr_point[1] - prev_point[1]
        cross_component = vx * self.unit_normal[0] + vy * self.unit_normal[1]
        return abs(cross_component) >= self.min_motion

    def _in_segment(self, point: List[float]) -> bool:
        """跨线点投影落在计数线有效段内 (裁剪到 ROI 后的 t∈[clip_t0,clip_t1]).

        ROI 关闭时有效段为整条线 [0,1]; ROI 启用时仅 ROI 内部分有效,
        越过 ROI 外线段不触发计数 (避免失效区误计).
        """
        if self.geometry.line_len_sq(self.line_start, self.line_end) == 0:
            return False
        t = self.geometry.projection_t(point, self.line_start, self.line_end)
        return self.clip_t0 <= t <= self.clip_t1