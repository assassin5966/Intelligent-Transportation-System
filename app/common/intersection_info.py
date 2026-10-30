"""路口信息 (路口数据卡配置) — MySQL 存储.

路口数据卡配置工具 (static/intersection-config.html) 用: 管理路口基础信息
与数据卡样式 (车道数量/车道类型/方向样式/旋转角度/车道顺序与方向/便道/计数线),
后续用于给设备信息做路口维度分类.

- 表: intersection_info (name 唯一), card_style 为 JSON 字符串
- 接口: static 页面经 /api/intersections CRUD (见 backend/api/intersection.py)
- MySQL 未启用时接口返回 503 (与 device_info 一致)
"""
import asyncio
import json
from pathlib import Path
from typing import Optional

import aiomysql

from .logger import logger
from .mysql_client import get_pool

_TABLE = "intersection_info"

# 种子数据 (9 条路口卡片配置, 含 card_style.slots 槽位绑定)
# 表为空时由 initialize_if_empty() 导入, 保证全新部署也有 8 张古城卡 + 云冈南门卡,
# 且槽位与设备按点位分类的绑定关系不丢失 (id 显式保留, 与 device_info.intersection_id 对应).
_SEED_FILE = Path(__file__).resolve().parents[2] / "data" / "intersection_info_seed.json"

_DDL = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id INT UNSIGNED NOT NULL AUTO_INCREMENT,
    name VARCHAR(255) NOT NULL COMMENT '路口名称(唯一)',
    lane_count TINYINT UNSIGNED NOT NULL DEFAULT 4 COMMENT '车道数量(1~10)',
    lane_width FLOAT NULL COMMENT '车道宽度(米), 示意图车道宽度联动',
    lane_type VARCHAR(32) NOT NULL DEFAULT 'dual' COMMENT '车道类型(single=单向, dual=双向)',
    direction VARCHAR(16) NULL COMMENT '行驶方向描述(如 向北)',
    badge VARCHAR(16) NULL COMMENT '出入口分类(入口/出口/出入口)',
    orientation VARCHAR(8) NOT NULL DEFAULT 'EW' COMMENT '方向样式(EW=东西方向/NS=南北方向)',
    rotation SMALLINT NOT NULL DEFAULT 0 COMMENT '数据卡旋转角度(0/90/180/270)',
    scenic VARCHAR(32) NULL COMMENT '所属景区(大同古城/云冈石窟/悬空寺)',
    longitude DOUBLE NULL COMMENT '经度',
    latitude DOUBLE NULL COMMENT '纬度',
    hidden TINYINT(1) NOT NULL DEFAULT 0 COMMENT '数据卡是否在大屏隐藏(0=显示/1=隐藏)',
    card_style JSON NULL COMMENT '数据卡样式(车道顺序/车道方向/便道/计数线/徽章等)',
    remark VARCHAR(255) NULL COMMENT '备注',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='路口信息(路口数据卡配置, 后续用于设备信息分类)'
