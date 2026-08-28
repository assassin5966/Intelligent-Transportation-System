# 智慧交管拥堵治理预警监控平台 · Vue3 前端

基于 `api(1).md`（前端对接 API 文档 v0.6.0）实现的**完整 Vue3 项目**。
页面视觉/布局严格沿用原 `map-marking-system.html` 大屏（顶栏 + AMap 3D 地图 + 四角坐标框 + 左右卡片栏 + 视频接入位），
并补齐文档定义的**全部接口对接与交互逻辑**（设备 CRUD、实时统计、告警、预测、警力分配、事件、双 WebSocket）。

---

## 一、运行方式

```bash
# 1. 安装依赖
npm install

# 2. 启动开发服务器（默认 http://localhost:5173）
npm run dev

# 3. 生产构建
npm run build
npm run preview
```

> 地图使用高德 JS API 2.0（CDN 引入），请用本地浏览器（Chrome/Edge）打开，并确保 Key 有效。
> 高德 Key / 安全密钥在 `index.html` 中，生产请替换为你自己的。

### 无后端也能跑（内置 Mock）

项目内置内存版 Mock 后端，提供两种使用方式，由 `src/api/config.js` 控制：

#### 1) 全量 Mock（`USE_MOCK`）—— 完全用内存后端替代真实接口
适合「没有后端，只想看完整交互」的演示场景，默认关闭。

```bash
# 方式一：环境变量
VITE_USE_MOCK=true npm run dev

# 方式二：URL 参数（最省事）
# 打开 http://localhost:5173/?mock=1
```

#### 2) 优雅降级（`MOCK_FALLBACK`）—— 仅接口【失败时】自动回退 Mock（默认开启）
适合「已有真实后端，但希望它挂掉时页面不至于白屏」的生产/联调场景。
**成功响应永远走真实接口，只有网络异常 / 超时 / HTTP 错误 / 返回空体时才用 Mock 兜底**，
因此完全不影响真实接口对接。会话内仅提示一次「已切换至本地模拟数据」。

```bash
# 关闭降级（严格直连真实后端，任何失败都会按原样报错）：
#   .env 设 VITE_MOCK_FALLBACK=false  或  打开 ?fallback=0
# 强制开启降级：
#   打开 ?fallback=1
```

> 降级触发条件：真实请求 `throw`（网络/超时/4xx/5xx）或 GET 读接口返回 200 + 空体。
> Mock 数据严格沿用 `api(1).md` 的字段与层级（`Device / RealtimeStats / Alert / Prediction / PolicePlan` 等），
> 并能驱动列表展示、详情查看、表单提交（增删改会写入内存，刷新列表即生效）等核心场景。

若要对接真实后端，关闭 Mock 并配置基地址（见 `.env` 或 `src/api/config.js`）：

```bash
# .env
VITE_API_BASE=http://<你的后端>:8000
VITE_AI_BASE=http://<你的AI服务>:8001
VITE_API_TOKEN=          # 可选 Bearer Token
VITE_USE_MOCK=false      # 关闭全量 Mock
VITE_MOCK_FALLBACK=true # 保留降级兜底（默认即开启，可设为 false 彻底关闭）
```

**彻底移除 Mock（不影响对接）**：将 `MOCK_FALLBACK`、`USE_MOCK` 置为 `false` 后，
`client.js` 中的 `fallbackToMock` 分支不会执行，且 `mock.js` 通过动态 `import()` 引入、
不会被打进真实请求路径；直接删除 `src/api/mock.js` 与 `client.js` 中对应分支即可，无需改动任何业务组件。

---

## 二、接口清单与对接映射（严格依据 api(1).md）

| 文档 | 方法 & 路径 | 前端落点 | 说明 |
|------|------------|---------|------|
| §1 | `GET /health` | `health()` | 健康检查 |
| §3.1 | `POST /api/devices` | 地图「➕ 添加设备」→ `registerDevice` | 注册摄像头（标点新增） |
| §3.2 | `GET /api/devices` | 初始加载 + 增删后刷新 | 设备列表 |
| §3.3 | `DELETE /api/devices/{id}` | 编辑弹窗「🗑 删除」 | 删除设备（标点删除） |
| §3.4 | `POST /api/devices/sync` | `syncDevices` | WVP 同步 |
| §3.5 | `GET /api/devices/{id}/stream` | 视频卡「刷新流地址」 | 刷新流地址 |
| §3.6 | `POST /api/devices/{id}/enable` | 编辑弹窗「保存」 | 配置计数线（编辑语义，文档无 PUT） |
| §3.8 | `GET /api/devices/{id}/snapshot` | `getDeviceSnapshotUrl` | 截取画面（图片） |
| §4.1 | `GET /api/stats/realtime` | 左①指标卡 + WS `stats` | 实时统计 |
| §4.2 | `GET /api/stats/devices` | 左②③ / 路口信息 | 各设备计数 |
| §4.3 | `GET /api/stats/devices/{id}` | `getDeviceStat` | 单设备计数 |
| §5.1 | `GET /api/alerts?limit=` | 右⑧告警卡 | 告警列表 |
| §5.3 | `POST /api/alerts/anomaly` | 「模拟上报黑屏异常」 | 视频异常上报 |
| §6.1 | `POST /api/prediction/predict` | 右⑨「触发预测」 | 时序预测 |
| §6.2 | `GET /api/prediction/latest` | 预测卡初始 | 最近预测 |
| §6.3 | `GET /api/prediction/health` | 「健康检查」 | 预测服务健康 |
| §7.1 | `POST /api/police/regions` | 左⑦「注册区域」 | 警力区域注册 |
| §7.2 | `GET /api/police/regions` | 警力卡列表 | 区域列表 |
| §7.3 | `DELETE /api/police/regions/{id}` | 警力卡「删」 | 删除区域 |
| §7.4 | `POST /api/police/total` | 警力卡「设置总警力」 | 设置总警力 |
| §7.5 | `GET /api/police/allocation` | `getAllocation` | 当前分配 |
| §7.6 | `POST /api/police/optimize` | 警力卡「触发优化」 | 分配优化 |
| §7.7 | `GET /api/police/plan` | 警力卡方案 | 最近方案 |
| §8.1 | `GET /api/events?limit=` | `listEvents` | 越线事件历史 |
| §9 | `ws://…:8000/ws` `ws://…:8001/ws` | `useRealtime` | 后端/AI 双 WebSocket |

