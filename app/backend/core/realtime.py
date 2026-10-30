"""Redis 实时状态管理.

当前车辆/人员数量 (持久) + 今日累计进出 (按日 key, 自然按天切换).
N 分钟区间计数 (供 Chronos-2 总人数预测). 逐设备在場人数 (供警力分配).
越线事件历史 (Redis List, 供前端"最近事件"与审计回溯).
"""
import json
import hashlib
from uuid import uuid4
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from ...common.config import settings
from ...common.redis_client import get_redis
from ...schemas.events import EVENT_DELTA

_CUR_KEY = f"{settings.redis_prefix}:realtime:current"
_DEVICES_KEY = f"{settings.redis_prefix}:devices:active"
_DEVICE_HASH_PREFIX = f"{settings.redis_prefix}:device:"
# 越线事件历史 (Redis List, lpush 头插, 保留最近 _EVENT_MAX 条)
_EVENTS_KEY = f"{settings.redis_prefix}:events"
_EVENT_MAX = 2000


def _device_key(device_id: str) -> str:
    """逐设备在場人数 key (供警力分配按区域读取)."""
    return f"{settings.redis_prefix}:realtime:device:{device_id}"


def _device_daily_key(device_id: str, d: date) -> str:
    """逐设备今日累计 key (按天隔离, 跨天自动清零)."""
    return f"{settings.redis_prefix}:realtime:device:{device_id}:daily:{d.strftime('%Y%m%d')}"

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
    from ...common.business_rules import get_rule

    interval = int(get_rule("prediction", "interval_minutes", default=settings.prediction_interval_minutes))
    minute = (dt.minute // interval) * interval
    interval_start = dt.replace(minute=minute, second=0, microsecond=0)
    return f"{settings.redis_prefix}:realtime:interval:{interval_start.strftime('%Y%m%d%H%M')}"


def _hourly_key(device_id: str, dt: datetime) -> str:
    """逐设备小时级累计 key (每设备每小时一个 Hash, 按小时自然切换)."""
    return f"{settings.redis_prefix}:hourly:{device_id}:{dt.strftime('%Y%m%d%H')}"


def _hourly_all_key(dt: datetime) -> str:
    """全局小时级累计 key (跨设备汇总, 供"每天每小时车流/人流量"查询)."""
    return f"{settings.redis_prefix}:hourly:all:{dt.strftime('%Y%m%d%H')}"


# 去重记录不设置过期：持久 outbox 可无限期重试，运维须监控 Redis 容量。
# 脚本先校验既有 key 类型及计数字段，避免 Redis 脚本错误造成部分写入。
_APPLY_EVENT_LUA = r"""
local expected = {'hash','hash','hash','hash','hash','hash','hash','set','list','hash','hash','hash','hash','hash','set'}
for i, kind in ipairs(expected) do
    local actual = redis.call('TYPE', KEYS[i]).ok
    if actual ~= 'none' and actual ~= kind then return redis.error_reply('event key type mismatch: ' .. i) end
end
local old = redis.call('HGET', KEYS[10], ARGV[1])
if old then
    if old == ARGV[2] then return 0 end
    return -1
end
local current, daily, interval = ARGV[3], ARGV[4], ARGV[5]
local late, safe = ARGV[13] == '1', true
if late then
    -- 没有完整桶证明时不允许用部分数据覆盖 MySQL；持久隔离待人工处理。
    if tonumber(ARGV[14]) <= tonumber(ARGV[15]) or redis.call('EXISTS',KEYS[6]) == 0
       or redis.call('EXISTS',KEYS[7]) == 0 or redis.call('TTL',KEYS[6]) <= 0
       or redis.call('TTL',KEYS[7]) <= 0 or redis.call('HEXISTS',KEYS[13],ARGV[12]) == 1 then
        safe = false
    end
end
local ver = redis.call('HGET', KEYS[12], ARGV[12])
if ver and (not string.match(ver, '^%d+$') or not tonumber(ver) or tonumber(ver) > 9000000000000000) then
    return redis.error_reply('invalid archive version')
end
for _, pair in ipairs({{1,current},{2,current},{3,daily},{4,daily},{5,interval},{6,interval},{7,interval}}) do
    local value = redis.call('HGET', KEYS[pair[1]], pair[2])
    if value and (not string.match(value, '^%-?%d+$') or not tonumber(value) or math.abs(tonumber(value)) > 9000000000000000) then
        return redis.error_reply('event counter is not a safe integer')
    end
end
local a = redis.call('HINCRBY', KEYS[1], current, ARGV[6])
if a < 0 then redis.call('HSET', KEYS[1], current, 0) end
redis.call('HINCRBY', KEYS[2], current, ARGV[6])
redis.call('HINCRBY', KEYS[3], daily, 1)
redis.call('HINCRBY', KEYS[4], daily, 1)
redis.call('HINCRBY', KEYS[5], interval, 1)
if safe then
    for i=6,7 do redis.call('HINCRBY', KEYS[i], interval, 1) end
    if late then
        local version = redis.call('HINCRBY', KEYS[12], ARGV[12], 1)
        redis.call('HSET', KEYS[11], ARGV[12], version)
        redis.call('SADD', KEYS[15], ARGV[8])
    end
else
    redis.call('HSET', KEYS[13], ARGV[12], 'hourly data missing, expired or already quarantined')
    redis.call('HSET', KEYS[14], ARGV[1], ARGV[11])
end
redis.call('HSET', KEYS[1], 'updated_at', ARGV[7])
redis.call('SADD', KEYS[8], ARGV[8])
redis.call('EXPIRE', KEYS[2], 86400)
redis.call('EXPIRE', KEYS[3], 7776000)
redis.call('EXPIRE', KEYS[4], 7776000)
redis.call('EXPIRE', KEYS[5], ARGV[9])
if safe then
    redis.call('EXPIREAT', KEYS[6], ARGV[14])
    redis.call('EXPIREAT', KEYS[7], ARGV[14])
end
redis.call('LPUSH', KEYS[9], ARGV[11])
redis.call('LTRIM', KEYS[9], 0, 1999)
redis.call('HSET', KEYS[10], ARGV[1], ARGV[2])
if not safe then return 2 end
return 1
"""


class EventIdConflict(ValueError):
    """同一 ID 被用于不同事件。"""


async def apply_event(
    event_type: str, device_id: str, occurred_at: Optional[datetime] = None,
    event_id: Optional[str] = None,
) -> bool:
    """原子去重并更新全部统计；返回是否首次应用。旧客户端不带 ID 时维持原有语义。"""
    delta = EVENT_DELTA.get(event_type)
    if delta is None:
        raise ValueError("invalid event_type")
    event_time = occurred_at or datetime.now()
    identifier = event_id or str(uuid4())
    # 同 ID 不允许用于不同内容，避免错误配置或冲突掩盖漏计。
    identity = json.dumps([device_id, event_type, event_time.isoformat()], ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(identity.encode()).hexdigest()
    current = next(k for k in delta if k in _CURRENT_FIELDS)
    daily = next(k for k in delta if k in _DAILY_FIELDS)
    from ...common.business_rules import get_rule
    series_length = int(get_rule("prediction", "series_length", default=settings.prediction_series_length))
    interval_minutes = int(get_rule("prediction", "interval_minutes", default=settings.prediction_interval_minutes))
    interval_ttl = (series_length + 10) * interval_minutes * 60
    timestamp = datetime.now(timezone.utc).isoformat()
    record = json.dumps({"event_id": event_id, "device_id": device_id, "event_type": event_type,
                         "occurred_at": event_time.isoformat(), "created_at": timestamp}, ensure_ascii=False)
    local_now = datetime.now(event_time.tzinfo)
    event_hour = event_time.replace(minute=0, second=0, microsecond=0)
    late = event_hour < local_now.replace(minute=0, second=0, microsecond=0)
    expires_at = (event_hour + timedelta(hours=1, days=settings.hourly_retention_days)).timestamp()
    keys = [_CUR_KEY, _device_key(device_id), _daily_key(event_time.date()),
            _device_daily_key(device_id, event_time.date()), _interval_key(event_time),
            _hourly_key(device_id, event_time), _hourly_all_key(event_time), _DEVICES_KEY,
            _EVENTS_KEY, f"{settings.redis_prefix}:event:dedup",
            f"{settings.redis_prefix}:archive:dirty", f"{settings.redis_prefix}:archive:versions",
            f"{settings.redis_prefix}:archive:quarantine", f"{settings.redis_prefix}:archive:quarantine_events",
            f"{settings.redis_prefix}:archive:dirty_devices:{event_hour:%Y%m%d%H}"]
    result = await get_redis().eval(_APPLY_EVENT_LUA, len(keys), *keys, identifier, digest,
                                    current, daily, _FIELD_MAP[daily], delta[current], timestamp,
                                    device_id, interval_ttl, settings.hourly_retention_days * 86400, record,
                                    event_hour.strftime("%Y%m%d%H"), int(late), int(expires_at), int(local_now.timestamp()))
    if result == -1:
        raise EventIdConflict("event_id already used for another event")
    if result == 2:
        from ...common.logger import logger
        logger.error(f"迟到事件 {identifier} 的小时 {event_hour:%Y%m%d%H} 缺少完整数据，已隔离，必须人工对账")
    return result in (1, 2)


async def list_events(limit: int = 100) -> list[dict]:
    """读取最近 N 条越线事件历史 (供前端"最近事件"展示与审计)."""
    if limit <= 0:
        return []
    redis = get_redis()
    raw_list = await redis.lrange(_EVENTS_KEY, 0, limit - 1)
    events: list[dict] = []
    for raw in raw_list:
        try:
            events.append(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            continue
    return events


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
    cur, daily = await pipe.execute()

    # 活跃设备数 = 设备表中 status 为 online/running 的当前在线设备
    # (旧口径统计的是"产生过检测事件的设备"集合, 空场景在线设备不入集且含历史残留, 语义失真)
    active = 0
    async for key in redis.scan_iter(f"{_DEVICE_HASH_PREFIX}*"):
        st = await redis.hget(key, "status")
        if st in ("online", "running"):
            active += 1

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


async def reset_current(device_id: Optional[str] = None) -> None:
    """重置当前计数 (设备重启/校准时调用).

    device_id 为空: 清全局 current + 全部逐设备在場人数;
    指定 device_id: 仅清该设备在場人数 (全局 key 无法安全扣减, 不动).
    """
    redis = get_redis()
    if device_id:
        await redis.delete(_device_key(device_id))
        return
    await redis.delete(_CUR_KEY)
    # 逐设备在場 key 一并清理 (sc:realtime:device:{id}); 日累计 :daily: 属流量账, 不动
    async for key in redis.scan_iter(f"{settings.redis_prefix}:realtime:device:*"):
        name = key if isinstance(key, str) else key.decode()
        if ":daily:" in name:
            continue
        await redis.delete(name)


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


async def get_device_stats(device_id: str) -> dict:
    """读取单个设备的实时统计 (当前在场 + 今日累计 + 当前小时内车流/人流)."""
    redis = get_redis()
    today = date.today()
    now = datetime.now()
    pipe = redis.pipeline()
    pipe.hgetall(_device_key(device_id))
    pipe.hgetall(_device_daily_key(device_id, today))
    pipe.hgetall(_hourly_key(device_id, now))  # 当前小时累计
    cur, daily, hourly = await pipe.execute()

    def _i(v) -> int:
        return int(v) if v else 0

    return {
        "device_id": device_id,
        "current_vehicles": max(0, _i(cur.get("current_vehicles"))),
        "current_persons": max(0, _i(cur.get("current_persons"))),
        "today_vehicle_in": _i(daily.get("today_vehicle_in")),
        "today_vehicle_out": _i(daily.get("today_vehicle_out")),
        "today_person_in": _i(daily.get("today_person_in")),
        "today_person_out": _i(daily.get("today_person_out")),
        "hour": now.hour,
        "hour_vehicle_in": _i(hourly.get("vehicle_in")),
        "hour_vehicle_out": _i(hourly.get("vehicle_out")),
        "hour_person_in": _i(hourly.get("person_in")),
        "hour_person_out": _i(hourly.get("person_out")),
    }


async def get_all_device_stats() -> list[dict]:
    """读取所有活跃设备的实时统计 (当前在场 + 今日累计 + 当前小时内车流/人流).

    仅返回产生过事件的设备 (_DEVICES_KEY); 未产生事件的设备不在此列.
    """
    redis = get_redis()
    device_ids = await redis.smembers(_DEVICES_KEY)
    if not device_ids:
        return []
    today = date.today()
    now = datetime.now()
    pipe = redis.pipeline()
    id_list: list[str] = []
    for did in device_ids:
        did_str = did if isinstance(did, str) else did.decode()
        id_list.append(did_str)
        pipe.hgetall(_device_key(did_str))
        pipe.hgetall(_device_daily_key(did_str, today))
        pipe.hgetall(_hourly_key(did_str, now))  # 当前小时累计
    results = await pipe.execute()

    def _i(v) -> int:
        return int(v) if v else 0

    output: list[dict] = []
    for i, did_str in enumerate(id_list):
        cur = results[i * 3]
        daily = results[i * 3 + 1]
        hourly = results[i * 3 + 2]
        output.append({
            "device_id": did_str,
            "current_vehicles": max(0, _i(cur.get("current_vehicles"))),
            "current_persons": max(0, _i(cur.get("current_persons"))),
            "today_vehicle_in": _i(daily.get("today_vehicle_in")),
            "today_vehicle_out": _i(daily.get("today_vehicle_out")),
            "today_person_in": _i(daily.get("today_person_in")),
            "today_person_out": _i(daily.get("today_person_out")),
            "hour": now.hour,
            "hour_vehicle_in": _i(hourly.get("vehicle_in")),
            "hour_vehicle_out": _i(hourly.get("vehicle_out")),
            "hour_person_in": _i(hourly.get("person_in")),
            "hour_person_out": _i(hourly.get("person_out")),
        })
    return output

