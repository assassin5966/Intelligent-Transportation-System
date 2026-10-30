"""设备点位信息 (名称/经纬度/分类/点位编号) — MySQL 存储 + 内存缓存.

设备元信息 (设备名称 -> 点位分类/经纬度/point_id) 由 data/device_geo.json 与
data/device_category.json (源自 data/设备信息汇总表.xlsx, 已提前固化到 JSON)
迁入 MySQL device_info 表, 供运维页面增删改查; 展示链路 (设备列表/统计/WebSocket)
通过 geo_by_name / category_by_name 读取内存缓存, 热路径无 SQL.

- 启动: ensure_table -> initialize_if_empty (表空时: JSON 种子导入, 排除云冈类) -> reload_cache
- 运行时: CRUD 接口写 MySQL 后同步刷新缓存, 展示链路立即生效
- MySQL 未启用/不可用时缓存为空, 展示链路回落旧 JSON 文件 (兼容本地开发, 只读不写)
"""
import asyncio
import json
import re
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
    intersection_id INT UNSIGNED NULL COMMENT '所属路口(id, 关联 intersection_info.id)',
    show_on_map TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否在大屏显示该设备点位(0=不显示, 1=显示)',
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
            if not await _has_column(cur, "intersection_id"):
                await cur.execute(
                    f"ALTER TABLE {_TABLE} ADD COLUMN intersection_id INT UNSIGNED NULL "
                    "COMMENT '所属路口(id, 关联 intersection_info.id)' AFTER point_type"
                )
            # 旧表无「大屏显示」开关列: 补列 (默认 0 = 关闭; 存量设备一律保持关闭,
            # 由运维页面「大屏显示」开关逐台/批量开启)
            if not await _has_column(cur, "show_on_map"):
                await cur.execute(
                    f"ALTER TABLE {_TABLE} ADD COLUMN show_on_map TINYINT(1) NOT NULL DEFAULT 0 "
                    "COMMENT '是否在大屏显示该设备点位(0=不显示, 1=显示)' AFTER intersection_id"
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


# 门级关键字 (如「永泰东门」): 用于「在线设备短名 → 设备信息表全称」的景区归属兜底匹配
_GATE_RE = re.compile(r"[\u4e00-\u9fa5]{2}(?:东|西|南|北|正|大)?门")


def gate_key(name: str) -> str:
    """取名称中的门级关键字: 「永泰东门1口」→永泰东门; 「…和阳南门出口…」→和阳南门."""
    m = _GATE_RE.search(normalize(name))
    return m.group(0) if m else ""


def region_for(name: str) -> str:
    """按名称取所属景区 (region), 未登记返回 "".

    1) 先走 get() 精确/短名前缀匹配;
    2) 兜底: 大屏在线设备常用门/口简称 (如「永泰东门1口」), 而 device_info 存的是
       白名单全称 (如「GAJK-4926永泰东门入口处…」), 归一化后无法前缀命中 ——
       改用门级关键字匹配, 且仅当命中点位同属一个景区时才采用, 避免误划景区.
    """
    row = get(name)
    region = str((row or {}).get("region") or "").strip()
    if region:
        return region
    key = gate_key(name)
    if not key:
        return ""
    regions = {
        str(r.get("region") or "").strip()
        for n, r in _cache.items()
        if key in n and str(r.get("region") or "").strip()
    }
    return regions.pop() if len(regions) == 1 else ""


def intersection_id_for(name: str) -> Optional[int]:
    """按设备名称取所属路口 id (intersection_info.id); 未关联返回 None.

    1) 先走 get() 精确/短名前缀匹配 (依赖回填的 intersection_id 列);
    2) 兜底: 在线设备常用门/口简称, 而 device_info 存的是白名单全称, 归一化后
       无法前缀命中 —— 改用门级关键字匹配, 仅当命中点位同属一个路口时才采用.
    """
    row = get(name)
    iid = row.get("intersection_id") if row else None
    if iid:
        return int(iid)
    key = gate_key(name)
    if not key:
        return None
    ids = {
        int(r["intersection_id"])
        for n, r in _cache.items()
        if key in n and r.get("intersection_id")
    }
    return ids.pop() if len(ids) == 1 else None


def show_on_map_for(name: str) -> bool:
    """按设备名称取「是否大屏显示」开关 (热路径, 读内存缓存).

    仅对 device_info 已登记设备生效; 未登记设备 (如手动注册的在线通道) 不受该开关
    控制, 一律返回 True (照常上图), 避免"改不到就无法显示"。
    """
    row = get(name)
    if row is None:
        return True
    return bool(row.get("show_on_map"))


async def set_show_on_map_all(value: bool) -> int:
    """批量设置全部设备的「大屏显示」开关, 同步内存缓存. 返回影响条数."""
    pool = await get_pool()
    flag = 1 if value else 0
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(f"UPDATE {_TABLE} SET show_on_map = %s", (flag,))
            affected = cur.rowcount
    for r in _cache.values():
        r["show_on_map"] = bool(flag)
    logger.info(f"批量设置大屏显示开关: show_on_map={flag}, 影响 {affected} 条")
    return int(affected or 0)


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
                f"entrance_type, point_type, intersection_id, show_on_map FROM {_TABLE}"
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
            "intersection_id": int(r["intersection_id"]) if r["intersection_id"] is not None else None,
            # 大屏显示开关 (device_info.show_on_map): 0/None 均按关闭处理
            "show_on_map": bool(r.get("show_on_map")),
        }
    logger.info(f"设备信息表已加载 {len(_cache)} 条到内存缓存")


