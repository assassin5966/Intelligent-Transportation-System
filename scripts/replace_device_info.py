# -*- coding: utf-8 -*-
"""用 data/device_geo.json + device_category.json 全量替换 MySQL device_info 表.

docker compose 环境: 宿主机 3307 -> 容器 mysql 3306 (root/root123456, 库 dt_stats).
替换 = TRUNCATE 后按 device_geo.json devices 全量 upsert (排除云冈类).
用法: python3 scripts/replace_device_info.py
"""
import json
from pathlib import Path

import pymysql

ROOT = Path(__file__).resolve().parents[1]
GEO_FILE = ROOT / "data" / "device_geo.json"
CAT_FILE = ROOT / "data" / "device_category.json"
REGION_DEFAULT = "大同古城"


def normalize(name: str) -> str:
    """名称归一化: 去空格 (与 device_info 匹配逻辑一致)."""
    return (name or "").replace(" ", "")


def is_yungang(name: str, point_id=None) -> bool:
    """是否"云冈类"设备 (JTKK 卡口, 无经纬度, 暂不入库)."""
    norm = normalize(name)
    if point_id and str(point_id).upper().startswith("JTKK"):
        return True
    return norm.upper().startswith("JTKK") or "云冈" in norm


def derive_entrance_type(category) -> str | None:
    """出入口类型: 含"出入口"→出入口, 含"入口"→入口, 含"出口"→出口 (规则同 app/common/device_info.py)."""
    cat = category or ""
    if "出入口" in cat:
        return "出入口"
    if "入口" in cat:
        return "入口"
    if "出口" in cat:
        return "出口"
    return None


def derive_point_type(category) -> str | None:
    """点位类型: 含"便道"→便道, 含"卡口"→车辆卡口."""
    cat = category or ""
    if "便道" in cat:
        return "便道"
    if "卡口" in cat:
        return "车辆卡口"
    return None


_DDL = """
CREATE TABLE IF NOT EXISTS device_info (
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


def _ensure_columns(cur) -> None:
    """旧表补列 (幂等): region / entrance_type / point_type. 随后本脚本 TRUNCATE 全量重写."""
    cur.execute(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'device_info'"
    )
    existing = {r[0] for r in cur.fetchall()}
    for col, ddl in (
        ("region", "ADD COLUMN region VARCHAR(64) NULL COMMENT '区域(如 大同古城)'"),
        ("entrance_type",
         "ADD COLUMN entrance_type VARCHAR(16) NULL COMMENT '出入口类型(入口/出口/出入口, 由 category 派生)'"),
        ("point_type",
         "ADD COLUMN point_type VARCHAR(16) NULL COMMENT '点位类型(便道/车辆卡口, 由 category 派生)'"),
    ):
        if col not in existing:
            cur.execute(f"ALTER TABLE device_info {ddl}")


def main() -> None:
    geo_raw = json.loads(GEO_FILE.read_text(encoding="utf-8")).get("devices", {})
    try:
        cat_map = json.loads(CAT_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        cat_map = {}

    rows = []
    for name, d in geo_raw.items():
        norm = normalize(name)
        if not norm or is_yungang(norm, d.get("point_id")):
            continue
        cat_entry = cat_map.get(norm)
        category = cat_entry.get("category") if isinstance(cat_entry, dict) else cat_entry
        point_id = cat_entry.get("point_id") if isinstance(cat_entry, dict) else None
        if not isinstance(cat_entry, dict):
            cat_entry = {}
        # JSON 未带派生字段时按点位分类兜底派生 (规则同 app/common/device_info.py)
        entrance_type = cat_entry.get("entrance_type") or d.get("entrance_type") \
            or derive_entrance_type(category)
        point_type = cat_entry.get("point_type") or d.get("point_type") \
            or derive_point_type(category)
        rows.append((
            norm,
            point_id or d.get("point_id") or None,
            category or None,
            d.get("longitude"),
            d.get("latitude"),
            d.get("status") or None,
            REGION_DEFAULT,
            entrance_type,
            point_type,
        ))

    conn = pymysql.connect(host="127.0.0.1", port=3307, user="root",
                           password="root123456", database="dt_stats", charset="utf8mb4")
    try:
        with conn.cursor() as cur:
            cur.execute(_DDL)
            _ensure_columns(cur)
            cur.execute("TRUNCATE TABLE device_info")
            cur.executemany(
                "INSERT INTO device_info (name, point_id, category, longitude, latitude, status, region, "
                "entrance_type, point_type) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                rows,
            )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM device_info")
            total = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM device_info WHERE longitude IS NOT NULL AND latitude IS NOT NULL"
            )
            with_geo = cur.fetchone()[0]
            cur.execute("SELECT entrance_type, COUNT(*) FROM device_info GROUP BY entrance_type")
            by_entrance = ", ".join(f"{k or '未知'} {v}" for k, v in cur.fetchall())
            cur.execute("SELECT point_type, COUNT(*) FROM device_info GROUP BY point_type")
            by_point = ", ".join(f"{k or '未知'} {v}" for k, v in cur.fetchall())
        print(f"device_info 表已替换: {total} 条 (有经纬度 {with_geo}, 云冈类已排除)")
        print(f"出入口类型: {by_entrance}")
        print(f"点位类型: {by_point}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
