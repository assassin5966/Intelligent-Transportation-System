# 离线地图：铺满 + 主题匹配 修复说明

## 问题
1. **没铺满**：缩放/平移时地图四周露出灰白块。
2. **不匹配主题**：深色大屏下地图仍是浅色 AMap 图，观感割裂。

## 根因
- `L.map('container', …)` 未设 `minZoom/maxZoom/maxBounds`，且**未调用 `map.invalidateSize()`**（Leaflet 经典坑：容器在挂载时尺寸未就绪，地图只渲染一半 → 露白）。
- 古城 z17/z18 瓦片只下到约 80–90%，缩到极限或平移出下载区即露白。
- 容器底色未设深色，未铺满处露出刺眼白块；反相滤镜缺少深色兜底。

## 改动（仅地图初始化 + CSS 视觉层，未触碰标点/数据/其他逻辑）
### `src/components/MapPanel.vue`（地图初始化）
- `L.map` 增加 `minZoom: 9`（与瓦片下限一致）、`maxZoom: 17`（古城 z18 仅下约 80%，封顶到覆盖完整层级）、`zoomSnap: 0.25`。
- 瓦片层增加 `minZoom/maxZoom`、`errorTileUrl`（1×1 透明兜底，避免破图白块）、`noWrap: true`。
- 初始化后调用 `map.invalidateSize()`，并 `setTimeout` 二次校正 + `window resize` 监听，彻底解决“只渲染一半/没铺满”。

### `src/styles/main.css`（仅视觉）
- 新增 `.leaflet-container { background: #0a1322 }`（深色兜底）：未铺满处显示深色而非白块，贴合大屏。
- 优化 `.leaflet-tile-pane` 反相滤镜 `invert(1) hue-rotate(180deg) brightness(.95) contrast(1) saturate(.5)`，深色主题下呈科技感暗色底图（水域保持蓝、路网呈线）。
- 配套 `[data-theme="light"]` 下 `filter: none` + 浅色底色兜底。
- 增强 `#container::after` 夜景氛围叠加。

## 验证
- `vite build` 通过：633 模块转换，0 错误。
- `public/tiles` 完好：4017 张 / 32M，覆盖 z9–z18，可铺满 1920×1080（含拖动余量）。
- 深色主题走 `/tiles`（浅色瓦片）+ CSS 反相，无需额外深色瓦片资源。

## 使用 / 备注
- 直接 `npm run dev` 即可，Leaflet 走 `/tiles/{z}/{x}/{y}.png`。
- 想看纯离线效果：浏览器打开 `public/tile-preview.html`。
- `public/tiles-dark/` 为早期未下全的残留（已被 `.gitignore` 的 `public/tiles-*/` 忽略、且未被任何代码引用）；本机安全删除机制锁定该目录，无法程序化删除，可在资源管理器中手动删除。
- 如需更高缩放细节（z17–z18 全覆盖），重跑 `node scripts/download-amap-tiles.mjs --cities=datong --min-zoom=17 --max-zoom=18`（注意高德限流，建议并发 ≤4）。
- 坐标提示：高德瓦片为 GCJ-02，Leaflet 默认 WGS-84，标点会偏 50–500m（本次未处理，需引入 proj4leaflet 或在 `useDevices.positions` 做坐标转换）。
