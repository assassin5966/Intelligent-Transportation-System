"""跨线检测器：判断轨迹是否越过计数线."""
import math
from typing import List, Optional, Tuple

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
    ) -> Optional[Tuple[str, List[float]]]:
        """检测是否发生跨线.

        Args:
            prev_point: 上一帧轨迹中心点 (像素坐标)
            curr_point: 当前帧轨迹中心点 (像素坐标)

        Returns:
            (方向, 交点像素坐标) | None
            方向: "outer_to_inner" | "inner_to_outer"
            交点: prev→curr 运动线段与计数线的精确交点
                  (而非 curr_point 的投影, 斜穿线端点时后者会落在段外造成漏计)
        """
        prev_off = self.geometry.offset(prev_point, self.line_start, self.normal)
        curr_off = self.geometry.offset(curr_point, self.line_start, self.normal)

        # 跨线检测: offset 变号.
        # 注意不能用 prev_off * curr_off >= 0 直接排除: 中心点恰好落在计数线上
        # (offset==0) 时乘积为 0, 会同时丢弃"越过线"和"离开线"两帧, 整次跨线静默漏计.
        if prev_off * curr_off > 0:
            return None
        if prev_off == 0.0 and curr_off == 0.0:
            return None

        # 夹角过滤: 排除沿线滑动
        if not self._angle_filter(prev_point, curr_point):
            return None

        # 本帧恰落在线上: 侧别未定, 留待下一帧确认 (下一帧 prev_off==0 可判向)
        if curr_off == 0.0 and prev_off != 0.0:
            return None

        # 精确求交: prev→curr 运动线段与计数线的交点 (offset 沿运动线段线性变化)
        cross_point = self._segment_line_intersection(
            prev_point, curr_point, prev_off, curr_off
        )

        # 投影范围检查: 交点需落在计数线有效段内 (排除延长线误判)
        if not self._in_segment(cross_point):
            return None

        # 方向判定 (基于 offset, 绝对语义)
        if prev_off < 0 and curr_off > 0:
            return "outer_to_inner", cross_point
        elif prev_off > 0 and curr_off < 0:
            return "inner_to_outer", cross_point
        else:
            # prev_off == 0 (上一帧恰落在线上): 由本帧所处侧别判定方向
            return ("inner_to_outer" if curr_off < 0 else "outer_to_inner"), cross_point

    def _segment_line_intersection(
        self,
        prev_point: List[float],
        curr_point: List[float],
        prev_off: float,
        curr_off: float,
    ) -> List[float]:
        """求 prev→curr 运动线段与计数线的交点 (像素坐标).

        offset 为仿射函数, 沿运动线段线性变化:
          off(s) = prev_off + s * (curr_off - prev_off)
        令 off(s*) = 0 -> s* = prev_off / (prev_off - curr_off), s*∈[0,1] 时交点在段内.
        prev_off==0 时交点即 prev_point (上一帧落线), curr_off==0 时为 curr_point.
        """
        denom = prev_off - curr_off
        if abs(denom) < 1e-9:
            return [float(curr_point[0]), float(curr_point[1])]
        s = prev_off / denom
        s = max(0.0, min(1.0, s))
        return [
            float(prev_point[0] + s * (curr_point[0] - prev_point[0])),
            float(prev_point[1] + s * (curr_point[1] - prev_point[1])),
        ]

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