"""

# 取值白名单 (与前端选项一致)
LANE_COUNT_ALLOWED = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)
LANE_TYPE_ALLOWED = ("single", "dual")
LANE_TYPE_TEXT = {
    "single": "单向",
    "dual": "双向",
}
ORIENTATION_ALLOWED = ("EW", "NS")
ROTATION_ALLOWED = (0, 90, 180, 270)
# 全局景区基线 (运维工具各页面「景区」下拉的固定项)。
# 实际选项 = 基线 ∪ device_info.region ∪ intersection_info.scenic (见 scenic_options())，
# 因此后端不再用固定白名单拒绝其它景区值，只做长度校验。
SCENIC_BASE = ("大同古城", "云冈石窟", "悬空寺")
SCENIC_ALLOWED = SCENIC_BASE        # 兼容旧引用
SCENIC_MAX_LEN = 32
BADGE_ALLOWED = ("入口", "出口", "出入口")

_COLS = ("id, name, lane_count, lane_width, lane_type, direction, badge, orientation, rotation, "
         "scenic, longitude, latitude, hidden, card_style, remark, created_at, updated_at")

# 旧表补列 (幂等: 已存在则忽略 1060)
_ALTERS = (
    f"ALTER TABLE {_TABLE} ADD COLUMN lane_width FLOAT NULL "
    f"COMMENT '车道宽度(米), 示意图车道宽度联动' AFTER lane_count",
    f"ALTER TABLE {_TABLE} ADD COLUMN scenic VARCHAR(32) NULL "
    f"COMMENT '所属景区(大同古城/云冈石窟/悬空寺)' AFTER rotation",
    f"ALTER TABLE {_TABLE} ADD COLUMN longitude DOUBLE NULL COMMENT '经度' AFTER scenic",
    f"ALTER TABLE {_TABLE} ADD COLUMN latitude DOUBLE NULL COMMENT '纬度' AFTER longitude",
    f"ALTER TABLE {_TABLE} ADD COLUMN badge VARCHAR(16) NULL "
    f"COMMENT '出入口分类(入口/出口/出入口)' AFTER direction",
    f"ALTER TABLE {_TABLE} ADD COLUMN hidden TINYINT(1) NOT NULL DEFAULT 0 "
    f"COMMENT '数据卡是否在大屏隐藏(0=显示/1=隐藏)' AFTER latitude",
)

# 车道类型收敛为 single/dual (与前端下拉一致): 仅修正默认值 + 归一化存量数据, 不删除列
_COLUMN_DEFAULTS = (
    f"ALTER TABLE {_TABLE} ALTER COLUMN lane_type SET DEFAULT 'dual'",
)
_DATA_FIXES = (
    f"UPDATE {_TABLE} SET lane_type='single' WHERE lane_type='oneway4_sidewalks'",
    f"UPDATE {_TABLE} SET lane_type='dual' WHERE lane_type IN ('dual4','dual6','dual8')",
    # 徽章升级为独立列: 从 card_style JSON 回填存量数据 (幂等)
    f"UPDATE {_TABLE} SET badge = JSON_UNQUOTE(JSON_EXTRACT(card_style, '$.badge')) "
    f"WHERE badge IS NULL AND card_style IS NOT NULL "
    f"AND JSON_UNQUOTE(JSON_EXTRACT(card_style, '$.badge')) IS NOT NULL "
    f"AND JSON_UNQUOTE(JSON_EXTRACT(card_style, '$.badge')) != ''",
)


def normalize(name: str) -> str:
    """名称归一化: 去首尾空白."""
    return (name or "").strip()


def validate(lane_count, lane_type, orientation, rotation,
             scenic=None, longitude=None, latitude=None, lane_width=None,
             badge=None) -> None:
    """字段取值校验 (非法抛 ValueError, 由 API 转 422)."""
    if int(lane_count) not in LANE_COUNT_ALLOWED:
        raise ValueError(f"车道数量仅支持 {list(LANE_COUNT_ALLOWED)}")
    if lane_width is not None:
        w = float(lane_width)
        if not (1 <= w <= 20):
            raise ValueError("车道宽度范围应为 1 ~ 20 米")
    if lane_type not in LANE_TYPE_ALLOWED:
        raise ValueError(f"车道类型仅支持 {list(LANE_TYPE_ALLOWED)}")
    if badge not in (None, "") and badge not in BADGE_ALLOWED:
        raise ValueError(f"出入口分类仅支持 {list(BADGE_ALLOWED)}")
    if orientation not in ORIENTATION_ALLOWED:
        raise ValueError(f"方向样式仅支持 {list(ORIENTATION_ALLOWED)}")
    if int(rotation) not in ROTATION_ALLOWED:
        raise ValueError(f"旋转角度仅支持 {list(ROTATION_ALLOWED)}")
    if scenic not in (None, ""):
        # 景区选项是动态的 (基线 + device_info.region + intersection_info.scenic),
        # 后端不再用固定白名单拒绝, 只限制长度并拒绝纯空白。
        s = str(scenic).strip()
        if not s:
            raise ValueError("景区名称不能为空白")
        if len(s) > SCENIC_MAX_LEN:
            raise ValueError(f"景区名称过长 (最多 {SCENIC_MAX_LEN} 字符)")
    if longitude is not None and not (-180 <= float(longitude) <= 180):
        raise ValueError("经度范围应为 -180 ~ 180")
    if latitude is not None and not (-90 <= float(latitude) <= 90):
        raise ValueError("纬度范围应为 -90 ~ 90")


def _dump_style(card_style: Optional[dict]) -> Optional[str]:
    if card_style is None:
        return None
    return json.dumps(card_style, ensure_ascii=False)


def _parse_style(raw) -> Optional[dict]:
    """card_style JSON -> dict (MySQL 8 JSON 列驱动可能直接返回 dict)."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


