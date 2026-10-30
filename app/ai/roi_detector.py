"""ROI 检测器：管理感兴趣区域多边形，判断点/线段是否在 ROI 内."""
import math
from typing import List, Optional, Tuple

from ..common.logger import logger
from .geometry_engine import GeometryEngine

Point = tuple[float, float]


class ROIDetector:
    """ROI 检测器.

    负责 ROI 多边形的设置、点在多边形内判断、线段裁剪到 ROI 等功能.
    依赖 GeometryEngine 进行坐标转换，但独立于轨迹状态.
    """

    def __init__(self, geometry: GeometryEngine):
        self.geometry = geometry
        # 归一化坐标 ROI 多边形顶点
        self.roi_polygon: Optional[List[List[float]]] = None
        # 像素坐标 ROI 多边形顶点
        self._roi_px: Optional[List[List[float]]] = None

    def set_roi(self, polygon: Optional[List[Point]]):
        """设置 ROI 感兴趣区域多边形 (归一化 [x,y] 顶点列表, >=3 个顶点).

        仅 ROI 内轨迹参与越线计数; 传 None 关闭 ROI (全画面计数).
        """
        if polygon is None or len(polygon) < 3:
            self.roi_polygon = None
            self._roi_px = None
            logger.info("ROI 已关闭, 全画面计数")
            return
        self.roi_polygon = [list(p) for p in polygon]
        self._recompute_roi_px()
        logger.info(f"已设置 ROI 多边形 ({len(self.roi_polygon)} 顶点)")

    def _recompute_roi_px(self):
        """将归一化 ROI 多边形转像素坐标 (None 时禁用)."""
        if self.roi_polygon is None or len(self.roi_polygon) < 3:
            self._roi_px = None
            return
        self._roi_px = [self.geometry.normalize_to_pixel(list(p)) for p in self.roi_polygon]

    def point_in_roi(self, point: List[float]) -> bool:
        """点是否在 ROI 多边形内 (射线法); ROI 未启用时恒返回 True."""
        if self._roi_px is None:
            return True
        n = len(self._roi_px)
        inside = False
        j = n - 1
        for i in range(n):
            xi, yi = self._roi_px[i]
            xj, yj = self._roi_px[j]
            # ROI 边界属于 ROI；否则沿边计数线会被射线法误判为无效。
            ex, ey = xj - xi, yj - yi
            px, py = point[0] - xi, point[1] - yi
            if (abs(ex * py - ey * px) <= 1e-9 * max(1.0, math.hypot(ex, ey))
                    and min(xi, xj) - 1e-9 <= point[0] <= max(xi, xj) + 1e-9
                    and min(yi, yj) - 1e-9 <= point[1] <= max(yi, yj) + 1e-9):
                return True
            # 射线法: point 与多边形边的交点奇偶性判断内外
            if (yi > point[1]) != (yj > point[1]):
                x_int = (xj - xi) * (point[1] - yi) / (yj - yi) + xi
                if point[0] < x_int:
                    inside = not inside
            j = i
        return inside

    def clip_line_intervals_to_roi(
        self,
        line_start: List[float],
        line_end: List[float],
    ) -> List[Tuple[float, float]]:
        """返回计数线位于 ROI 内的全部闭区间，支持凹多边形及边界重合。"""
        if self._roi_px is None:
            return [(0.0, 1.0)]

        p1x, p1y = line_start
        dx = line_end[0] - line_start[0]
        dy = line_end[1] - line_start[1]
        len_sq = dx * dx + dy * dy

        if len_sq < 1e-6:
            return []

        # 候选参数点: 端点 + 线段与 ROI 每条边的交点
        ts = [0.0, 1.0]
        n = len(self._roi_px)
        for i in range(n):
            ax, ay = self._roi_px[i]
            bx, by = self._roi_px[(i + 1) % n]
            ex, ey = bx - ax, by - ay
            # 求解 P1 + t*(dx,dy) = (ax,ay) + s*(ex,ey), t∈[0,1], s∈[0,1]
            denom = dx * (-ey) + dy * ex
            if abs(denom) < 1e-9:
                if abs((ax - p1x) * dy - (ay - p1y) * dx) < 1e-9:
                    for x, y in ((ax, ay), (bx, by)):
                        t = ((x - p1x) * dx + (y - p1y) * dy) / len_sq
                        if 0.0 <= t <= 1.0:
                            ts.append(t)
                continue
            t = ((ax - p1x) * (-ey) + (ay - p1y) * ex) / denom
            s = (dx * (ay - p1y) - dy * (ax - p1x)) / denom
            if 0.0 <= t <= 1.0 and 0.0 <= s <= 1.0:
                ts.append(t)

        ts = sorted(set(ts))

        intervals: List[Tuple[float, float]] = []
        for i in range(len(ts) - 1):
            seg_t0, seg_t1 = ts[i], ts[i + 1]
            if seg_t1 - seg_t0 < 1e-9:
                continue
            mid_t = (seg_t0 + seg_t1) / 2
            mx = p1x + mid_t * dx
            my = p1y + mid_t * dy
            if self.point_in_roi([mx, my]):
                if intervals and abs(intervals[-1][1] - seg_t0) < 1e-9:
                    intervals[-1] = (intervals[-1][0], seg_t1)
                else:
                    intervals.append((seg_t0, seg_t1))
        return intervals

    def clip_line_to_roi(
        self, line_start: List[float], line_end: List[float],
    ) -> Tuple[float, float, List[float], List[float]]:
        """兼容旧调用方：返回最长子段；计数使用 clip_line_intervals_to_roi。"""
        intervals = self.clip_line_intervals_to_roi(line_start, line_end)
        if not intervals:
            # 线段完全在 ROI 外, 标记整段失效 (t0>t1)
            logger.warning(
                "计数线与 ROI 不相交 (线完全落在 ROI 外), 越线段失效, "
                "该线将不再产生任何越线事件; 请检查 roi_polygon 与 line_coords 配置"
            )
            return 1.0, 0.0, list(line_start), list(line_end)

        best_t0, best_t1 = max(intervals, key=lambda item: item[1] - item[0])
        p1x, p1y = line_start
        dx, dy = line_end[0] - p1x, line_end[1] - p1y
        clip_p0 = [p1x + best_t0 * dx, p1y + best_t0 * dy]
        clip_p1 = [p1x + best_t1 * dx, p1y + best_t1 * dy]
        return best_t0, best_t1, clip_p0, clip_p1
