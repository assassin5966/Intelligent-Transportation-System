# -*- coding: utf-8 -*-
"""用 data/设备信息汇总表.xlsx (最新更新) 重建设备白名单种子数据.

生成:
  data/device_geo.json       设备名(设备名称-新) -> {point_id, category, longitude, latitude, status}
  data/device_category.json  设备名 -> {category, point_id}

表结构 (Sheet1):
  E列 点位编号 (GAJK-xxxx / GAKK-xxxx / JTKK-xxxx)
  F列 点位分类 (城墙出入口便道监控点位 / 城墙入口车辆卡口点位 / 城墙出口车辆卡口点位)
  G列 设备名称 (旧名)   H列 设备名称-新 (含 (球)042031 等通道后缀, 白名单采用此名)
  J列 经度  K列 纬度  L列 状态 (已验证/待验证)
  卡口每点位多通道: 序号仅在首通道行, 状态为空的行沿用同点位首行状态.

云冈类 (JTKK, 无经纬度/无新名) 不入库, 与 app/common/device_info.py 的 is_yungang 规则一致.
MySQL 表替换脚本见同目录 replace_device_info.py.
"""
import json
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
XLSX = ROOT / "data" / "设备信息汇总表.xlsx"
GEO_OUT = ROOT / "data" / "device_geo.json"
CAT_OUT = ROOT / "data" / "device_category.json"


def is_yungang(point_id, name) -> bool:
    """云冈类 (JTKK 卡口, 无经纬度, 暂不入库)."""
    if point_id and str(point_id).upper().startswith("JTKK"):
        return True
    return (name or "").upper().startswith("JTKK") or "云冈" in (name or "")


def main() -> None:
    wb = openpyxl.load_workbook(XLSX, data_only=True, read_only=True)
    ws = wb["Sheet1"]

    devices: dict[str, dict] = {}
    # 卡口同点位多通道行状态为空, 沿用同点位首行状态
    last_status = "待验证"
    for row in ws.iter_rows(values_only=True):
        point_id = row[4]  # E列 点位编号
        if point_id is None:
            continue
        point_id = str(point_id).strip()
        if not point_id or point_id == "点位编号":  # 跳过表头行
            continue
        category = str(row[5]).strip() if row[5] is not None else None
        name_new = str(row[7]).strip() if row[7] is not None else None
        name = name_new or (str(row[6]).strip() if row[6] is not None else None)
        if not name or is_yungang(point_id, name):
            continue  # 云冈类不入库
        status = str(row[11]).strip() if row[11] is not None else None
        if status:
            last_status = status
        devices[name] = {
            "point_id": point_id,
            "category": category,
            "longitude": float(row[9]) if row[9] is not None else None,
            "latitude": float(row[10]) if row[10] is not None else None,
            "status": last_status,
        }

    # --- device_geo.json ---
    with_geo = sum(1 for d in devices.values() if d["longitude"] is not None)
    geo_doc = {
        "_meta": {
            "source": "data/设备信息汇总表.xlsx",
            "generated_at": "2026-09-15",
            "total": len(devices),
            "total_with_geo": with_geo,
            "total_without_geo": len(devices) - with_geo,
            "note": "设备名称(设备名称-新) -> 经纬度/分类/状态 映射 (直接取自汇总表, 排除云冈类)",
        },
        "devices": devices,
    }
    GEO_OUT.write_text(json.dumps(geo_doc, ensure_ascii=False, indent=2), encoding="utf-8")

    # --- device_category.json (新版对象值, 兼容旧版字符串读取) ---
    cat_doc = {
        name: {"category": d["category"], "point_id": d["point_id"]}
        for name, d in devices.items()
    }
    CAT_OUT.write_text(json.dumps(cat_doc, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"汇总表设备总数: {len(devices)} (便道 {sum(1 for d in devices.values() if '便道' in d['category'])}, 卡口 {sum(1 for d in devices.values() if '卡口' in d['category'])})")
    print(f"有经纬度: {with_geo}, 无经纬度: {len(devices) - with_geo} (云冈类已排除)")
    print("written:", GEO_OUT)
    print("written:", CAT_OUT)
    for name, d in devices.items():
        geo = "有" if d["longitude"] is not None else "无"
        print(f"  [{geo}] {name}  <- {d['point_id']} / {d['category']} / {d['status']}")


if __name__ == "__main__":
    main()
