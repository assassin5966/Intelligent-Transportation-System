# 离线瓦片目录

本目录用于存放高德地图瓦片，路径为 `public/tiles/{z}/{x}/{y}.png`。

**不要把已下载的瓦片提交到 git**（.gitignore 已忽略 `public/tiles*`）。

## 下载

执行 `node scripts/download-amap-tiles.mjs` 从高德瓦片服务器下载指定区域/缩放区间的瓦片。

常用参数：

```bash
# 下载大同古城 z12-z18 路网矢量中文（默认）
node scripts/download-amap-tiles.mjs --cities=datong --min-zoom=12 --max-zoom=18

# 三个城市全量
node scripts/download-amap-tiles.mjs --cities=datong,yungangshiku,xian --min-zoom=12 --max-zoom=18

# 卫星图（style=6，输出 .jpg）
node scripts/download-amap-tiles.mjs --cities=datong --min-zoom=12 --max-zoom=18 --style=6 --out=public/tiles-sat

# 先看下载计划不执行
node scripts/download-amap-tiles.mjs --dry-run --cities=datong --min-zoom=12 --max-zoom=18
```

## 验证

启动 Vite dev server（`npm run dev`），地图会自动通过 `/tiles/{z}/{x}/{y}.png` 加载本目录的瓦片。

如需在生产环境使用，构建时 `public/tiles/` 会自动拷贝到 `dist/tiles/`，由 nginx 静态服务即可。
