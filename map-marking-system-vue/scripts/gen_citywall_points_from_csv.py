# -*- coding: utf-8 -*-
"""按「大同古城墙点位坐标_验证版.csv」真实经纬度重建 cityWallPoints.js。
保留原数据的 count / lastActive（按 id 从旧文件映射），其余字段由 CSV 解析。"""
import csv, io, re, json

CSV_PATH = r"D:\软件安装\xwechat_files\wxid_4lbv9zdza6hi41_fb66\msg\file\2026-08\大同古城墙点位坐标_验证版.csv"
OLD_PATH = r"D:/order/test1/map-marking-system-vue/src/data/cityWallPoints.js"
OUT_PATH = r"D:/order/test1/map-marking-system-vue/src/data/cityWallPoints.js"

# 1) 读取旧文件，提取 id -> {count, lastActive}
with open(OLD_PATH, "r", encoding="utf-8") as f:
    old_text = f.read()
old_map = {}
for m in re.finditer(r'\{"id": "([^"]+)", "desc": "[^"]+", "type": "[^"]+", "gate": "[^"]+", "direction": "[^"]+", "coord": \[[^\]+]+\], "lastActive": "([^"]+)", "status": "[^"]+", "typeLabel": "[^"]+", "count": (\d+)\}', old_text):
    old_map[m.group(1)] = {"lastActive": m.group(2), "count": int(m.group(3))}
print("old ids:", len(old_map))

# 2) 读取 CSV
with open(CSV_PATH, "r", encoding="utf-8-sig") as f:
    raw = f.read()
rows = list(csv.DictReader(io.StringIO(raw)))
print("csv rows:", len(rows))

def parse_gate(addr):
    for g, kw in (("和阳门", "和阳"), ("永泰门", "永泰"), ("清远门", "清远"), ("武定门", "武定")):
        if kw in addr:
            return g
    return "大同古城"

def parse_dir(typ, addr):
    if "入口" in typ:
        return "入口"
    if "出口" in typ:
        return "出口"
    if "入口" in addr:
        return "入口"
    if "出口" in addr:
        return "出口"
    return "其他"

points = []
for r in rows:
    pid = r["点位ID"].strip()
    typ = r["类型"].strip()
    addr = r["地址"].strip()
    is_kakou = "卡口" in typ
    ptype = "卡口" if is_kakou else "便道"
    old = old_map.get(pid, {"lastActive": "2026-08-18", "count": 66})
    points.append({
        "id": pid,
        "desc": addr,
        "type": ptype,
        "gate": parse_gate(addr),
        "direction": parse_dir(typ, addr),
        "coord": [float(r["经度"].strip()), float(r["纬度"].strip())],
        "lastActive": old["lastActive"],
        "status": "syncing",  # CSV 状态均为「待验证」
        "typeLabel": "车行" if is_kakou else "人行",
        "count": old["count"],
    })

# 3) 写出新 js
lines = []
lines.append("/**")
lines.append(" * 城墙（大同古城墙）出入口监控 / 卡口点位数据")
lines.append(" * 数据源自「大同古城墙点位坐标_验证版.csv」（25 个点位，真实经纬度）。")
lines.append(" * 字段说明：")
lines.append(" *   id         设备编号（GAJK-xxxx 便道监控 / GAKK-xxxx 车辆卡口）")
lines.append(" *   desc       点位地址描述（CSV 原文）")
lines.append(" *   type       类型：便道（人行道监控）| 卡口（车辆卡口）")
lines.append(" *   gate       所属城门：和阳门 / 永泰门 / 清远门 / 武定门")
lines.append(" *   direction  出入口方向：入口 / 出口 / 其他")
lines.append(" *   coord      经纬度（CSV 验证版真实坐标）")
lines.append(" *   lastActive 最近活跃日期（合成，用于日期范围筛选演示）")
lines.append(" *   status     运行状态（CSV 均为待验证，映射为 syncing）")
lines.append(" *   typeLabel  计数类型：人行 / 车行")
lines.append(" *   count      实时计数（合成）")
lines.append(" */")
lines.append("export const CITY_WALL_POINTS = [")
for p in points:
    lines.append("  " + json.dumps(p, ensure_ascii=False, separators=(", ", ": ")) + ",")
lines.append("]")
lines.append("")
lines.append("export const CITY_WALL_GATES = ['和阳门', '永泰门', '清远门', '武定门']")
lines.append("export const CITY_WALL_TYPES = ['便道', '卡口']")
lines.append("export const CITY_WALL_DIRS = ['入口', '出口', '其他']")
lines.append("")

with open(OUT_PATH, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print("written:", OUT_PATH, "points:", len(points))

# 校验坐标范围
lons = [p["coord"][0] for p in points]
lats = [p["coord"][1] for p in points]
print("lon range:", min(lons), "->", max(lons))
print("lat range:", min(lats), "->", max(lats))
