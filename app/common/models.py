"""ORM 模型: 事件 / 告警 / 设备 / 小时统计."""
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    Integer,
    String,
    Text,
    Index,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Event(Base):
    """AI 推送的业务事件 (VehicleEnter/Exit, PersonEnter/Exit)."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True
    )

    __table_args__ = (Index("ix_events_device_type_time", "device_id", "event_type", "occurred_at"),)


class Alert(Base):
    """规则告警记录."""

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    level: Mapped[str] = mapped_column(String(16), index=True)  # info/warning/critical
    category: Mapped[str] = mapped_column(String(32), index=True)
    message: Mapped[str] = mapped_column(String(255))
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True
    )


class Device(Base):
    """摄像头设备."""

    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    stream_url: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(16), default="offline")
    # 越线计数线坐标 (JSON: [[x1,y1],[x2,y2], ...])
    line_coords: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class HourlyStat(Base):
    """小时维度统计 (趋势分析 / 预测样本)."""

    __tablename__ = "hourly_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stat_hour: Mapped[datetime] = mapped_column(DateTime, index=True)  # 整点桶
    vehicle_in: Mapped[int] = mapped_column(Integer, default=0)
    vehicle_out: Mapped[int] = mapped_column(Integer, default=0)
    person_in: Mapped[int] = mapped_column(Integer, default=0)
    person_out: Mapped[int] = mapped_column(Integer, default=0)
    peak_vehicles: Mapped[int] = mapped_column(Integer, default=0)
    peak_persons: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (Index("ix_hourly_stat_hour", "stat_hour"),)
