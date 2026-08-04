"""Redis 实时状态管理.

当前车辆/人员数量 (持久) + 今日累计进出 (按日 key, 自然按天切换).
N 分钟区间计数 (供 Chronos-2 总人数预测). 逐设备在場人数 (供警力分配).
"""
from datetime import date, datetime, timezone
from typing import Optional

from ...common.config import settings
from ...common.redis_client import get_redis
from ...schemas.events import EVENT_DELTA

_CUR_KEY = f"{settings.redis_prefix}:realtime:current"
_DEVICES_KEY = f"{settings.redis_prefix}:devices:active"


def _device_key(device_id: str) -> str:
    """逐设备在場人数 key (供警力分配按区域读取)."""
    return f"{settings.redis_prefix}:realtime:device:{device_id}"

# 当前态字段 (可正可负, 会做下限钳位)
_CURRENT_FIELDS = ("current_vehicles", "current_persons")
# 今日累计字段 (只增)
_DAILY_FIELDS = (
    "today_vehicle_in",
    "today_vehicle_out",
    "today_person_in",
    "today_person_out",
)

# 日累计字段 -> N 分钟区间字段 (供 Chronos-2 总人数预测读取序列)
_FIELD_MAP = {
    "today_vehicle_in": "vehicle_in",
    "today_vehicle_out": "vehicle_out",
    "today_person_in": "person_in",
    "today_person_out": "person_out",
}


def _daily_key(d: date) -> str:
    return f"{settings.redis_prefix}:realtime:daily:{d.strftime('%Y%m%d')}"


def _interval_key(dt: datetime) -> str:
    """N 分钟区间 key (向下取整到区间起点)."""
    interval = settings.prediction_interval_minutes
    minute = (dt.minute // interval) * interval
    interval_start = dt.replace(minute=minute, second=0, microsecond=0)
    return f"{settings.redis_prefix}:realtime:interval:{interval_start.strftime('%Y%m%d%H%M')}"


async def apply_event(
    event_type: str, device_id: str, occurred_at: Optional[datetime] = None
) -> dict:
    """应用一个业务事件到 Redis 实时状态, 返回更新后的统计.

    occurred_at: 事件实际发生时间 (用于日/N分钟区间维度聚合); 缺省取当前时间.
    """
    delta = EVENT_DELTA.get(event_type)
    if delta is None:
        return await get_stats()

    redis = get_redis()
    # 用事件发生时间聚合日/N分钟区间维度 (离线回放/延迟事件落到正确时段)
    event_time = occurred_at or datetime.now()
    daily_key = _daily_key(event_time.date())
    interval_key = _interval_key(event_time)
    pipe = redis.pipeline()
    device_key = _device_key(device_id)
    for field, d in delta.items():
        if field in _CURRENT_FIELDS:
            pipe.hincrby(_CUR_KEY, field, d)
            pipe.hincrby(device_key, field, d)  # 逐设备在場人数 (供警力分配)
        else:
            pipe.hincrby(daily_key, field, d)
            interval_field = _FIELD_MAP.get(field)
            if interval_field is not None:
                # N 分钟区间计数 (供 Chronos-2 总人数预测)
                pipe.hincrby(interval_key, interval_field, d)
    pipe.hset(_CUR_KEY, "updated_at", datetime.now(timezone.utc).isoformat())
    pipe.sadd(_DEVICES_KEY, device_id)
    pipe.expire(daily_key, 90 * 24 * 3600)  # 日累计保留 90 天, 防止内存泄漏
    pipe.expire(device_key, 24 * 3600)  # 逐设备在場人数保留 24 小时
    # N 分钟区间保留 (序列长度 + 余量) × 区间分钟 × 60 秒
    interval_ttl = (settings.prediction_series_length + 10) * settings.prediction_interval_minutes * 60
    pipe.expire(interval_key, interval_ttl)
    await pipe.execute()

    await _clamp_negatives(_CUR_KEY, _CURRENT_FIELDS)
    return await get_stats()


async def _clamp_negatives(key: str, fields: tuple[str, ...]) -> None:
    """当前数量不允许为负 (异常事件/重启漂移修正)."""
    redis = get_redis()
    pipe = redis.pipeline()
    for f in fields:
        pipe.hget(key, f)
    vals = await pipe.execute()
    pipe = redis.pipeline()
    for f, v in zip(fields, vals):
        if v is not None and int(v) < 0:
            pipe.hset(key, f, 0)
    await pipe.execute()


async def get_stats() -> dict:
    """读取实时统计 (供 REST/告警引擎使用)."""
    redis = get_redis()
    daily_key = _daily_key(date.today())
    pipe = redis.pipeline()
    pipe.hgetall(_CUR_KEY)
    pipe.hgetall(daily_key)
    pipe.scard(_DEVICES_KEY)
    cur, daily, active = await pipe.execute()

    def _i(v) -> int:
        return int(v) if v else 0

    updated_raw = cur.get("updated_at")
    try:
        updated_at = (
            datetime.fromisoformat(updated_raw) if updated_raw else datetime.now(timezone.utc)
        )
    except ValueError:
        updated_at = datetime.now(timezone.utc)

    return {
        "current_vehicles": _i(cur.get("current_vehicles")),
        "current_persons": _i(cur.get("current_persons")),
        "today_vehicle_in": _i(daily.get("today_vehicle_in")),
        "today_vehicle_out": _i(daily.get("today_vehicle_out")),
        "today_person_in": _i(daily.get("today_person_in")),
        "today_person_out": _i(daily.get("today_person_out")),
        "active_devices": int(active),
        "updated_at": updated_at,
    }


async def reset_current() -> None:
    """重置当前计数 (设备重启/校准时调用)."""
    redis = get_redis()
    await redis.delete(_CUR_KEY)


async def get_device_crowd(device_id: str) -> dict:
    """读取单个设备的在場人数 (供警力分配)."""
    redis = get_redis()
    data = await redis.hgetall(_device_key(device_id))

    def _i(v) -> int:
        return int(v) if v else 0

    return {
        "device_id": device_id,
        "current_persons": max(0, _i(data.get("current_persons"))),
        "current_vehicles": max(0, _i(data.get("current_vehicles"))),
    }


async def get_all_device_crowds() -> dict[str, dict]:
    """读取所有活跃设备的在場人数 (供警力分配批量读取)."""
    redis = get_redis()
    device_ids = await redis.smembers(_DEVICES_KEY)
    if not device_ids:
        return {}
    pipe = redis.pipeline()
    for did in device_ids:
        pipe.hgetall(_device_key(did))
    results = await pipe.execute()
    output: dict[str, dict] = {}
    for did, data in zip(device_ids, results):
        did_str = did if isinstance(did, str) else did.decode()
        persons = int(data.get("current_persons", 0)) if data else 0
        vehicles = int(data.get("current_vehicles", 0)) if data else 0
        output[did_str] = {
            "device_id": did_str,
            "current_persons": max(0, persons),
            "current_vehicles": max(0, vehicles),
        }
    return output
