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
    if first > last_completed:
        return  # 没有新的已完成小时

    # 限制回填量, 防止宕机过久后一次补写过多
    if (last_completed - first) > timedelta(hours=_MAX_BACKFILL_HOURS):
        first = last_completed - timedelta(hours=_MAX_BACKFILL_HOURS - 1)

    hour = first
    while hour <= last_completed:
        await _archive_hour(redis, hour)
        await redis.set(_MARKER_KEY, hour.strftime("%Y%m%d%H"))
        hour += timedelta(hours=1)


async def _archive_hour(redis, hour: datetime) -> None:
    """归档单个已完成小时的设备数据到 MySQL."""
    from ...common.mysql_client import upsert_hourly

    hour_str = hour.strftime("%Y%m%d%H")
    global_key = _hourly_all_key(hour)
    rows: list[tuple] = []
    # 匹配 sc:hourly:*:{YYYYMMDDHH} (含设备与全局 key; 全局 key 单独跳过)
    async for key in redis.scan_iter(f"{_HOURLY_PREFIX}*:{hour_str}"):
        if key == global_key:
            continue  # 全局汇总可由 MySQL SUM 推导
        data = await redis.hgetall(key)
        if not data:
            continue
        device_id = key[len(_HOURLY_PREFIX):].rsplit(":", 1)[0]
        rows.append((
            device_id,
            hour.date().isoformat(),
            hour.hour,
            int(data.get("vehicle_in", 0) or 0),
            int(data.get("vehicle_out", 0) or 0),
            int(data.get("person_in", 0) or 0),
            int(data.get("person_out", 0) or 0),
        ))
    if rows:
        await upsert_hourly(rows)
        logger.info(f"[归档] {hour_str} 设备 {len(rows)} 条 -> MySQL")
