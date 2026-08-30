#!/usr/bin/env python3
"""
内网离线瓦片下载工具
----------------------
从公网 OSM 瓦片源下载指定区域的瓦片，保存为 {z}/{x}/{y}.png 结构，
供内网 Nginx 或 TileServer 静态托管。

用法:
  # 下载所有区域（大同/云冈/西安），zoom 10~18
  python download_tiles.py --output ./tiles

  # 仅下载大同区域，zoom 12~16
  python download_tiles.py --output ./tiles --area datong --min-zoom 12 --max-zoom 16

  # 使用天地图瓦片源（需替换 YOUR_KEY）
  python download_tiles.py --url "https://t0.tianditu.gov.cn/vec_c/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER=vec&STYLE=default&TILEMATRIXSET=c&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}&FORMAT=tiles&tk=YOUR_KEY"

  # 下载深色主题瓦片（CartoDB dark）
  python download_tiles.py --output ./tiles/dark --url "https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png"
"""

import os
import sys
import math
import time
import argparse
import urllib.request
import urllib.error
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── 区域定义（从 MapPanel.vue 的 CITY 配置提取） ──
AREAS = {
    "datong": {
        "name": "大同 · 古城",
        "bounds": [113.289814, 40.083292, 113.315134, 40.103218],  # min_lng, min_lat, max_lng, max_lat
    },
    "yungang": {
        "name": "大同 · 云冈石窟",
        "bounds": [113.12589, 40.101345, 113.14589, 40.121345],
    },
    "xian": {
        "name": "西安 · 雁塔",
        "bounds": [108.9440, 34.2550, 108.9640, 34.2750],
    },
}

# 默认瓦片源（OSM 标准样式，WGS-84 坐标系，与项目坐标一致）
DEFAULT_URL = "https://a.tile.openstreetmap.org/{z}/{x}/{y}.png"

# 请求头（模拟浏览器，避免被部分瓦片源拦截）
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; TileDownloader/1.0; +internal)",
    "Accept": "image/png,image/*;q=0.8,*/*;q=0.5",
}


def lat_lng_to_tile(lat, lng, zoom):
    """经纬度 → 瓦片坐标 (x, y)"""
    n = 2.0 ** zoom
    x = int((lng + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def tile_bounds_to_range(min_lng, min_lat, max_lng, max_lat, zoom):
    """区域经纬度边界 → 瓦片 x/y 范围"""
    x_min, y_max = lat_lng_to_tile(min_lat, min_lng, zoom)   # 左上
    x_max, y_min = lat_lng_to_tile(max_lat, max_lng, zoom)   # 右下
    return x_min, x_max, y_min, y_max


def download_tile(url_template, z, x, y, output_dir, retries=3):
    """下载单张瓦片，保存到 output_dir/{z}/{x}/{y}.png"""
    url = url_template.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))
    save_path = output_dir / str(z) / str(x) / f"{y}.png"

    if save_path.exists():
        return True  # 已存在，跳过

    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
            if len(data) < 100:
                # 太小的文件可能是占位图，跳过
                return False
            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.write_bytes(data)
            return True
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
            if attempt < retries - 1:
                time.sleep(1)
            else:
                return False


def download_area(area_key, area_info, url_template, output_dir,
                  min_zoom, max_zoom, workers, progress_callback=None):
    """下载指定区域所有 zoom 级别的瓦片"""
    b = area_info["bounds"]  # min_lng, min_lat, max_lng, max_lat
    total = 0
    success = 0

    for z in range(min_zoom, max_zoom + 1):
        x_min, x_max, y_min, y_max = tile_bounds_to_range(*b, z)
        tiles_at_zoom = (x_max - x_min + 1) * (y_max - y_min + 1)
        total += tiles_at_zoom

        # 收集该 zoom 的所有瓦片坐标
        tasks = []
        for x in range(x_min, x_max + 1):
            for y in range(y_min, y_max + 1):
                tasks.append((z, x, y))

        # 并发下载
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {}
            for z_, x_, y_ in tasks:
                fut = pool.submit(download_tile, url_template, z_, x_, y_, output_dir)
                futures[fut] = (z_, x_, y_)

            for fut in as_completed(futures):
                ok = fut.result()
                if ok:
                    success += 1
                if progress_callback:
                    progress_callback(1)

    return total, success


def main():
    parser = argparse.ArgumentParser(description="内网离线瓦片下载工具")
    parser.add_argument("--output", "-o", default="./tiles",
                        help="瓦片输出目录 (默认: ./tiles)")
    parser.add_argument("--url", "-u", default=DEFAULT_URL,
                        help="瓦片源 URL，支持 {z}/{x}/{y} 占位符 (默认: OSM)")
    parser.add_argument("--area", "-a", default=None,
                        help="区域: datong / yungang / xian / all (默认: all)")
    parser.add_argument("--min-zoom", type=int, default=10,
                        help="最小 zoom 级别 (默认: 10)")
    parser.add_argument("--max-zoom", type=int, default=18,
                        help="最大 zoom 级别 (默认: 18)")
    parser.add_argument("--workers", "-w", type=int, default=8,
                        help="并发下载线程数 (默认: 8)")
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 确定要下载的区域
    if args.area and args.area in AREAS:
        areas = {args.area: AREAS[args.area]}
    else:
        areas = AREAS

    print(f"瓦片源: {args.url}")
    print(f"输出目录: {output_dir.resolve()}")
    print(f"Zoom 范围: {args.min_zoom} ~ {args.max_zoom}")
    print(f"区域: {', '.join(areas.keys())}")
    print(f"并发线程: {args.workers}")
    print()

    grand_total = 0
    grand_success = 0

    for area_key, area_info in areas.items():
        print(f"── [{area_info['name']}] 开始下载 ──")

        # 预估总数
        est = 0
        for z in range(args.min_zoom, args.max_zoom + 1):
            x_min, x_max, y_min, y_max = tile_bounds_to_range(*area_info["bounds"], z)
            est += (x_max - x_min + 1) * (y_max - y_min + 1)
        print(f"  预估瓦片数: {est}")

        total, success = download_area(
            area_key, area_info, args.url, output_dir,
            args.min_zoom, args.max_zoom, args.workers,
        )
        grand_total += total
        grand_success += success
        print(f"  完成: {success}/{total}  ({success/total*100:.1f}% 成功率)" if total > 0 else "  无瓦片需下载")
        print()

    print(f"══ 全部完成 ══")
    print(f"  总计: {grand_success}/{grand_total}")
    print(f"  输出目录: {output_dir.resolve()}")
    print(f"  磁盘占用: ", end="")

    # 统计磁盘占用
    total_size = sum(f.stat().st_size for f in output_dir.rglob("*.png"))
    if total_size > 1024**3:
        print(f"{total_size / 1024**3:.2f} GB")
    elif total_size > 1024**2:
        print(f"{total_size / 1024**2:.2f} MB")
    else:
        print(f"{total_size / 1024:.1f} KB")

    print()
    print("部署指南:")
    print(f"  1. 将 {output_dir.resolve()} 目录复制到内网服务器")
    print(f"  2. 用 Nginx 托管: 见 nginx-tile.conf")
    print(f"  3. 前端配置: VITE_TILE_URL=http://<内网IP>/tiles/{{z}}/{{x}}/{{y}}.png")


if __name__ == "__main__":
    main()