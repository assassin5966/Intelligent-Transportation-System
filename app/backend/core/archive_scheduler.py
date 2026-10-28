"""MySQL 长期归档调度器.

定期把 Redis 中"已完成小时"的车流/人流量累计 (sc:hourly:{device_id}:{YYYYMMDDHH})
归档到 MySQL 的 hourly_traffic 表, 支撑跨 Redis 30 天保留窗口的长期报表.

- 只归档严格早于当前进行中小时的数据 (已完成的小时, 避免写未定稿数据)
- 归档进度用 Redis 标记 sc:archive:last_hour 记录, 重启/停机后自动补中间缺失小时
- 写入按 device_id+stat_date+hour 幂等 (upsert), 重复归档安全
- MySQL 端 SUM 可推导全局汇总, 故不单独落库 hourly:all
- 每日至多一次清理 MySQL 中超过 mysql_retention_days 的历史数据
"""
import asyncio
from datetime import datetime, timedelta
from typing import Optional

from ...common.config import settings
from ...common.logger import logger
from ...common.redis_client import get_redis
from ..core.realtime import _hourly_all_key

_task: Optional[asyncio.Task] = None
_stop = asyncio.Event()

# 归档进度标记 (存已归档的最大小时 YYYYMMDDHH)
_MARKER_KEY = f"{settings.redis_prefix}:archive:last_hour"
# 清理进度标记 (存上次清理日期 YYYY-MM-DD, 每日至多一次)
_PRUNE_MARKER_KEY = f"{settings.redis_prefix}:archive:last_prune"
# 单轮最多回填的小时数 (防止宕机过久后一次性补写过多)
_MAX_BACKFILL_HOURS = 24 * 7

_HOURLY_PREFIX = f"{settings.redis_prefix}:hourly:"
_DIRTY_KEY = f"{settings.redis_prefix}:archive:dirty"
_QUARANTINE_KEY = f"{settings.redis_prefix}:archive:quarantine"
_DIRTY_DEVICES_PREFIX = f"{settings.redis_prefix}:archive:dirty_devices:"

_ACK_DIRTY_LUA = """
if redis.call('HEXISTS', KEYS[2], ARGV[1]) == 0
   and redis.call('HGET', KEYS[1], ARGV[1]) == ARGV[2] then
    redis.call('HDEL', KEYS[1], ARGV[1])
    redis.call('DEL', KEYS[3])
    return 1
end
return 0
"""


async def start_scheduler() -> None:
    """启动归档调度器 (MYSQL_ENABLED=false 时跳过)."""
    global _task
    if not settings.mysql_enabled:
        logger.info("MySQL 归档已禁用 (MYSQL_ENABLED=false), 跳过")
        return
    if _task is not None:
        return
    _stop.clear()
    _task = asyncio.create_task(_loop())
    logger.info(
        f"MySQL 归档调度器已启动 (周期 {settings.archive_interval_seconds}s, "
        f"保留 {settings.mysql_retention_days} 天)"
    )


async def stop_scheduler() -> None:
    """停止归档调度器."""
    global _task
    if _task is None:
        return
    _stop.set()
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    _task = None
    logger.info("MySQL 归档调度器已停止")


async def _loop() -> None:
    while not _stop.is_set():
        try:
            await _archive_once()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"MySQL 归档失败: {e}")
        await asyncio.sleep(settings.archive_interval_seconds)


async def _archive_once() -> None:
    from ...common.mysql_client import ensure_table, prune_old, upsert_hourly

    await ensure_table()
    redis = get_redis()

    now = datetime.now()
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    last_completed = current_hour - timedelta(hours=1)

    # 定期清理 MySQL 超过保留天数的数据 (每日至多一次)
    today = now.date()
    last_prune = await redis.get(_PRUNE_MARKER_KEY)
    if last_prune != today.isoformat():
        await prune_old(today - timedelta(days=settings.mysql_retention_days))
        await redis.set(_PRUNE_MARKER_KEY, today.isoformat())
        logger.info(f"[归档] 已清理 {settings.mysql_retention_days} 天前的历史数据")

    # 确定待归档小时区间: 上次归档之后 -> 上一个已完成小时
    last_raw = await redis.get(_MARKER_KEY)
    if last_raw:
        try:
            last_hour = datetime.strptime(last_raw, "%Y%m%d%H")
        except ValueError:
            last_hour = last_completed - timedelta(hours=1)
    else:
        # 无标记: 从上一个已完成小时开始, 不一次性回填历史
        last_hour = last_completed - timedelta(hours=1)

    first = last_hour + timedelta(hours=1)
    # 限制回填量, 防止宕机过久后一次补写过多
    if (last_completed - first) > timedelta(hours=_MAX_BACKFILL_HOURS):
        first = last_completed - timedelta(hours=_MAX_BACKFILL_HOURS - 1)

    hour = first
    while hour <= last_completed:
        await _archive_hour(redis, hour)
        await redis.set(_MARKER_KEY, hour.strftime("%Y%m%d%H"))
        hour += timedelta(hours=1)
    # 即使 marker 已推进或没有新小时，也必须重试已归档小时的迟到事件。
    await _archive_dirty(redis, current_hour)


