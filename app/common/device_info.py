"""设备点位信息 (名称/经纬度/分类/点位编号) — MySQL 存储 + 内存缓存.

设备元信息 (设备名称 -> 点位分类/经纬度/point_id) 由 data/device_geo.json 与
data/device_category.json (源自 data/设备信息汇总表.xlsx, 已提前固化到 JSON)
迁入 MySQL device_info 表, 供运维页面增删改查; 展示链路 (设备列表/统计/WebSocket)
通过 geo_by_name / category_by_name 读取内存缓存, 热路径无 SQL.

- 启动: ensure_table -> initialize_if_empty (表空时: JSON 种子导入, 排除云冈类) -> reload_cache
- 运行时: CRUD 接口写 MySQL 后同步刷新缓存, 展示链路立即生效
- MySQL 未启用/不可用时缓存为空, 展示链路回落旧 JSON 文件 (兼容本地开发, 只读不写)
"""
import json
from pathlib import Path
from typing import Optional

import aiomysql

from .logger import logger
from .mysql_client import get_pool

_TABLE = "device_info"

_DDL = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id INT UNSIGNED NOT NULL AUTO_INCREMENT,
    name VARCHAR(255) NOT NULL COMMENT '设备名称(去空格, 唯一)',
    point_id VARCHAR(64) NULL COMMENT '点位编号(如 GAJK-2648)',
    category VARCHAR(64) NULL COMMENT '点位分类',
    longitude DECIMAL(10,6) NULL COMMENT '经度',
    latitude DECIMAL(10,6) NULL COMMENT '纬度',
    status VARCHAR(16) NULL COMMENT '验证状态(已验证/待验证)',
    region VARCHAR(64) NULL COMMENT '区域(如 大同古城)',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='设备点位信息(名称/经纬度/分类/区域)'
"""

# 区域默认值 (与前端顶栏区域选项对应)
_REGION_DEFAULT = "大同古城"

# 内存缓存: {归一化名称: {point_id, category, longitude, latitude, status, region}}
_cache: dict[str, dict] = {}

# 云冈类设备 (设备信息汇总表.xlsx 中"类=云冈类", 现为 JTKK 卡口, 无经纬度) —
# 暂不迁入 MySQL: 点位编号以 JTKK 开头 或 名称含"云冈"者一律排除.
_YUN_GANG_POINT_PREFIX = "JTKK"
_YUN_GANG_KEYWORDS = ("云冈",)

# 种子数据源 (源自 data/设备信息汇总表.xlsx, 已提前固化到 JSON)
_GEO_FILE = Path(__file__).resolve().parents[2] / "data" / "device_geo.json"
_CATEGORY_FILE = Path(__file__).resolve().parents[2] / "data" / "device_category.json"


def normalize(name: str) -> str:
    """名称归一化: 去空格 (与 geo_by_name 匹配逻辑一致)."""
    return (name or "").replace(" ", "")


def is_yungang(name: str, point_id: Optional[str] = None) -> bool:
    """是否"云冈类"设备 (暂不迁入 MySQL).

    云冈类 = 设备信息汇总表.xlsx 中"类=云冈类"的 JTKK 卡口 (无经纬度):
    点位编号以 JTKK 开头, 或名称以 JTKK 开头, 或名称含"云冈".
    """
    norm = normalize(name)
    if point_id and str(point_id).upper().startswith(_YUN_GANG_POINT_PREFIX):
        return True
    if norm.upper().startswith(_YUN_GANG_POINT_PREFIX):
        return True
    return any(k in norm for k in _YUN_GANG_KEYWORDS)


async def ensure_table() -> None:
    """建表 (幂等), 并对旧表补齐 region 列 (迁移)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_DDL)
            # 旧表无 region 列: 补列并把已有数据区域默认置为"大同古城"
            await cur.execute(
                "SELECT COUNT(*) FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = 'region'",
                (_TABLE,),
            )
            row = await cur.fetchone()
            if not row or int(row[0]) == 0:
                await cur.execute(f"ALTER TABLE {_TABLE} ADD COLUMN region VARCHAR(64) NULL COMMENT '区域(如 大同古城)' AFTER status")
                await cur.execute(f"UPDATE {_TABLE} SET region = %s WHERE region IS NULL", (_REGION_DEFAULT,))


async def count() -> int:
    """当前表内设备信息条数."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(f"SELECT COUNT(*) FROM {_TABLE}")
            row = await cur.fetchone()
    return int(row[0]) if row else 0


def get(name: str) -> Optional[dict]:
    """按名称查设备信息 (去空格匹配), 未匹配返回 None. 同步读内存缓存 (热路径)."""
    return _cache.get(normalize(name))


def geo_map() -> dict[str, tuple[float, float]]:
    """返回 {归一化名称: (经度, 纬度)} 快照 (警力就近归区等使用)."""
    return {
        n: (r["longitude"], r["latitude"])
        for n, r in _cache.items()
        if r["longitude"] is not None and r["latitude"] is not None
    }


def cache_size() -> int:
    return len(_cache)


async def reload_cache() -> None:
    """全量加载 MySQL device_info 表到内存缓存."""
    global _cache
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                f"SELECT name, point_id, category, longitude, latitude, status, region FROM {_TABLE}"
            )
            rows = await cur.fetchall()
    _cache = {}
    for r in rows:
        try:
            longitude = float(r["longitude"]) if r["longitude"] is not None else None
            latitude = float(r["latitude"]) if r["latitude"] is not None else None
        except (TypeError, ValueError):
            longitude = latitude = None
        _cache[r["name"]] = {
            "point_id": r["point_id"] or None,
            "category": r["category"] or None,
            "longitude": longitude,
            "latitude": latitude,
            "status": r["status"] or None,
            "region": r["region"] or _REGION_DEFAULT,
        }
    logger.info(f"设备信息表已加载 {len(_cache)} 条到内存缓存")


async def fetch(name: str) -> Optional[dict]:
    """从 MySQL 查单条 (运维接口用, 不依赖缓存). 返回 None 表示不存在."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                f"SELECT id, name, point_id, category, longitude, latitude, status, region FROM {_TABLE} "
                f"WHERE name = %s",
                (normalize(name),),
            )
            return await cur.fetchone()


