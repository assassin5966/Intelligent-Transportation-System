"""事件生成器：根据越线检测结果生成 CrossingEvent."""
from datetime import datetime
from typing import List

from ..schemas.events import (
    PERSON_ENTER,
    PERSON_EXIT,
    VEHICLE_ENTER,
    VEHICLE_EXIT,
)


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


class EventGenerator:
    """事件生成器.

    负责根据跨线检测结果生成 CrossingEvent 对象.
    不依赖轨迹状态，纯函数式设计.
    """

    @staticmethod
    def determine_event_type(class_name: str, entry_exit: str) -> str:
        """根据目标类别和跨线方向确定事件类型.

        person/bicycle/motorcycle 归为人流, car/truck/bus 归为车流.
        """
        if class_name in ("person", "bicycle", "motorcycle"):
            return PERSON_ENTER if entry_exit == "enter" else PERSON_EXIT
        else:
            return VEHICLE_ENTER if entry_exit == "enter" else VEHICLE_EXIT

    @staticmethod
    def generate(
        track,
        camera_id: str,
        cross_point: List[float],
        direction: str,
        entry_exit: str,
        line_name: str = "line_a",
    ) -> CrossingEvent:
        """生成越线事件.

        Args:
            track: 轨迹对象
            camera_id: 摄像头 ID
            cross_point: 跨线点 (像素坐标)
            direction: 跨线方向 ("outer_to_inner" | "inner_to_outer")
            entry_exit: "enter" 或 "exit"
            line_name: 计数线名称

        Returns:
            CrossingEvent 对象
        """
        event_type = EventGenerator.determine_event_type(track.class_name, entry_exit)
        return CrossingEvent(
            event_type=event_type,
            track_id=track.track_id,
            class_name=track.class_name,
            timestamp=datetime.now().isoformat(),
            camera_id=camera_id,
            cross_point=cross_point,
            cross_line=line_name,
            direction=direction,
            confidence=track.confidence,
        )