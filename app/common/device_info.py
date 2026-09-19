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
    entrance_type VARCHAR(16) NULL COMMENT '出入口类型(入口/出口/出入口, 由 category 派生)',
    point_type VARCHAR(16) NULL COMMENT '点位类型(便道/车辆卡口, 由 category 派生)',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='设备点位信息(名称/经纬度/分类/区域)'
"""

# 区域默认值 (与前端顶栏区域选项对应)
_REGION_DEFAULT = "大同古城"

# 内存缓存: {归一化名称: {point_id, category, longitude, latitude, status, region,
#                         entrance_type, point_type}}
_cache: dict[str, dict] = {}

# 短名 -> 白名单全名 的解析缓存 (见 _resolve_key); 缓存增删改时清空
_alias: dict[str, str] = {}

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


def derive_entrance_type(category: Optional[str]) -> Optional[str]:
    """从点位分类派生出入口类型: 含"出入口"→出入口, 含"入口"→入口, 含"出口"→出口.

    注意判定顺序: "城墙出入口便道监控点位"同时含"出入口"与"入口", 需先判"出入口".
    """
    cat = category or ""
    if "出入口" in cat:
        return "出入口"
    if "入口" in cat:
        return "入口"
    if "出口" in cat:
        return "出口"
    return None


def derive_point_type(category: Optional[str]) -> Optional[str]:
    """从点位分类派生点位类型: 含"便道"→便道, 含"卡口"→车辆卡口."""
    cat = category or ""
    if "便道" in cat:
        return "便道"
    if "卡口" in cat:
        return "车辆卡口"
    return None


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


async def _has_column(cur, column: str) -> bool:
    """表是否已有该列 (旧表迁移前检查)."""
    await cur.execute(
        "SELECT COUNT(*) FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
        (_TABLE, column),
    )
    row = await cur.fetchone()
    return bool(row and int(row[0]) > 0)


async def ensure_table() -> None:
    """建表 (幂等), 并对旧表补齐 region / 出入口类型 / 点位类型列 (迁移)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_DDL)
            # 旧表无 region 列: 补列并把已有数据区域默认置为"大同古城"
            if not await _has_column(cur, "region"):
                await cur.execute(f"ALTER TABLE {_TABLE} ADD COLUMN region VARCHAR(64) NULL COMMENT '区域(如 大同古城)' AFTER status")
                await cur.execute(f"UPDATE {_TABLE} SET region = %s WHERE region IS NULL", (_REGION_DEFAULT,))
            # 旧表无出入口类型/点位类型列: 补列并按 category 回填派生值 (规则同 derive_*)
            if not await _has_column(cur, "entrance_type"):
                await cur.execute(
                    f"ALTER TABLE {_TABLE} ADD COLUMN entrance_type VARCHAR(16) NULL "
                    "COMMENT '出入口类型(入口/出口/出入口, 由 category 派生)' AFTER region"
                )
                await cur.execute(
                    f"UPDATE {_TABLE} SET entrance_type = CASE "
                    "WHEN category LIKE '%出入口%' THEN '出入口' "
                    "WHEN category LIKE '%入口%' THEN '入口' "
                    "WHEN category LIKE '%出口%' THEN '出口' END "
                    "WHERE entrance_type IS NULL"
                )
            if not await _has_column(cur, "point_type"):
                await cur.execute(
                    f"ALTER TABLE {_TABLE} ADD COLUMN point_type VARCHAR(16) NULL "
                    "COMMENT '点位类型(便道/车辆卡口, 由 category 派生)' AFTER entrance_type"
                )
                await cur.execute(
                    f"UPDATE {_TABLE} SET point_type = CASE "
                    "WHEN category LIKE '%便道%' THEN '便道' "
                    "WHEN category LIKE '%卡口%' THEN '车辆卡口' END "
                    "WHERE point_type IS NULL"
                )


