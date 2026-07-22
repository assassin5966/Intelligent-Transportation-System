"""事件与统计 schema (AI <-> 后端 通信契约)."""
from datetime import datetime

from pydantic import BaseModel, Field

# 业务事件类型 (与方案一致: 仅输出事件, 不传视频)
VEHICLE_ENTER = "VehicleEnter"
VEHICLE_EXIT = "VehicleExit"
PERSON_ENTER = "PersonEnter"
PERSON_EXIT = "PersonExit"

EVENT_TYPES = {VEHICLE_ENTER, VEHICLE_EXIT, PERSON_ENTER, PERSON_EXIT}

# 进入/离开 -> 对统计量的影响 (delta)
EVENT_DELTA: dict[str, dict[str, int]] = {
    VEHICLE_ENTER: {"current_vehicles": 1, "today_vehicle_in": 1},
    VEHICLE_EXIT: {"current_vehicles": -1, "today_vehicle_out": 1},
    PERSON_ENTER: {"current_persons": 1, "today_person_in": 1},
    PERSON_EXIT: {"current_persons": -1, "today_person_out": 1},
}


class EventIn(BaseModel):
    """AI 推送到后端的事件."""

    device_id: str = Field(..., description="设备ID")
    event_type: str = Field(..., description="VehicleEnter/VehicleExit/PersonEnter/PersonExit")
    occurred_at: datetime = Field(..., description="事件发生时间")


class RealtimeStats(BaseModel):
    """实时统计 (Redis 维护)."""

    current_vehicles: int = 0
    current_persons: int = 0
    today_vehicle_in: int = 0
    today_vehicle_out: int = 0
    today_person_in: int = 0
    today_person_out: int = 0
    active_devices: int = 0
    updated_at: datetime


class TrendPoint(BaseModel):
    """趋势曲线单点."""

    time: datetime
    vehicles: int = 0
    persons: int = 0


class AlertOut(BaseModel):
    id: int
    level: str
    category: str
    message: str
    value: float | None = None
    threshold: float | None = None
    created_at: datetime
