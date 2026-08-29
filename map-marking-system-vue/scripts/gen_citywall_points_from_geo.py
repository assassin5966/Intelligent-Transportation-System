# -*- coding: utf-8 -*-
"""按 data/device_geo.json（后端地理库，源自 设备信息汇总表.xlsx）重建 cityWallPoints.js。

与 gen_citywall_points_from_csv.py 的区别：
  - 数据源从本机 CSV（Windows 路径）改为仓库内 data/device_geo.json，可在任意环境复现；
  - 点位字段（id/经纬度/category/camera_type）与后端地理库保持一致，作为无后端时的离线兜底；
  - 门楼/方向由设备名派生，实时计数等展示字段由前端 WS 真实数据覆盖。

用法: python3 scripts/gen_citywall_points_from_geo.py
"""
import json, re, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根 (map-marking-system-vue)
GEO_PATH = os.path.join(ROOT, "..", "data", "device_geo.json")      # 后端地理库
OUT_PATH = os.path.join(ROOT, "src", "data", "cityWallPoints.js")

with open(GEO_PATH, "r", encoding="utf-8") as f:
    geo = json.load(f)

ID_RE = re.compile(r"(GAJ[KQ]-\d+|GAKK-\d+|JTKK-\d+)")


def parse_gate(name):
    for g, kw in (("和阳门", "和阳"), ("永泰门", "永泰"), ("清远门", "清远"), ("武定门", "武定")):
        if kw in name:
            return g
    return "大同古城"


def parse_dir(name):
    if "入口" in name:
        return "入口"
    if "出口" in name:
        return "出口"
    return "其他"


points = []
for dev_name, info in geo.get("devices", {}).items():
    lng = info.get("longitude")
    lat = info.get("latitude")
    if lng is None or lat is None:
        continue
    m = ID_RE.search(dev_name)
    pid = m.group(1) if m else dev_name
    category = info.get("category") or ""
    is_kakou = "卡口" in category or pid.startswith("GAKK")
    points.append({
        "id": pid,
        "desc": dev_name,
        "type": "卡口" if is_kakou else "便道",
        "gate": parse_gate(dev_name),
        "direction": parse_dir(dev_name),
        "coord": [float(lng), float(lat)],
        "lastActive": "2026-08-18",
        "status": "syncing",
        "typeLabel": "车行" if is_kakou else "人行",
        "count": 0,
        "category": category,
        "camera_type": "vehicle" if is_kakou else "person",
    })

points.sort(key=lambda p: (p["id"]))

lines = []
lines.append("/**")
lines.append(" * 城墙（大同古城墙）出入口监控 / 卡口点位数据（离线兜底）")
lines.append(" * 数据源自 data/device_geo.json（后端地理库，设备名称 -> 经纬度/分类映射）。")
lines.append(" * 字段说明：")
lines.append(" *   id         设备编号（GAJK-xxxx 便道监控 / GAKK-xxxx 车辆卡口）")
lines.append(" *   desc       设备名称（与后端 /api/devices 的 name 一致）")
lines.append(" *   type       类型：便道（人行道监控）| 卡口（车辆卡口）")
lines.append(" *   gate       所属城门：和阳门 / 永泰门 / 清远门 / 武定门")
lines.append(" *   direction  出入口方向：入口 / 出口 / 其他")
lines.append(" *   coord      经纬度（[经度, 纬度]，与后端 longitude/latitude 一致）")
lines.append(" *   lastActive 最近活跃日期（合成，用于日期范围筛选演示）")
lines.append(" *   status     运行状态（兜底为 syncing，实际以后端 status 为准）")
lines.append(" *   typeLabel  计数类型：人行 / 车行")
lines.append(" *   count      实时计数（占位 0，实际以 WS stats 为准）")
lines.append(" *   category   点位分类（与后端 category 一致）")
lines.append(" *   camera_type 摄像头类型：vehicle / person（与后端 camera_type 一致）")
lines.append(" *")
lines.append(" * 注意：地图点位实时数据（名称/经纬度/分类/计数/拥挤度）以后端 /api/devices + WS stats.devices 为准；")
lines.append(" *       本文件仅在后端不可用时作离线兜底展示，不覆盖后端真实数据。")
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
lines.append("/** 按展示优先级排序：城门顺序 → 类型（卡口>便道）→ 方向（入口>出口>其他） */")
lines.append("const _GATE_ORDER = ['和阳门', '永泰门', '清远门', '武定门']")
lines.append("const _DIR_ORDER = { '入口': 0, '出口': 1, '其他': 2 }")
lines.append("export function sortPointsByPriority(points) {")
lines.append("  return (points || []).slice().sort((a, b) => {")
lines.append("    const ga = _GATE_ORDER.indexOf(a.gate)")
lines.append("    const gb = _GATE_ORDER.indexOf(b.gate)")
lines.append("    if (ga !== gb) return (ga === -1 ? 99 : ga) - (gb === -1 ? 99 : gb)")
lines.append("    const ta = a.type === '卡口' ? 0 : 1")
lines.append("    const tb = b.type === '卡口' ? 0 : 1")
lines.append("    if (ta !== tb) return ta - tb")
lines.append("    return (_DIR_ORDER[a.direction] ?? 2) - (_DIR_ORDER[b.direction] ?? 2)")
lines.append("  })")
lines.append("}")
lines.append("")

os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
with open(OUT_PATH, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))

print("written:", OUT_PATH, "points:", len(points))
lons = [p["coord"][0] for p in points]
lats = [p["coord"][1] for p in points]
print("lon range:", min(lons), "->", max(lons))
print("lat range:", min(lats), "->", max(lats))
