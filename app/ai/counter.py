"""越线 (Line Crossing) 计数.

判断轨迹是否跨越预设计数线及方向, 产出业务事件:
  vehicle/person 从 A 侧->B 侧 = Enter, 反向 = Exit.
"""
from ..schemas.events import (
    PERSON_ENTER,
    PERSON_EXIT,
    VEHICLE_ENTER,
    VEHICLE_EXIT,
)

Point = tuple[float, float]


def _cross_direction(p1: Point, p2: Point, a: Point, b: Point) -> int:
    """线段 p1->p2 是否穿越线段 a->b, 返回 +1 / -1 / 0(未穿越)."""
    # 叉积判断
    def cross(o: Point, x: Point, y: Point) -> float:
        return (x[0] - o[0]) * (y[1] - o[1]) - (x[1] - o[1]) * (y[0] - o[0])

    d1 = cross(b, a, p1)
    d2 = cross(b, a, p2)
    d3 = cross(p2, p1, a)
    d4 = cross(p2, p1, b)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return 1 if (d2 - d1) > 0 else -1
    return 0


class LineCrossingCounter:
    """单条计数线的越线计数器."""

    def __init__(self, line: tuple[Point, Point]):
        self.a, self.b = line
        self._prev: dict[int, Point] = {}

    def update(
        self, track_id: int, category: str, cur_center: Point
    ) -> str | None:
        """更新一个跟踪点, 若发生越线则返回事件类型, 否则 None."""
        prev = self._prev.get(track_id)
        self._prev[track_id] = cur_center
        if prev is None:
            return None

        direction = _cross_direction(prev, cur_center, self.a, self.b)
        if direction == 0:
            return None

        if category == "vehicle":
            return VEHICLE_ENTER if direction > 0 else VEHICLE_EXIT
        return PERSON_ENTER if direction > 0 else PERSON_EXIT
