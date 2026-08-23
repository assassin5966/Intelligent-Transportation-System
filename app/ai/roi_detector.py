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
            # 射线法: point 与多边形边的交点奇偶性判断内外
            if (yi > point[1]) != (yj > point[1]):
                x_int = (xj - xi) * (point[1] - yi) / (yj - yi) + xi
                if point[0] < x_int:
                    inside = not inside
            j = i
        return inside

    def clip_line_to_roi(
        self,
        line_start: List[float],
        line_end: List[float],
    ) -> Tuple[float, float, List[float], List[float]]:
        """将线段 P1->P2 裁剪到 ROI 多边形内, 得到有效参数区间 [t0,t1] 与端点像素.

        线段参数 t: P(t) = P1 + t*(P2-P1), t∈[0,1] 为原线段.
        算法: 收集线段与 ROI 所有边的交点 t + 端点 t=0/1, 排序后扫描相邻 t 的中点,
        中点在 ROI 内 -> 该子段有效; 合并所有有效子段取最长者作为 [t0,t1].
        ROI 关闭时整段有效 [0,1]; 线完全在 ROI 外时 t0>t1 (整段失效).

        Returns:
            (t0, t1, clip_p0, clip_p1): 有效参数区间和裁剪后端点像素坐标
        """
        # 默认整段有效
        t0, t1 = 0.0, 1.0
        clip_p0, clip_p1 = list(line_start), list(line_end)

        if self._roi_px is None:
            return t0, t1, clip_p0, clip_p1

        p1x, p1y = line_start
        dx = line_end[0] - line_start[0]
        dy = line_end[1] - line_start[1]
        len_sq = dx * dx + dy * dy

        if len_sq < 1e-6:
            return t0, t1, clip_p0, clip_p1

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
                continue  # 线段与边平行/重合, 跳过
            t = ((ax - p1x) * (-ey) + (ay - p1y) * ex) / denom
            s = (dx * (ay - p1y) - dy * (ax - p1x)) / denom
            if 0.0 <= t <= 1.0 and 0.0 <= s <= 1.0:
                ts.append(t)

        ts = sorted(set(ts))

        # 扫描相邻 t 区间, 中点在 ROI 内则为有效子段; 取最长有效段
        best_t0, best_t1, best_len = 1.0, 0.0, -1.0
        for i in range(len(ts) - 1):
            seg_t0, seg_t1 = ts[i], ts[i + 1]
            if seg_t1 - seg_t0 < 1e-9:
                continue
            mid_t = (seg_t0 + seg_t1) / 2
            mx = p1x + mid_t * dx
            my = p1y + mid_t * dy
            if self.point_in_roi([mx, my]):
                if (seg_t1 - seg_t0) > best_len:
                    best_t0, best_t1, best_len = seg_t0, seg_t1, seg_t1 - seg_t0

        if best_len < 0:
            # 线段完全在 ROI 外, 标记整段失效 (t0>t1)
            return 1.0, 0.0, list(line_start), list(line_end)

        clip_p0 = [p1x + best_t0 * dx, p1y + best_t0 * dy]
        clip_p1 = [p1x + best_t1 * dx, p1y + best_t1 * dy]
        return best_t0, best_t1, clip_p0, clip_p1