async def count() -> int:
    """当前表内设备信息条数."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(f"SELECT COUNT(*) FROM {_TABLE}")
            row = await cur.fetchone()
    return int(row[0]) if row else 0


def _resolve_key(norm: str) -> Optional[str]:
    """精确未命中时, 用"短名是白名单全名前缀"再匹配一次.

    device_info 的 name 取自白名单 (含通道后缀, 如 '...以东90米(球)041216'),
    而人工注册/运维页录入的设备名常不带后缀, 归一化后无法精确命中.
    仅当候选唯一时命中: 同一前缀对应多个通道 (如卡口的 A/B 车道) 时返回 None,
    避免落到错误的点位分类/经纬度.
    """
    if not norm:
        return None
    if norm in _alias:
        return _alias[norm]
    matches = [k for k in _cache if k.startswith(norm)]
    if len(matches) != 1:
        return None
    _alias[norm] = matches[0]
    return matches[0]


def get(name: str) -> Optional[dict]:
    """按名称查设备信息 (去空格匹配), 未匹配返回 None. 同步读内存缓存 (热路径).

    精确未命中时按前缀兼容短名 (见 _resolve_key).
    """
    norm = normalize(name)
    row = _cache.get(norm)
    if row is not None:
        return row
    key = _resolve_key(norm)
    return _cache.get(key) if key else None


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
                f"SELECT name, point_id, category, longitude, latitude, status, region, "
                f"entrance_type, point_type FROM {_TABLE}"
            )
            rows = await cur.fetchall()
    _cache = {}
    _alias.clear()
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
            # 列为空时按 category 兜底派生 (旧数据未回填也能正确展示)
            "entrance_type": r["entrance_type"] or derive_entrance_type(r["category"]),
            "point_type": r["point_type"] or derive_point_type(r["category"]),
        }
    logger.info(f"设备信息表已加载 {len(_cache)} 条到内存缓存")


async def fetch(name: str) -> Optional[dict]:
    """从 MySQL 查单条 (运维接口用, 不依赖缓存). 返回 None 表示不存在."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                f"SELECT id, name, point_id, category, longitude, latitude, status, region, "
                f"entrance_type, point_type FROM {_TABLE} "
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
    entrance_type: Optional[str] = None,
    point_type: Optional[str] = None,
) -> str:
    """新增/更新一条设备信息 (name 唯一, 冲突则覆盖), 并同步内存缓存. 返回归一化名称.

    出入口类型/点位类型未显式传入时按 category 自动派生; 显式传入则以此为准
    (运维页面手工改过的值不会被 category 覆盖).
    """
    norm = normalize(name)
    if not norm:
        raise ValueError("设备名称不能为空")
    region = region or _REGION_DEFAULT
    category = category or None
    entrance_type = entrance_type or derive_entrance_type(category)
    point_type = point_type or derive_point_type(category)
    pool = await get_pool()
    sql = f"""
    INSERT INTO {_TABLE} (name, point_id, category, longitude, latitude, status, region,
                          entrance_type, point_type)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON DUPLICATE KEY UPDATE
        point_id = VALUES(point_id),
        category = VALUES(category),
        longitude = VALUES(longitude),
        latitude = VALUES(latitude),
        status = VALUES(status),
        region = VALUES(region),
        entrance_type = VALUES(entrance_type),
        point_type = VALUES(point_type)
    """
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                sql,
                (norm, point_id or None, category, longitude, latitude, status or None, region,
                 entrance_type, point_type),
            )
    _cache[norm] = {
        "point_id": point_id or None,
        "category": category,
        "longitude": longitude,
        "latitude": latitude,
        "status": status or None,
        "region": region,
        "entrance_type": entrance_type,
        "point_type": point_type,
    }
    _alias.clear()  # 新增/改名后短名解析失效, 下次查询重建
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
        _alias.clear()
    return bool(affected)


async def list_all() -> list[dict]:
    """返回全部设备信息 (运维页面表格用)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                f"SELECT id, name, point_id, category, longitude, latitude, status, region, "
                f"entrance_type, point_type, updated_at "
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
            "entrance_type": r["entrance_type"],
            "point_type": r["point_type"],
        }
        for n, r in _cache.items()
    ]


def is_registered(name: str) -> bool:
    """是否已在 device_info 表注册 (去空格匹配, WVP 启流选取用; 兼容不带通道后缀的短名)."""
    return get(name) is not None


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
            region=d.get("region") or None,
            # JSON 显式给值则优先, 否则 upsert 按 category 派生
            entrance_type=d.get("entrance_type") or None,
            point_type=d.get("point_type") or None,
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