async def ensure_table() -> None:
    """建表 + 旧表补列 (幂等, 启动/首次请求时调用)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_DDL)
            for sql in _ALTERS:
                try:
                    await cur.execute(sql)
                except aiomysql.Error as e:
                    if e.args and e.args[0] == 1060:  # Duplicate column
                        continue
                    raise
            # 车道类型收敛: 修正默认值 (幂等) + 归一化存量旧值 (幂等)
            for sql in _COLUMN_DEFAULTS:
                try:
                    await cur.execute(sql)
                except aiomysql.Error as e:
                    if e.args and e.args[0] == 1060:
                        continue
                    raise
            for sql in _DATA_FIXES:
                await cur.execute(sql)


async def count() -> int:
    """路口总数 (查询失败按 0 处理, 由调用方决定是否继续)."""
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(f"SELECT COUNT(*) FROM {_TABLE}")
                row = await cur.fetchone()
        return int(row[0]) if row else 0
    except aiomysql.Error as e:
        logger.warning(f"统计路口数量失败 (按 0 处理): {e}")
        return 0


async def seed_from_json_if_empty() -> int:
    """表空时从 data/intersection_info_seed.json 导入初始路口配置 (幂等).

    种子保留显式 id (1/3/4/5/6/7/8/9/16), 与设备表 intersection_id 的既有对应关系一致;
    冲突时 ON DUPLICATE KEY UPDATE name = VALUES(name) 即原样跳过, 不覆盖运维改过的数据.
    数据源随 data 目录 bind mount, 更新种子只需改 JSON 后重启容器, 无需重建镜像.
    """
    if await count() > 0:
        return 0
    try:
        payload = json.loads(_SEED_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:  # noqa: BLE001
        logger.warning(f"路口种子导入: 读取 {_SEED_FILE.name} 失败: {e}")
        return 0
    rows = payload.get("intersections") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not rows:
        logger.warning(f"路口种子导入: {_SEED_FILE.name} 无有效数据, 跳过")
        return 0

    sql = (
        f"INSERT INTO {_TABLE} "
        f"(id, name, lane_count, lane_width, lane_type, direction, badge, orientation, rotation, "
        f"scenic, longitude, latitude, hidden, card_style, remark) "
        f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        f"ON DUPLICATE KEY UPDATE name = VALUES(name)"
    )
    imported = 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            for r in rows:
                if not isinstance(r, dict):
                    continue
                name = normalize(r.get("name"))
                if not name:
                    continue
                style = r.get("card_style")
                await cur.execute(
                    sql,
                    (
                        int(r["id"]) if r.get("id") else None,
                        name,
                        int(r.get("lane_count") or 4),
                        r.get("lane_width"),
                        r.get("lane_type") or "dual",
                        r.get("direction") or None,
                        r.get("badge") or None,
                        r.get("orientation") or "EW",
                        int(r.get("rotation") or 0),
                        r.get("scenic") or None,
                        r.get("longitude"),
                        r.get("latitude"),
                        1 if r.get("hidden") else 0,
                        _dump_style(style) if isinstance(style, dict) else (style or None),
                        r.get("remark") or None,
                    ),
                )
                imported += 1
    logger.info(f"路口种子导入完成: {imported} 条 (来自 {_SEED_FILE.name})")
    return imported


async def initialize_if_empty() -> int:
    """表为空时初始化 (幂等), 返回导入的路口条数.

    表已有数据时直接跳过, 运维在配置页产生的数据不受影响.
    """
    if await count() > 0:
        logger.info("路口信息表已有数据, 跳过初始化")
        return 0
    return await seed_from_json_if_empty()


# 表就绪标记: 建表 + 种子导入成功后置位, 之后各入口免重复 DDL
_ready = False
_ready_lock = asyncio.Lock()


async def ensure_ready(force: bool = False) -> None:
    """确保路口表可用 (建表 + 空表种子导入), 幂等单飞.

    后端可能早于 MySQL 就绪, 首次调用失败时不置 _ready, 下次调用自动重试,
    因此任意入口 (启动 lifespan / 配置页 API) 先调本函数即可自愈 "表不存在 (1146)".
    """
    global _ready
    if _ready and not force:
        return
    async with _ready_lock:
        if _ready and not force:
            return
        await ensure_table()
        await initialize_if_empty()
        _ready = True


async def list_all() -> list[dict]:
    """路口列表 (配置页表格用)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(f"SELECT {_COLS} FROM {_TABLE} ORDER BY id")
            rows = await cur.fetchall()
    for r in rows:
        r["card_style"] = _parse_style(r.get("card_style"))
    return rows


