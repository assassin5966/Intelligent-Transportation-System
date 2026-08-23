"""几何计算引擎：提供坐标转换、法向量计算、投影等基础几何运算.

纯函数式设计，不依赖任何轨迹状态，易于测试和复用。
"""
import math
from typing import List, Tuple


class GeometryEngine:
    """几何计算引擎.

    负责所有与计数线相关的几何计算，包括：
    - 归一化坐标 <-> 像素坐标转换
    - 内侧法向量计算
    - 点沿法向的投影 (offset)
    - 点到线段距离
    - 点在线段上的投影参数 t
    """

    def __init__(self, frame_width: int = 1920, frame_height: int = 1080):
        self.frame_width = frame_width
        self.frame_height = frame_height

    def normalize_to_pixel(self, point: List[float]) -> List[float]:
        """归一化坐标 [0-1] 转像素坐标."""
        return [point[0] * self.frame_width, point[1] * self.frame_height]

    def compute_inner_normal(
        self,
        line_start: List[float],
        line_end: List[float],
        anchor: List[float],
    ) -> Tuple[List[float], List[float]]:
        """计算内侧法向量（指向锚点所在侧）.

        Args:
            line_start: 线段起点 (像素坐标)
            line_end: 线段终点 (像素坐标)
            anchor: 内侧锚点 (像素坐标)

        Returns:
            (n_inner, n_unit): 法向量（长度=线段长度）和归一化法向量
        """
        dx = line_end[0] - line_start[0]
        dy = line_end[1] - line_start[1]
        line_len = math.sqrt(dx * dx + dy * dy)

        # 法向 (左旋90°): n=(-dy, dx)
        nx, ny = -dy, dx

        # 用锚点归一化：使法向指向锚点所在侧 (内侧)
        anchor_off = nx * (anchor[0] - line_start[0]) + ny * (anchor[1] - line_start[1])
        if anchor_off < 0:
            nx, ny = -nx, -ny

        # 归一化法向
        if line_len > 0:
            unit_nx, unit_ny = nx / line_len, ny / line_len
        else:
            unit_nx, unit_ny = 0.0, 0.0

        return [nx, ny], [unit_nx, unit_ny]

    def offset(self, point: List[float], line_start: List[float], normal: List[float]) -> float:
        """点沿法向的投影: >0 内侧, <0 外侧."""
        return normal[0] * (point[0] - line_start[0]) + normal[1] * (point[1] - line_start[1])

    def side(self, point: List[float], line_start: List[float], normal: List[float]) -> int:
        """判断点在线段的哪一侧: 1=内侧, -1=外侧, 0=线上."""
        off = self.offset(point, line_start, normal)
        if off > 0:
            return 1
        if off < 0:
            return -1
        return 0

    def point_to_line_distance(
        self,
        point: List[float],
        line_start: List[float],
        line_end: List[float],
    ) -> float:
        """点到线段的距离."""
        A = point[0] - line_start[0]
        B = point[1] - line_start[1]
        C = line_end[0] - line_start[0]
        D = line_end[1] - line_start[1]
        dot = A * C + B * D
        len_sq = C * C + D * D
        if len_sq == 0:
            return math.hypot(point[0] - line_start[0], point[1] - line_start[1])
        param = dot / len_sq
        if param < 0:
            xx, yy = line_start[0], line_start[1]
        elif param > 1:
            xx, yy = line_end[0], line_end[1]
        else:
            xx = line_start[0] + param * C
            yy = line_start[1] + param * D
        return math.hypot(point[0] - xx, point[1] - yy)

    def projection_t(self, point: List[float], line_start: List[float], line_end: List[float]) -> float:
        """点在线段上的投影参数 t ∈ [0,1].

        P(t) = line_start + t * (line_end - line_start)
        t 超出 [0,1] 表示投影在线段延长线上.
        """
        dx = line_end[0] - line_start[0]
        dy = line_end[1] - line_start[1]
        len_sq = dx * dx + dy * dy
        if len_sq == 0:
            return 0.0
        return (
            (point[0] - line_start[0]) * dx
            + (point[1] - line_start[1]) * dy
        ) / len_sq

    def line_len_sq(self, line_start: List[float], line_end: List[float]) -> float:
        """线段长度的平方."""
        dx = line_end[0] - line_start[0]
        dy = line_end[1] - line_start[1]
        return dx * dx + dy * dy

    def line_len(self, line_start: List[float], line_end: List[float]) -> float:
        """线段长度."""
        return math.sqrt(self.line_len_sq(line_start, line_end))