> **地图标点 = 摄像头设备（§3）**：增→`POST`、查→`GET`+实时计数、删→`DELETE`、改→`POST /enable`（文档设备无 `PUT`，以"启用/重配置"承载编辑）。

---

## 三、代码结构与职责

```
src/
├─ api/
│  ├─ config.js       # 基地址 / Token / 超时 / USE_MOCK(全量) + MOCK_FALLBACK(降级) 开关
│  ├─ client.js       # 统一请求：JSON + Bearer + 超时(AbortController) + 错误{detail}拦截 + 状态码映射
│  ├─ endpoints.js    # 全部接口函数，JSDoc 标注参数/返回结构（字段命名严格沿用文档）
│  └─ mock.js         # 内存 Mock 后端（模拟全部端点，演示用）
├─ composables/
│  ├─ useToast.js     # 全局提示栈
│  ├─ useRealtime.js  # REST 轮询 + 双 WebSocket 订阅 + 指数退避重连
│  ├─ useDevices.js   # 设备(标点)状态 + CRUD + 地图落点
│  └─ useMapControl.js# 顶栏↔地图 控制总线（解耦）
├─ components/
│  ├─ TopBar.vue      # 标题 / 城市·配色·视角 / 时钟 / WS 状态 / 添加设备
│  ├─ MapPanel.vue    # AMap 3D 地图 + 范围框 + 设备标点 + 拖拽 + 添加/编辑
│  ├─ DeviceEditor.vue# 设备新增/编辑弹窗（文档字段）
│  ├─ StatBoard.vue   # 左③ / 右③ 卡片 + 嵌入 Video/Police/Alert/Prediction
│  ├─ VideoCard.vue   # 道路监控接入位（§3.5 流地址）
│  ├─ PolicePanel.vue # 警力区域 CRUD + 优化（§7）
│  ├─ AlertPanel.vue  # 告警列表 + 异常上报（§5）
│  ├─ PredictionPanel.vue # 预测触发 + 健康（§6）
│  ├─ ToastHost.vue   # 提示渲染
│  └─ LoadingOverlay.vue  # 加载遮罩
├─ styles/main.css    # 全局样式（严格沿用原大屏视觉/布局）
└─ App.vue            # 组装 + 启动实时层
```

---

## 四、字段合规与约定

- **请求体**均为 JSON（`Content-Type: application/json`）；成功直接返回数据体，无 `{code,data}` 包装（§2.2）。
- **错误**统一 `{ detail }`；状态码映射：`400` 参数 / `401` 认证 / `403` 权限 / `404` 不存在 / `409` 冲突 / `5xx` 服务端 / `504` 超时（§11）。
- **坐标**：计数线/锚点/ROI 为归一化 `[0,1]`（§2.3）；地图标点落点 `lng/lat` 仅前端维护（文档设备模型无地理字段），不写入后端请求体，确保后端请求严格合规。
- **`count_only` / `camera_type`**：空值以 `null` 发送（双向/全部检测），仅接受小写枚举（§3.1）。
- **鉴权**：文档未定义，按增强需求预留 `Authorization: Bearer <token>`，`401` 视为认证失败。

## 五、异常处理与边界

- 网络异常 / 超时 → 友好 Toast，不阻塞页面。
- 资源不存在(404) / 冲突(409) / 服务不可用(503/504) → 文案化提示。
- WebSocket 断线 → 指数退避自动重连 + 提示；轮询兜底保证数据持续刷新。
- 空数据 / 加载中 / 参数缺失 → 对应空态与禁用态，避免未捕获错误导致崩溃。
- 所有异步调用均 `try/catch`，错误经 `useToast` 统一反馈。
