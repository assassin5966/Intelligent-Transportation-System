# GPS 实时位置接入前端（Vue 地图）方案

> 配套后端文档：`docs/GPS_LOCATION_INTEGRATION.md`
> 规范来源：`实时位置数据接入规范V1.0`（Pulsar `persistent://dtzhjg/device/location_realtime_v1`，JSON 消息）
> 范围：**仅方案，不写代码**。落地时按本文件逐项实施。

---

## 0. 关键结论（先看）

1. **地图技术栈 = Leaflet 1.9.4 + 项目自带离线瓦片**（`map-marking-system-vue/package.json` 已锁 `leaflet@^1.9.4`；`MapPanel.vue` 瓦片走 `/tiles/{z}/{x}/{y}.png` 同源路径）。
2. **坐标系 = WGS84 直投**：现有设备落点 `dev.positions[id] = {lng, lat}`，渲染时 `toLatLng(p) = [p[1], p[0]]`（即 `[lat, lng]`）。规范里的 `longitude/latitude` 也是 WGS84。**两者同基准，GPS 坐标可直接复用，无需火星坐标（GCJ-02）/BD-09 转换。**
3. **现有数据层模式可直接复用**：
   - `composables/useDevices.js`：REST 拉列表 + 维护响应式 `state` + `normalizeGeo` 经纬度归一化。
   - `composables/useRealtime.js`：`WebSocket` 主通道 + **REST 兜底轮询**（WS 不可用时每 6s 轮询），含断线指数退避重连。
   - `components/MapPanel.vue`：`L.map('container')` + `L.layerGroup` + `L.divIcon`（自定义 HTML 标点）+ `watch` 驱动增量重绘。
4. **后端 API 已就绪**（见后端文档）：`GET /api/location/devices`、`GET /api/location/devices/{id}`、`GET /api/location/stream`（SSE，首帧 `snapshot` + 后续 `update`）。

> ⚠️ **坐标系隐患（本次不做，留作备注）**：若将来底图从「项目自带离线瓦片」换成天地图/高德/百度等公共底图，公共底图是 GCJ-02/BD-09 加密的，届时需在前端加 `wgs84ToGcj02(lng,lat)` 转换，否则点位会整体偏移数百米。当前离线瓦片是 WGS84，无需转换。

---

## 1. 总体架构（推荐方案）

```
Pulsar(lcoation_realtime_v1)
   │  (后端已接入)
   ▼
backend  core/gps_location.py  →  Redis(最新位置+TTL)  →  REST / SSE API
   │
   ▼
Vue  composables/useGpsLocations.js   ← 新增：REST 拉快照 + SSE 订阅
   │   维护响应式 locations{} / trails{} / connected
   ▼
Vue  components/MapPanel.vue          ← 扩展：独立 GPS 图层(L.layerGroup)
   │   复用 toLatLng / L.divIcon 渲染 GPS 标点 + 朝向箭头 + 轨迹线
   ▼
地图打点（与现有摄像头标点同底图、同坐标系，互不污染）
```

**设计原则**：GPS 作为一个**独立图层 + 独立 composable**，不混入现有 `useDevices`（摄像头/卡口是「固定监控点位」，GPS 是「移动执法设备」，语义不同）。复用既有渲染与实时模式，但解耦数据来源。

---

## 2. 具体改动清单（文件级，仅描述职责）

### 2.1 新增 `src/composables/useGpsLocations.js`
职责（对齐 `useDevices` / `useRealtime` 的写法）：
- **响应式状态**：`locations`（按 `deviceId` 索引的 `LocationOut` 表）、`trails`（每个 `deviceId` 最近 N 个点的环形缓冲，用于画轨迹）、`loading`、`connected`（SSE 是否连上）、`lastUpdate`。
- **初始快照**：用项目现有 `API_BASE` + `axios`（来自 `src/api/client.js`）调 `GET /api/location/devices`，把返回的设备写进 `locations`，同时按 `positioningTime` 顺序预填 `trails`。
- **实时推送**：`new EventSource(API_BASE + '/api/location/stream')`，监听两类事件：
  - `snapshot`：首帧，整体覆盖 `locations`；
  - `update`：单设备增量，按 `deviceId` 更新 `locations`，并把新点 push 进 `trails`（限长，如最近 50 点）。
