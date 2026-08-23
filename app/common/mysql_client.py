"""MySQL 异步客户端 (长期归档: 小时级车流/人流量报表).

仅在 settings.mysql_enabled=True 时启用; 连接池懒加载, 服务关闭时统一释放.
表按 device_id+stat_date+hour 幂等覆盖写入, 供长期报表跨天/跨月查询.
"""
import asyncio
from datetime import date
from typing import Optional

import aiomysql

from .config import settings
from .logger import logger

_TABLE = "hourly_traffic"

_DDL = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    device_id VARCHAR(128) NOT NULL COMMENT '设备ID',
    stat_date DATE NOT NULL COMMENT '统计日期',
    hour TINYINT UNSIGNED NOT NULL COMMENT '小时 0-23',
    vehicle_in INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '车辆进入',
    vehicle_out INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '车辆离开',
    person_in INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '人员进入',
    person_out INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '人员离开',
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_device_hour (device_id, stat_date, hour)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='小时级车流/人流量归档'
"""

_pool: Optional[aiomysql.Pool] = None
_pool_lock = asyncio.Lock()


async def get_pool() -> aiomysql.Pool:
    """获取 MySQL 连接池 (懒加载, 加锁单飞, 避免并发重复建池)."""
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                _pool = await aiomysql.create_pool(
                    host=settings.mysql_host,
                    port=settings.mysql_port,
                    user=settings.mysql_user,
                    password=settings.mysql_password,
                    db=settings.mysql_database,
                    charset="utf8mb4",
                    autocommit=True,
                    minsize=1,
                    maxsize=5,
                    pool_recycle=3600,
                )
    return _pool


async def ensure_table() -> None:
    """建表 (幂等, 启动/首次归档时调用)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_DDL)


async def upsert_hourly(rows: list[tuple]) -> None:
    """批量写入小时数据 (device_id+stat_date+hour 冲突则覆盖计数).

    rows 元素: (device_id, stat_date_str, hour, vehicle_in, vehicle_out, person_in, person_out)
    """
    if not rows:
        return
    pool = await get_pool()
    sql = f"""
    INSERT INTO {_TABLE}
        (device_id, stat_date, hour, vehicle_in, vehicle_out, person_in, person_out)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    ON DUPLICATE KEY UPDATE
        vehicle_in = VALUES(vehicle_in),
        vehicle_out = VALUES(vehicle_out),
        person_in = VALUES(person_in),
        person_out = VALUES(person_out)
    """
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.executemany(sql, rows)


async def query_hourly(
    device_id: Optional[str],
    start_date: date,
    start_hour: int,
    end_date: date,
    end_hour: int,
) -> list[dict]:
    """查询长期小时数据 (长期报表), 支持天+时精确范围 [start_date:start_hour, end_date:end_hour].

    - 指定 device_id: 返回该设备每小时数据
    - 不指定: 按日期+小时聚合所有设备 (全局汇总, 等价于 Redis 的 hourly:all)

    start_date 缺省小时取 0, end_date 缺省小时取 23 即按整日查询 (兼容旧用法).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            # 复合过滤: stat_date 跨天 + 边界日按 hour 截取 (闭区间)
            cond = (
                "(stat_date > %s OR (stat_date = %s AND hour >= %s)) "
                "AND (stat_date < %s OR (stat_date = %s AND hour <= %s))"
            )
            args = (start_date, start_date, start_hour, end_date, end_date, end_hour)
            if device_id:
                sql = f"""
                SELECT stat_date, hour, vehicle_in, vehicle_out, person_in, person_out
                FROM {_TABLE}
                WHERE device_id = %s AND {cond}
                ORDER BY stat_date, hour
                """
                await cur.execute(sql, (device_id, *args))
            else:
                sql = f"""
                SELECT stat_date, hour,
                       SUM(vehicle_in) AS vehicle_in,
                       SUM(vehicle_out) AS vehicle_out,
                       SUM(person_in) AS person_in,
                       SUM(person_out) AS person_out
                FROM {_TABLE}
                WHERE {cond}
                GROUP BY stat_date, hour
                ORDER BY stat_date, hour
                """
                await cur.execute(sql, args)
            return await cur.fetchall()


async def prune_old(before: date) -> None:
    """删除早于给定日期的归档数据 (控制长期存储成本)."""
    pool = await get_pool()
    sql = f"DELETE FROM {_TABLE} WHERE stat_date < %s"
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (before,))


async def close_mysql() -> None:
    """关闭连接池 (服务关闭时调用)."""
    global _pool
    if _pool is not None:
        _pool.close()
        await _pool.wait_closed()
        _pool = None
        logger.info("MySQL 连接池已关闭")
