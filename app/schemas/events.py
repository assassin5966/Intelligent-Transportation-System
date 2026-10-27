"""事件与统计 schema (AI <-> 后端 通信契约)."""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

VEHICLE_ENTER = "VehicleEnter"
VEHICLE_EXIT = "VehicleExit"
PERSON_ENTER = "PersonEnter"
PERSON_EXIT = "PersonExit"

EVENT_TYPES = {VEHICLE_ENTER, VEHICLE_EXIT, PERSON_ENTER, PERSON_EXIT}

EVENT_DELTA: dict[str, dict[str, int]] = {
    VEHICLE_ENTER: {"current_vehicles": 1, "today_vehicle_in": 1},
    VEHICLE_EXIT: {"current_vehicles": -1, "today_vehicle_out": 1},
    PERSON_ENTER: {"current_persons": 1, "today_person_in": 1},
    PERSON_EXIT: {"current_persons": -1, "today_person_out": 1},
}


class Detection(BaseModel):
    id: int
    class_name: str
    bbox: List[float]
    confidence: float
    center: List[float]


class DetectionResult(BaseModel):
    frame_id: int
    timestamp: str
    detections: List[Detection]


class Track(BaseModel):
    track_id: str
    class_name: str
    bbox: List[float]
    center: List[float]
    confidence: float
    age: int
    velocity: Optional[List[float]] = None
    history: Optional[List[List[float]]] = None


class TrackResult(BaseModel):
    frame_id: int
    tracks: List[Track]


class CrossingEvent(BaseModel):
    event_type: str
    track_id: str
    class_name: str
    timestamp: str
    camera_id: str
    cross_point: List[float]
    cross_line: str
    direction: str
    confidence: float


class EventIn(BaseModel):
    device_id: str = Field(..., description="设备ID")
    event_type: str = Field(..., description="VehicleEnter/VehicleExit/PersonEnter/PersonExit")
    occurred_at: datetime = Field(..., description="事件发生时间")


class RealtimeStats(BaseModel):
    current_vehicles: int = 0
    current_persons: int = 0
    today_vehicle_in: int = 0
    today_vehicle_out: int = 0
    today_person_in: int = 0
    today_person_out: int = 0
    active_devices: int = 0
    updated_at: datetime


class AlertOut(BaseModel):
    id: int
    level: str
    category: str
    message: str
    value: Optional[float] = None
    threshold: Optional[float] = None
    created_at: datetime
