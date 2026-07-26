"""越线 (Line Crossing) 计数.

单计数线模式: 物体跨过计数线即按跨线方向产出业务事件:
  负侧→正侧 = Enter, 正侧→负侧 = Exit (叉积侧判定, 适用于水平/垂直任意方向计数线).
方向由跨线序列起止位置的叉积侧判定, 不受帧间抖动影响.
单向流动: 每条轨迹只计一次 (counted_tracks 去重).
防抖: 事件落定需轨迹远离计数线 (min_distance_threshold).
"""
import math
from datetime import datetime
from typing import List, Tuple, Optional, Dict

from ..schemas.events import (
    PERSON_ENTER,
    PERSON_EXIT,
    VEHICLE_ENTER,
    VEHICLE_EXIT,
)

Point = tuple[float, float]


class CrossingEvent:
    def __init__(self, event_type, track_id, class_name, timestamp, camera_id, cross_point, cross_line, direction, confidence):
        self.event_type = event_type
        self.track_id = track_id
        self.class_name = class_name
        self.timestamp = timestamp
        self.camera_id = camera_id
        self.cross_point = cross_point
        self.cross_line = cross_line
        self.direction = direction
        self.confidence = confidence


def _cross_product(o: Point, a: Point, b: Point) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _point_side_of_line(point: Point, line_start: Point, line_end: Point) -> float:
    return _cross_product(line_start, line_end, point)


def _detect_crossing(prev_point: Point, curr_point: Point, line: List[Point]) -> Tuple[bool, float, float]:
    prev_side = _point_side_of_line(prev_point, line[0], line[1])
    curr_side = _point_side_of_line(curr_point, line[0], line[1])

    if prev_side * curr_side < 0:
        return True, prev_side, curr_side
    return False, prev_side, curr_side