async def fetch(name: str) -> Optional[dict]:
    """从 MySQL 查单条 (运维接口用, 不依赖缓存). 返回 None 表示不存在."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                f"SELECT id, name, point_id, category, longitude, latitude, status, region, "
                f"entrance_type, point_type, intersection_id, show_on_map FROM {_TABLE} "
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
    intersection_id: Optional[int] = None,
    show_on_map: Optional[bool] = False,
) -> str:
    """新增/更新一条设备信息 (name 唯一, 冲突则覆盖), 并同步内存缓存. 返回归一化名称.

    出入口类型/点位类型未显式传入时按 category 自动派生; 显式传入则以此为准
    (运维页面手工改过的值不会被 category 覆盖).
    show_on_map: 是否在大屏显示该设备点位, 缺省 False (新建设备默认不上图).
    """
    norm = normalize(name)
    if not norm:
        raise ValueError("设备名称不能为空")
    region = region or _REGION_DEFAULT
    category = category or None
    entrance_type = entrance_type or derive_entrance_type(category)
    point_type = point_type or derive_point_type(category)
    show_on_map_val = 1 if show_on_map else 0
    pool = await get_pool()
    sql = f"""
    INSERT INTO {_TABLE} (name, point_id, category, longitude, latitude, status, region,
                          entrance_type, point_type, intersection_id, show_on_map)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON DUPLICATE KEY UPDATE
        point_id = VALUES(point_id),
        category = VALUES(category),
        longitude = VALUES(longitude),
        latitude = VALUES(latitude),
        status = VALUES(status),
        region = VALUES(region),
        entrance_type = VALUES(entrance_type),
        point_type = VALUES(point_type),
        intersection_id = VALUES(intersection_id),
        show_on_map = VALUES(show_on_map)
    """
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                sql,
                (norm, point_id or None, category, longitude, latitude, status or None, region,
                 entrance_type, point_type, intersection_id, show_on_map_val),
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
        "intersection_id": intersection_id,
        "show_on_map": bool(show_on_map_val),
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
                f"entrance_type, point_type, intersection_id, show_on_map, updated_at "
                f"FROM {_TABLE} ORDER BY id"
            )
            rows = await cur.fetchall()
    result = []
    for r in rows:
        row = dict(r)
        row["longitude"] = float(row["longitude"]) if row["longitude"] is not None else None
        row["latitude"] = float(row["latitude"]) if row["latitude"] is not None else None
        row["show_on_map"] = bool(row.get("show_on_map"))
        result.append(row)
    return result


async def search_options(q: Optional[str] = None, scenic: Optional[str] = None) -> list[dict]:
    """槽位绑定设备下拉选项: 来自 MySQL device_info 表, 支持名称模糊检索与景区过滤.

    仅返回绑定所需字段 (id/name/region/category/point_id), 不含经纬度等大字段.
    q 按 name LIKE %q% 模糊匹配 (走 MySQL 默认排序规则, 大小写不敏感);
    scenic 按 region 精确过滤 (与前端全局景区筛选对齐).
    """
    pool = await get_pool()
    sql = f"SELECT id, name, region, category, point_id FROM {_TABLE} WHERE 1=1"
    args: list = []
    if q and q.strip():
        sql += " AND name LIKE %s"
        args.append(f"%{q.strip()}%")
    if scenic and scenic.strip():
        sql += " AND region = %s"
        args.append(scenic.strip())
    sql += " ORDER BY id"
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(sql, args)
            rows = await cur.fetchall()
    result = []
    for r in rows:
        result.append({
            "id": r["id"],
            "name": r["name"],
            "region": r.get("region") or _REGION_DEFAULT,
            "category": r.get("category") or None,
            "point_id": r.get("point_id") or None,
        })
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
            "intersection_id": r["intersection_id"],
            "show_on_map": bool(r.get("show_on_map")),
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


async def backfill_intersection_ids() -> int:
    """按设备名最长匹配路口名回填 intersection_id (仅填 NULL 行, 幂等).

    匹配规则与大屏前端 gateKeyOfCfg 一致: 路口名完整出现在设备名中, 多个命中取最长者
    (如「永泰东门」优先于「永泰」)。只处理 intersection_id IS NULL 的设备, 不覆盖运维
    在设备页手工指定的关联; 回填后同步内存缓存, 展示链路立即生效。
    依赖 intersection_info 表已建好 + 种子已导入 (启动链路保证顺序)。
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            try:
                await cur.execute("SELECT id, name FROM intersection_info")
                crossroads = [
                    (int(r["id"]), str(r["name"]).strip())
                    for r in await cur.fetchall()
                    if r.get("name")
                ]
            except aiomysql.Error as e:  # noqa: BLE001
                logger.warning(f"回填设备所属路口: 读取 intersection_info 失败, 跳过: {e}")
                return 0
            if not crossroads:
                return 0
            crossroads.sort(key=lambda item: len(item[1]), reverse=True)

            await cur.execute(f"SELECT id, name FROM {_TABLE} WHERE intersection_id IS NULL")
            pending = await cur.fetchall()
            if not pending:
                return 0

            hits: list[tuple[int, int, str]] = []
            for d in pending:
                norm = normalize(d.get("name"))
                if not norm:
                    continue
                for iid, iname in crossroads:
                    if iname in norm:
                        hits.append((iid, int(d["id"]), norm))
                        break
            if not hits:
                return 0

            for iid, did, _ in hits:
                await cur.execute(
                    f"UPDATE {_TABLE} SET intersection_id = %s "
                    f"WHERE id = %s AND intersection_id IS NULL",
                    (iid, did),
                )
    for iid, _, norm in hits:
        if norm in _cache:
            _cache[norm]["intersection_id"] = iid
    logger.info(f"设备所属路口回填完成: {len(hits)} 条 (按名称最长匹配)")
    return len(hits)


# 表就绪标记: 建表+种子导入成功后置位, 之后各入口免重复 DDL/查询
_ready = False
_ready_lock = asyncio.Lock()


async def ensure_ready(force: bool = False) -> None:
    """确保 device_info 表可用 (建表 + 空表种子导入 + 缓存加载), 幂等单飞.

    后端可能早于 MySQL 就绪, 首次调用失败时不置 _ready, 下次调用自动重试,
    因此任意入口 (启动 lifespan / 运维 API) 先调本函数即可自愈 "表不存在 (1146)".
    """
    global _ready
    if _ready and not force:
        return
    async with _ready_lock:
        if _ready and not force:
            return
        await ensure_table()
        await initialize_if_empty()
        await reload_cache()
        _ready = True