async def upsert(
    name: str,
    point_id: Optional[str] = None,
    category: Optional[str] = None,
    longitude: Optional[float] = None,
    latitude: Optional[float] = None,
    status: Optional[str] = None,
    region: Optional[str] = None,
) -> str:
    """新增/更新一条设备信息 (name 唯一, 冲突则覆盖), 并同步内存缓存. 返回归一化名称."""
    norm = normalize(name)
    if not norm:
        raise ValueError("设备名称不能为空")
    region = region or _REGION_DEFAULT
    pool = await get_pool()
    sql = f"""
    INSERT INTO {_TABLE} (name, point_id, category, longitude, latitude, status, region)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    ON DUPLICATE KEY UPDATE
        point_id = VALUES(point_id),
        category = VALUES(category),
        longitude = VALUES(longitude),
        latitude = VALUES(latitude),
        status = VALUES(status),
        region = VALUES(region)
    """
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                sql,
                (norm, point_id or None, category or None, longitude, latitude, status or None, region),
            )
    _cache[norm] = {
        "point_id": point_id or None,
        "category": category or None,
        "longitude": longitude,
        "latitude": latitude,
        "status": status or None,
        "region": region,
    }
    return norm


async def delete(name: str) -> bool:
    """删除一条设备信息, 并同步内存缓存. 返回是否实际删除."""
    norm = normalize(name)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(f"DELETE FROM {_TABLE} WHERE name = %s", (norm,))
            affected = cur.rowcount
    if affected:
        _cache.pop(norm, None)
    return bool(affected)


async def list_all() -> list[dict]:
    """返回全部设备信息 (运维页面表格用)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                f"SELECT id, name, point_id, category, longitude, latitude, status, region, updated_at "
                f"FROM {_TABLE} ORDER BY id"
            )
            rows = await cur.fetchall()
    result = []
    for r in rows:
        row = dict(r)
        row["longitude"] = float(row["longitude"]) if row["longitude"] is not None else None
        row["latitude"] = float(row["latitude"]) if row["latitude"] is not None else None
        result.append(row)
    return result


def list_cached() -> list[dict]:
    """返回内存缓存全量设备信息 (供 stats/WS 按注册设备全量推送).

    热路径无 SQL; MySQL 未启用/表空时返回空列表 (调用方回落 Redis 设备集合).
    """
    return [
        {
            "name": n,
            "point_id": r["point_id"],
            "category": r["category"],
            "longitude": r["longitude"],
            "latitude": r["latitude"],
            "status": r["status"],
            "region": r["region"] or _REGION_DEFAULT,
        }
        for n, r in _cache.items()
    ]


def is_registered(name: str) -> bool:
    """是否已在 device_info 表注册 (去空格匹配, WVP 启流选取用)."""
    return normalize(name) in _cache


async def seed_from_json_if_empty() -> int:
    """设备信息表为空时, 从 data/device_geo.json + device_category.json 种子导入.

    经纬度/状态来自 device_geo.json, 分类/点位编号来自 device_category.json
    (值形如 {"category": ..., "point_id": ...}, 兼容旧版字符串值).
    仅导入"古城类"设备 (有经纬度者), 排除"云冈类"(JTKK) 设备.
    返回导入条数; 表已有数据则跳过 (保留运维页面人工编辑, 不覆盖).
    """
    if await count() > 0:
        return 0
    try:
        geo_raw = json.loads(_GEO_FILE.read_text(encoding="utf-8")).get("devices", {})
    except (json.JSONDecodeError, OSError) as e:  # noqa: BLE001
        logger.warning(f"设备信息种子导入: 读取 device_geo.json 失败: {e}")
        return 0
    try:
        cat_map = json.loads(_CATEGORY_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):  # noqa: BLE001
        cat_map = {}
    imported = 0
    for name, d in geo_raw.items():
        norm = normalize(name)
        if not norm or is_yungang(norm, d.get("point_id")):
            continue  # 云冈类设备暂不迁入
        cat_entry = cat_map.get(norm)
        category = cat_entry.get("category") if isinstance(cat_entry, dict) else cat_entry
        point_id = cat_entry.get("point_id") if isinstance(cat_entry, dict) else None
        await upsert(
            norm,
            point_id=point_id or d.get("point_id") or None,
            category=category or None,
            longitude=d.get("longitude"),
            latitude=d.get("latitude"),
            status=d.get("status") or None,
        )
        imported += 1
    logger.info(f"设备信息种子导入完成: {imported} 条 (已排除云冈类设备)")
    return imported


async def initialize_if_empty() -> int:
    """设备信息表为空时统一初始化 (幂等), 返回导入的设备条数.

    数据源 (源自 data/设备信息汇总表.xlsx, 已提前固化到 JSON):
      device_geo.json (名称/经纬度/状态) + device_category.json (分类/点位编号)
      种子导入, 排除云冈类设备. 运行时不再依赖 xlsx/openpyxl, 更新设备数据
      只需改 JSON 后重启容器 (data 目录 bind mount, 无需重建镜像).
    表已有数据时直接跳过 (运维 CRUD 已产生的数据不受影响).
    """
    if await count() > 0:
        logger.info("设备信息表已有数据, 跳过初始化")
        return 0
    return await seed_from_json_if_empty()