async def _archive_dirty(redis, current_hour: datetime) -> None:
    async for hour_str, version in redis.hscan_iter(_DIRTY_KEY):
        try:
            hour = datetime.strptime(hour_str, "%Y%m%d%H")
        except ValueError:
            logger.error(f"[归档] 待重归档小时格式错误: {hour_str}")
            continue
        if hour >= current_hour:
            continue
        await _archive_hour(redis, hour, expected_version=version)


async def _archive_hour(redis, hour: datetime, expected_version: str | None = None) -> None:
    """归档单个小时；只有版本未变且完整时才确认迟到修正。"""
    from ...common.mysql_client import upsert_hourly

    hour_str = hour.strftime("%Y%m%d%H")
    if expected_version is None:
        # 普通归档与迟到修正可能重叠，使用同一完整性检查及版本确认。
        expected_version = await redis.hget(_DIRTY_KEY, hour_str)
    devices_key = f"{_DIRTY_DEVICES_PREFIX}{hour_str}"
    if await redis.hexists(_QUARANTINE_KEY, hour_str):
        logger.error(f"[归档] {hour_str} 已隔离，等待人工对账")
        return
    if hour + timedelta(hours=1, days=settings.hourly_retention_days) <= datetime.now():
        await redis.hset(_QUARANTINE_KEY, hour_str, "hourly retention expired")
        logger.error(f"[归档] {hour_str} 已超过 Redis 保留期，不覆盖 MySQL，等待人工对账")
        return
    affected = await redis.smembers(devices_key) if expected_version is not None else set()
    if expected_version is not None and not affected:
        await redis.hset(_QUARANTINE_KEY, hour_str, "dirty device set missing")
        logger.error(f"[归档] {hour_str} 缺失迟到设备清单，等待人工对账")
        return
    for device in affected:
        key = f"{_HOURLY_PREFIX}{device}:{hour_str}"
        if await redis.ttl(key) <= 0:
            await redis.hset(_QUARANTINE_KEY, hour_str, "dirty device hourly data missing")
            logger.error(f"[归档] {hour_str} 设备 {device} 数据缺失，等待人工对账")
            return
    if expected_version is not None and await redis.ttl(_hourly_all_key(hour)) <= 0:
        await redis.hset(_QUARANTINE_KEY, hour_str, "global hourly data missing")
        logger.error(f"[归档] {hour_str} 全局小时数据缺失，等待人工对账")
        return
    global_key = _hourly_all_key(hour)
    rows: list[tuple] = []
    seen_devices: set[str] = set()
    # 匹配 sc:hourly:*:{YYYYMMDDHH} (含设备与全局 key; 全局 key 单独跳过)
    async for key in redis.scan_iter(f"{_HOURLY_PREFIX}*:{hour_str}"):
        if key == global_key:
            continue  # 全局汇总可由 MySQL SUM 推导
        data = await redis.hgetall(key)
        if not data:
            continue
        device_id = key[len(_HOURLY_PREFIX):].rsplit(":", 1)[0]
        seen_devices.add(device_id)
        rows.append((
            device_id,
            hour.date().isoformat(),
            hour.hour,
            int(data.get("vehicle_in", 0) or 0),
            int(data.get("vehicle_out", 0) or 0),
            int(data.get("person_in", 0) or 0),
            int(data.get("person_out", 0) or 0),
        ))
    if expected_version is not None:
        if (not affected.issubset(seen_devices)
                or await redis.ttl(global_key) <= 0
                or hour + timedelta(hours=1, days=settings.hourly_retention_days) <= datetime.now()):
            await redis.hset(_QUARANTINE_KEY, hour_str, "hourly data disappeared during archive")
            logger.error(f"[归档] {hour_str} 读取期间数据过期或缺失，等待人工对账")
            return
        for device in affected:
            if await redis.ttl(f"{_HOURLY_PREFIX}{device}:{hour_str}") <= 0:
                await redis.hset(_QUARANTINE_KEY, hour_str, "dirty device expired during archive")
                logger.error(f"[归档] {hour_str} 设备 {device} 读取期间过期，等待人工对账")
                return
    if rows:
        # 扫描期间若出现隔离，禁止用不完整快照覆盖已有 MySQL 数据。
        if await redis.hexists(_QUARANTINE_KEY, hour_str):
            return
        await upsert_hourly(rows)
        logger.info(f"[归档] {hour_str} 设备 {len(rows)} 条 -> MySQL")
    if expected_version is not None:
        await redis.eval(_ACK_DIRTY_LUA, 3, _DIRTY_KEY, _QUARANTINE_KEY, devices_key,
                         hour_str, str(expected_version))
