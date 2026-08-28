# 点位到点位路径高亮功能 —— 临时停用操作记录

> 操作日期: 2026-08-21 · 目的: 临时注释停用路径高亮，保留完整代码结构以便快速恢复

---

## 一、功能定位

"点位到点位路径高亮"指地图上相邻点位之间沿真实道路绘制的 3D 立体高亮连线（路面层 + 箭头指引层 + 点击弹距离 InfoWindow）。涉及 4 个文件，调用链如下：

```
src/components/MapPanel.vue  ──import──►  src/utils/roadHighlight.js  （渲染器：3D MeshLine / Polyline 双层 + 流光箭头）
                                            （roadHighlight 不依赖寻路）
src/utils/roadPath.js       ──import──►  src/data/roadNetwork.js     （A* 寻路 + 路网数据，当前未被运行时引用）
```

> 说明：`roadPath.js` / `roadNetwork.js` 为"沿真实道路 A* 寻路"链路，当前未被任何运行时代码引用（属功能保留代码）；为保证功能边界完整，一并停用。

---

## 二、注释清单（4 文件 · 9 调用点）

### 1. `src/utils/roadHighlight.js` — 渲染器模块（整体块注释）
- 整个文件用单对 `/* ... */` 块注释包裹（文件首行开标记、末行闭标记）。
- 处理细节：原文件内 `/* noop */`、`/* 静态箭头亦可 */` 两处行内块注释改为 `//`，防止外层块注释提前终止。
- 恢复：删除首行开标记与末行闭标记即可。

### 2. `src/utils/roadPath.js` — A* 寻路工具（整体块注释）
- 整个文件用单对 `/* ... */` 块注释包裹。

### 3. `src/data/roadNetwork.js` — 路网数据（整体块注释）
- 整个文件用单对 `/* ... */` 块注释包裹。
- 仅被 `roadPath.js` 引用，二者必须同时停用/恢复。

### 4. `src/components/MapPanel.vue` — 9 处调用点（局部注释）

| # | 位置 | 内容 | 注释方式 |
|---|------|------|----------|
| 1 | L24 | `import { getRoadHighlight, buildAdjacencyEdges }` | 行注释 `//` |
| 2 | L102 | `let roadHL = null` | 行注释 `//` |
| 3 | L104 | `let roadInfoWindow = null` | 行注释 `//` |
| 4 | L821–849 | `highlightPointPaths()` + `closeRoadInfoWindow()` 函数定义 | 块注释 `/* ===== ... ===== */` |
| 5 | L876 | `changeCity` 内 `highlightPointPaths()` 调用 | 行注释 `//` |
| 6 | L946–947 | `renderStaticLayers` 内 `try { highlightPointPaths() }` | 行注释 `//` |
| 7 | L1043 | `mapCtl.refreshRoadPaths = () => highlightPointPaths()` 注册 | 行注释 `//` |
| 8 | L1068–1108 | 挂载渲染器 `getRoadHighlight()` + `_onSelect` InfoWindow 块 | 块注释 `/* ===== ... ===== */` |
| 9 | L1148–1149 | `onBeforeUnmount` 内 `roadHL.destroy()` + `closeRoadInfoWindow()` | 行注释 `//` |

> `useMapControl.js` L21 的 `refreshRoadPaths() {}` 空实现 stub 保留不动（无外部调用者，不影响运行）。

---

## 三、验证结果

### 构建验证（vite build）
```
vite v5.4.21 building for production...
✓ 32 modules transformed.
dist/index.html                   0.86 kB │ gzip:  0.70 kB
dist/assets/index-DfEPp97F.css   53.32 kB │ gzip: 11.04 kB
dist/assets/mock-jK69ilWh.js      8.72 kB │ gzip:  3.26 kB
dist/assets/index-BhsmeQDm.js   135.71 kB │ gzip: 50.56 kB
✓ built in 1.92s
```
- **0 错误、0 警告**，编译通过。
- 无未定义变量 / 未解析 import 报错 → 注释边界正确，未破坏其他逻辑。

### 产物残留检查
对 `dist/assets/*.js` 搜索 `roadHighlight | getRoadHighlight | highlightPointPaths | findPathAlongRoads | ROAD_ADJ | buildAdjacencyEdges` → **无匹配**。
- 路径高亮功能代码完全脱离产物包，无死代码残留。
- 其他核心功能（地图初始化 / 设备标点 / 区域圈地 / TipLayer 提示框 / 统计面板 / 视频接入）正常编译打包。

---

## 四、恢复方式

需要恢复时，按以下步骤取消注释（每处均已标注 `[临时停用]` 与 `恢复方式` 提示）：

1. **roadHighlight.js**：删除文件第一行 `/* ===...` 与最后一行 `*/`。
2. **roadPath.js**：同上。
3. **roadNetwork.js**：同上。
4. **MapPanel.vue**：
   - 取消 L24、L102、L104 的 `//` 行注释；
   - 删除 L821–849 与 L1068–1108 两处块注释的开闭标记（`/* ===== ... ===== */` 与 `===== ... ===== */`）；
   - 取消 L876、L946–947、L1043、L1148–1149 的 `//` 行注释。

恢复后重新 `npm run build` 验证即可。