class LineCrossingCounter:
    """单计数线越线计数器.

    - 跨过计数线, 按跨线方向判定 Enter/Exit (下移=Enter, 上移=Exit)
    - 单向流动: 每条轨迹只计一次 (counted_tracks 去重)
    - 防抖: 事件落定需轨迹远离计数线 (min_distance_threshold)
    """

    def __init__(self, line: Optional[Tuple[Point, Point]] = None):
        if line is not None:
            self.line_points = [list(line[0]), list(line[1])]
        else:
            self.line_points = [[0.5, 0.1], [0.5, 0.9]]

        self.line_name = "line_a"

        self.anti_jitter = True
        self.min_distance_ratio = 0.05  # 距线最小距离占帧短边的比例
        self.min_distance_threshold = 50  # set_frame_size 按帧尺寸重算
        self.endpoint_sensitivity = 0.05

        self.track_crossing_history: Dict[str, List[Tuple[float, str, str]]] = {}
        self.track_states: Dict[str, str] = {}
        self.track_crossing_start_pos: Dict[str, List[float]] = {}  # 跨线序列起始位置 (跨线前)
        self.counted_tracks: set = set()  # 已计数的轨迹 (单向流动每物体只计一次)
        self.frame_width = 1920
        self.frame_height = 1080

    def set_frame_size(self, width: int, height: int):
        self.frame_width = width
        self.frame_height = height
        self.min_distance_threshold = max(
            20, int(self.min_distance_ratio * min(width, height))
        )

    def set_line(self, line: List[Point]):
        """设置计数线 (归一化坐标 [[x1,y1],[x2,y2]])."""
        self.line_points = [list(line[0]), list(line[1])]

    def set_lines(self, outer_line: List[Point], inner_line: Optional[List[Point]] = None):
        """向后兼容: 仅使用第一条线 (outer_line), 忽略 inner_line."""
        self.set_line(outer_line)

    def _normalize_to_pixel(self, point: List[float]) -> List[float]:
        return [point[0] * self.frame_width, point[1] * self.frame_height]

    def _get_cross_direction(self, prev_side: float, curr_side: float) -> str:
        if prev_side < 0 and curr_side > 0:
            return "left_to_right"
        elif prev_side > 0 and curr_side < 0:
            return "right_to_left"
        else:
            return "same_side"

    def _point_to_line_distance(self, point: List[float], line_start: List[float], line_end: List[float]) -> float:
        A = point[0] - line_start[0]
        B = point[1] - line_start[1]
        C = line_end[0] - line_start[0]
        D = line_end[1] - line_start[1]

        dot = A * C + B * D
        len_sq = C * C + D * D

        if len_sq == 0:
            return math.sqrt((point[0] - line_start[0])**2 + (point[1] - line_start[1])**2)

        param = dot / len_sq

        if param < 0:
            xx, yy = line_start[0], line_start[1]
        elif param > 1:
            xx, yy = line_end[0], line_end[1]
        else:
            xx = line_start[0] + param * C
            yy = line_start[1] + param * D

        dx = point[0] - xx
        dy = point[1] - yy
        return math.sqrt(dx*dx + dy*dy)

    def _is_clear_of_line(self, current_center: List[float]) -> bool:
        """确认轨迹已远离计数线 (防抖确认策略).

        事件只在轨迹中心距计数线超过 ``min_distance_threshold`` 时才落定,
        避免跟踪框在线附近抖动造成误计数.
        """
        line_pixels = [self._normalize_to_pixel(p) for p in self.line_points]
        dist = self._point_to_line_distance(current_center, line_pixels[0], line_pixels[1])
        return dist >= self.min_distance_threshold

    def _filter_endpoint_false_positive(self, track) -> bool:
        current_center = track.center

        for endpoint in self.line_points:
            endpoint_pixel = self._normalize_to_pixel(endpoint)
            distance = math.sqrt(
                (current_center[0] - endpoint_pixel[0])**2 +
                (current_center[1] - endpoint_pixel[1])**2
            )

            sensitivity_pixel = self.endpoint_sensitivity * max(self.frame_width, self.frame_height)
            if distance < sensitivity_pixel:
                if track.history and len(track.history) > 1:
                    prev_center = track.history[-2]
                    prev_distance = math.sqrt(
                        (prev_center[0] - endpoint_pixel[0])**2 +
                        (prev_center[1] - endpoint_pixel[1])**2
                    )
                    if prev_distance < sensitivity_pixel:
                        return False
                return False

        return True

    def process_tracks(self, track_result, camera_id: str = "CAM001") -> List[CrossingEvent]:
        events = []

        line_pixels = [self._normalize_to_pixel(p) for p in self.line_points]

        for track in track_result.tracks:
            track_id = track.track_id

            if track_id not in self.track_states:
                self.track_states[track_id] = "TRACKING"
            if track_id not in self.track_crossing_history:
                self.track_crossing_history[track_id] = []

            if len(track.history) < 2:
                continue

            prev_point = track.history[-2]
            curr_point = track.center

            # 检测当前帧是否跨过计数线
            current_time = datetime.now().timestamp()
            crossed, prev_side, curr_side = _detect_crossing(prev_point, curr_point, line_pixels)
            if crossed:
                direction = self._get_cross_direction(prev_side, curr_side)
                self.track_crossing_history[track_id].append((current_time, self.line_name, direction))
                # 跨线序列起始: 记录跨线前位置, 用于最终位移方向判定 (避免帧间抖动反转方向)
                if self.track_states[track_id] != "CROSSING":
                    self.track_crossing_start_pos[track_id] = list(prev_point)
                self.track_states[track_id] = "CROSSING"

            # 非 CROSSING 状态: 无待确认事件
            if self.track_states[track_id] != "CROSSING":
                continue

            # 防抖: 需远离计数线后才落定
            if self.anti_jitter:
                if not self._is_clear_of_line(curr_point):
                    continue
                if not self._filter_endpoint_false_positive(track):
                    continue

            # 方向由跨线序列起止的叉积侧判定 (适用于任意方向计数线, 不受帧间抖动影响)
            # 起始侧 → 当前侧: 负→正 = Enter, 正→负 = Exit
            start_pos = self.track_crossing_start_pos.get(track_id, prev_point)
            start_side = _point_side_of_line(start_pos, line_pixels[0], line_pixels[1])
            curr_side_now = _point_side_of_line(curr_point, line_pixels[0], line_pixels[1])
            if start_side < 0 and curr_side_now > 0:
                entry_exit = "enter"
                direction_str = "left_to_right"
            elif start_side > 0 and curr_side_now < 0:
                entry_exit = "exit"
                direction_str = "right_to_left"
            else:
                # 起止同侧 (抖动跨回), 不产出事件
                self.track_states[track_id] = "TRACKING"
                continue

            # 单向流动: 每条轨迹只计一次
            if track_id in self.counted_tracks:
                self.track_states[track_id] = "TRACKING"
                continue

            if track.class_name == "person":
                event_type = PERSON_ENTER if entry_exit == "enter" else PERSON_EXIT
            else:
                event_type = VEHICLE_ENTER if entry_exit == "enter" else VEHICLE_EXIT

            event = CrossingEvent(
                event_type=event_type,
                track_id=track_id,
                class_name=track.class_name,
                timestamp=datetime.now().isoformat(),
                camera_id=camera_id,
                cross_point=curr_point,
                cross_line=self.line_name,
                direction=direction_str,
                confidence=track.confidence,
            )
            events.append(event)
            self.counted_tracks.add(track_id)  # 标记该轨迹已计数 (单向流动只计一次)
            self.track_states[track_id] = "TRACKING"

        self._cleanup_old_tracks()

        return events

    def _cleanup_old_tracks(self):
        current_time = datetime.now().timestamp()
        to_remove = []

        for track_id, history in self.track_crossing_history.items():
            if not history:
                to_remove.append(track_id)
                continue

            last_time = history[-1][0]
            if current_time - last_time > 20:
                to_remove.append(track_id)

        for track_id in to_remove:
            if track_id in self.track_crossing_history:
                del self.track_crossing_history[track_id]
            if track_id in self.track_states:
                del self.track_states[track_id]
            if track_id in self.track_crossing_start_pos:
                del self.track_crossing_start_pos[track_id]
            # 注意: 不清理 counted_tracks —— 已计数的轨迹永久去重,
            # 避免离线处理慢帧/墙钟时间偏差导致同一轨迹被重复计数.

    def reset(self):
        self.track_crossing_history.clear()
        self.track_states.clear()
        self.track_crossing_start_pos.clear()
        self.counted_tracks.clear()