- **健壮性**：`onerror` 静默 + 指数退避重连（与 `useRealtime.scheduleReconnect` 同思路）；`onUnmounted` / `stop()` 里 `es.close()`。
- **类型归一**：`deviceType` 三值（`HANDHELD_RADIO` / `BODY_CAMERA` / `MOTORCYCLE_PATROL`）映射为中文标签与配色 key；`online` 字段（后端按 TTL 算）透出，前端再按 `lastUpdate` 超时（如 60s 无新帧）二次判定「失联」。
- **导出**：`{ state, start, stop, flyToDevice }`（与 `useDevices()` 形态一致）。

### 2.2 扩展 `src/components/MapPanel.vue`（主要改动）
复用文件已有的 `toLatLng`、`L`、`L.divIcon`、`overlayColors`、`theme`：
- `import { useGpsLocations } from '../composables/useGpsLocations.js'`，取 `gps = useGpsLocations()`。
- **图层**：新增 `let gpsLayer = null`（独立 `L.layerGroup()`）；可选 `let gpsTrailLayer = null`（轨迹线层）。
- **渲染函数 `renderGpsLayer()`**：遍历 `gps.state.locations`，对每个有效坐标设备：`L.marker(toLatLng([lng,lat]), { icon: L.divIcon({ html: 按 deviceType 分色的 GPS pin + 按 heading 旋转的箭头, ... }) })`；点击 → `L.popup` 展示设备详情（设备类型/速度/朝向/海拔/定位时间/在线状态）。
- **轨迹（可选）**：`renderGpsTrails()` 把每个 `deviceId` 的 `trails` 点连成 `L.polyline`（半透明，按类型配色）。
- **挂载时机**：在 `onMounted` 里 `gps.start()`（在 `load()` 之后或并行），并 `gpsLayer.addTo(map)`；`map.on('moveend'/'zoomend'/'resize')` 不必重投影 divIcon 标点（Leaflet marker 自动跟随），但轨迹层无需特殊处理。
- **响应式更新**：`watch(() => gps.state.rev, ...)`（在 composable 里维护一个变更计数器，类比 `dev.statsRev`）→ 调 `renderGpsLayer()` / `renderGpsTrails()`。初始或设备集合变化时可全量重绘，单点 update 时按需只改该 marker。
- **控制总线**：在现有的 `mapCtl.xxx = ...` 注册处补充：
  - `mapCtl.toggleGps = (show) => show ? gpsLayer.addTo(map) : map.removeLayer(gpsLayer)`
  - `mapCtl.toggleGpsTrail = (show) => ...`
  - `mapCtl.flyToGpsDevice = (id) =>` 用 `gps.state.locations[id]` 的经纬度 `map.flyTo(...)`（复用现有 `focusDevice` 思路）。
- **卸载清理**：`onBeforeUnmount` 里 `gps.stop()` + 移除 `gpsLayer` / `gpsTrailLayer`（与现有清理段落并列）。

### 2.3 新增 `src/components/GpsDevicePanel.vue`（可选，侧栏列表）
- 从 `useGpsLocations().state.locations` 渲染设备列表：**类型筛选**（三类型 + 全部）、**在线/离线筛选**、**点击列表项 → `mapCtl.flyToGpsDevice(id)`**。
- 样式对齐现有 `PolicePanel` / `PredictionPanel`（右侧信息栏）。

### 2.4 扩展 `src/api/endpoints.js`（可选，推荐）
- 新增 `listGpsLocations()`、`getGpsLocation(id)`，统一走项目 `axios` 封装（与 `listDevices()` 同风格），供 composable 调用，避免散落 URL。

### 2.5 扩展 `src/composables/useMapControl.js`（可选）
- 增加 `gpsVisible`、`gpsTrailVisible` 状态与对应 toggle，供顶栏/控制条开关使用（与现有 `mapCtl` 字段风格一致）。