async def fetch(iid: int) -> Optional[dict]:
    """按 id 查单条 (不存在返回 None)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(f"SELECT {_COLS} FROM {_TABLE} WHERE id = %s", (iid,))
            row = await cur.fetchone()
    if row:
        row["card_style"] = _parse_style(row.get("card_style"))
    return row


async def exists_name(name: str, exclude_id: Optional[int] = None) -> bool:
    """重名校查 (改名时排除自身)."""
    norm = normalize(name)
    pool = await get_pool()
    sql = f"SELECT id FROM {_TABLE} WHERE name = %s"
    args: tuple = (norm,)
    if exclude_id is not None:
        sql += " AND id != %s"
        args = (norm, exclude_id)
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql + " LIMIT 1", args)
            return await cur.fetchone() is not None


async def create(name: str, lane_count: int = 4, lane_width: Optional[float] = None,
                 lane_type: str = "dual",
                 direction: Optional[str] = None, badge: Optional[str] = None,
                 orientation: str = "EW",
                 rotation: int = 0, scenic: Optional[str] = None,
                 longitude: Optional[float] = None, latitude: Optional[float] = None,
                 card_style: Optional[dict] = None,
                 remark: Optional[str] = None,
                 hidden: bool = False) -> int:
    """新增路口 (name 唯一, 重名抛 ValueError). 返回新 id."""
    norm = normalize(name)
    if not norm:
        raise ValueError("路口名称不能为空")
    validate(lane_count, lane_type, orientation, rotation,
             scenic=scenic, longitude=longitude, latitude=latitude,
             lane_width=lane_width, badge=badge)
    if await exists_name(norm):
        raise ValueError(f"路口名称已存在: {norm}")
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"INSERT INTO {_TABLE} "
                f"(name, lane_count, lane_width, lane_type, direction, badge, orientation, rotation, "
                f"scenic, longitude, latitude, hidden, card_style, remark) "
                f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (norm, int(lane_count), lane_width, lane_type, direction or None,
                 badge or None, orientation,
                 int(rotation), scenic or None, longitude, latitude,
                 1 if hidden else 0,
                 _dump_style(card_style), remark or None),
            )
            iid = cur.lastrowid
    logger.info(f"路口已创建: id={iid} name={norm}")
    return iid


async def update(iid: int, fields: dict) -> bool:
    """按 id 更新 (fields 仅含要改的列; 取值校验同新增). 返回是否存在."""
    existing = await fetch(iid)
    if not existing:
        return False
    allowed = {"name", "lane_count", "lane_width", "lane_type", "direction", "badge", "orientation",
               "rotation", "scenic", "longitude", "latitude", "hidden", "card_style", "remark"}
    sets: dict = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return True
    if "hidden" in sets:
        sets["hidden"] = 1 if sets["hidden"] else 0
    merged = {**existing, **sets}
    validate(merged["lane_count"], merged["lane_type"],
             merged["orientation"], merged["rotation"],
             scenic=merged.get("scenic"),
             longitude=merged.get("longitude"),
             latitude=merged.get("latitude"),
             lane_width=merged.get("lane_width"),
             badge=merged.get("badge"))
    new_name = normalize(merged["name"])
    if not new_name:
        raise ValueError("路口名称不能为空")
    if await exists_name(new_name, exclude_id=iid):
        raise ValueError(f"路口名称已存在: {new_name}")
    sets["name"] = new_name
    if "card_style" in sets:
        sets["card_style"] = _dump_style(sets["card_style"])
    col_sql = ", ".join(f"{k} = %s" for k in sets)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE {_TABLE} SET {col_sql} WHERE id = %s",
                (*sets.values(), iid),
            )
    logger.info(f"路口已更新: id={iid} fields={list(sets)}")
    return True


async def delete(iid: int) -> bool:
    """删除路口. 返回是否实际删除."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(f"DELETE FROM {_TABLE} WHERE id = %s", (iid,))
            affected = cur.rowcount
    if affected:
        logger.info(f"路口已删除: id={iid}")
    return bool(affected)


async def scenic_options() -> dict:
    """全局景区选项: 基线 ∪ device_info.region ∪ intersection_info.scenic.

    设备信息表的「区域」与路口表的「景区」在库里各自独立保存 (region / scenic)，
    前端统一展示为「景区」下拉。选项按基线顺序在前、其余值按名称排序在后，
    保证运维新录入的景区自动出现在下拉里 (无需改代码)。
    device_info 表缺失/查询失败时不影响路口侧选项 (容错)。
    """
    pool = await get_pool()
    regions: list[str] = []
    scenics: list[str] = []
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute(
                    "SELECT DISTINCT region FROM device_info "
                    "WHERE region IS NOT NULL AND TRIM(region) <> ''"
                )
                regions = [str(r[0]).strip() for r in await cur.fetchall() if r[0]]
            except aiomysql.Error as e:
                logger.warning(f"读取 device_info.region 失败 (忽略, 仅用路口侧选项): {e}")
            await cur.execute(
                f"SELECT DISTINCT scenic FROM {_TABLE} "
                "WHERE scenic IS NOT NULL AND TRIM(scenic) <> ''"
            )
            scenics = [str(r[0]).strip() for r in await cur.fetchall() if r[0]]

    extra = sorted((set(regions) | set(scenics)) - set(SCENIC_BASE))
    return {
        "options": list(SCENIC_BASE) + extra,
        "base": list(SCENIC_BASE),
        "from_device_region": sorted(set(regions)),
        "from_intersection": sorted(set(scenics)),
    }