### 2.6 样式
- GPS 标点样式（分色 + 朝向箭头 + 在线/离线态）追加到现有样式体系（scoped 或在 `MapPanel` 关联的 CSS 中），走 `--c-*` 主题令牌，深浅主题自动适配（与现有 `.cam-pin` 一致）。

---

## 3. 实时性机制选择

| 机制 | 用途 | 说明 |
|---|---|---|
| **SSE `/api/location/stream`**（首选） | 主通道 | `EventSource` 原生、单向、自动重连，无需手动心跳；后端已提供 `snapshot` + `update`。 |
| **轮询 `GET /api/location/devices`**（兜底） | SSE 不可用时 | 每 5–10s 拉一次整体列表覆盖 `locations`（参考 `useRealtime` 的 WS+REST 兜底：**仅当 SSE 断开才轮询**，正常时不双重刷新）。 |
| **不引入第二个 WS** | — | 避免与现有 `/ws` 主通道争用；SSE 已满足单向推送需求。 |

> 代理/旧浏览器若不支持 `EventSource`：用上面轮询兜底即可，无需 polyfill（初期设备规模小）。

---

## 4. 前端展示设计

- **三类设备分色图标**：手持电台 / 执法记录仪 / 摩托巡逻，各一主色（参考现有 `.cam-pin` 的 `kakou`/`biandao` 双色思路）。
- **朝向箭头**：pin 内 `<i>` 元素按 `heading` 旋转（`transform:rotate(...)`），直观显示行进方向。
- **在线/离线**：圆点色（绿=在线 / 灰=失联），依据后端 `online` 标记 + 前端 `lastUpdate` 超时二次判定。
- **点击详情**：`L.popup` 或复用现有 divIcon tip 风格，展示 `deviceId / deviceType / speed / heading / altitude / positioningTime / online`。
- **图层显隐**：右上角或控制条加「GPS 设备 / 轨迹」开关（接 `mapCtl.toggleGps*`）。
- **轨迹线**（可选）：每个设备最近 N 点 `L.polyline`，半透明分色，开关控制。

---

## 5. 配置与 mock

- **`.env`**：复用现有 `VITE_API_BASE`（指向 backend 8000）；可加 `VITE_GPS_ENABLED`（默认随后端 `pulsar_enabled`，true 时前端才启动 GPS 图层）。
- **Mock 演示**：在 `src/api/mock.js` 增加模拟 GPS 设备周期移动（类比现有 `MockSocket`），保证无后端/Pulsar 时地图也能演示 GPS 移动点位（与现有 USE_MOCK 模式一致）。

---

## 6. 验证与风险

**验证**：
1. 后端 `PULSAR_ENABLED=true` + `.env` 注入 `PULSAR_TOKEN`，启动 backend；
2. 浏览器打开地图 → 看到 GPS 设备在底图上以分色 pin 出现并随实时推送移动；
3. 关掉 Pulsar/SSE → 前端按 TTL/超时判离线（灰点）+ 轮询兜底生效；
4. **坐标对齐验证**：GPS 点与现有摄像头点位在同一张离线瓦片底图上无整体偏移（确认 WGS84 一致）。

**风险与对策**：
- *EventSource 不支持* → 轮询兜底（§3）。
- *设备规模大导致 marker 多* → 初期（手持/记录仪/摩托数量有限）直接 `divIcon` 即可；若后期上千点，再换 `L.canvasRenderer` 或点聚合（L.markercluster），本期不引入。
- *坐标系错位* → 务必确认离线瓦片是 WGS84（现有已如此）；若换公共底图需补 §0 的转换函数。

---

## 7. 与后端 API 对应关系（已建好，无需再改后端）

| 前端调用 | 后端端点 | 说明 |
|---|---|---|
| 初始快照 | `GET /api/location/devices`（`?deviceType=` 可选过滤） | 返回 `{devices:[LocationOut], count}`，含 `online` |
| 单设备 | `GET /api/location/devices/{id}` | 详情 |
| 实时推送 | `GET /api/location/stream`（SSE） | `snapshot`（首帧）+ `update`（单设备增量） |

`LocationOut` 字段：`deviceId / deviceType / longitude / latitude / speed / heading / altitude / positioningTime / extension / online`。
