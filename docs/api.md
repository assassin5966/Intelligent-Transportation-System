# 前端对接 API 文档

> 智慧交管拥堵治理预警监控平台
> 版本: 0.7.0 · 更新日期: 2026-08-13

---

## 目录

1. [服务概述](#1-服务概述)
2. [通用约定](#2-通用约定)
3. [设备管理 API](#3-设备管理-api)
4. [实时统计 API](#4-实时统计-api)
5. [告警 API](#5-告警-api)
6. [时序预测 API](#6-时序预测-api)
7. [警力分配 API](#7-警力分配-api)
8. [事件接收 API（AI → 后端）](#8-事件接收-apiai--后端)
9. [WebSocket 实时推送](#9-websocket-实时推送)
10. [数据模型](#10-数据模型)
11. [错误码](#11-错误码)
12. [接入示例](#12-接入示例)
---

## 1. 服务概述

系统由两个独立服务组成，前端统一通过 **业务后端** 对接，AI 分析服务一般不直接暴露给前端。

| 服务 | 默认端口 | 说明 | 对前端是否暴露 |
|------|---------|------|---------------|
| 业务后端 (backend) | `8000` | 设备管理、统计、告警、预测、警力分配、WebSocket 推送、CORS | ✅ 是 |
| AI 分析服务 (ai) | `8001` | 视频拉流、检测跟踪、越线计数、视频异常识别、WebSocket 推送 | ⚠️ 仅 WebSocket |

> **建议**：前端 REST 请求全部发往后端 `8000`；实时画面/事件流连接 AI 服务 `8001` 的 WebSocket；统计/告警/预测/警力方案订阅后端 `8000` 的 WebSocket。

### 运维工具

后端提供静态文件服务，前端可直接访问内置的运维工具页面（设备计数线配置）：

```
http://<backend-host>:8000/static/device-config.html
```

该页面用于 WVP 同步设备的计数线配置：同步设备 → 截帧 → 画计数线和锚点 → 启流计数。详见 §3.4 / §3.8 / §3.6。

### 健康检查

```
GET http://<backend-host>:8000/health
```

**响应**
```json
{ "status": "ok", "service": "backend" }
```

```
GET http://<ai-host>:8001/health
```

**响应**
```json
{ "status": "ok", "service": "ai", "active_devices": 2 }
```

---

## 2. 通用约定

### 2.1 请求格式

- 所有 REST 接口均为 JSON 格式（`Content-Type: application/json`）。
- 除 WebSocket 外，所有接口均为标准 HTTP 请求。
- 时间字段统一使用 ISO 8601 格式（含时区），例如 `2026-07-30T08:21:07+00:00`。

### 2.2 响应格式

- 成功响应直接返回数据体（无外层 `{code, data}` 包装）。
- 错误响应遵循 FastAPI 标准格式：

```json
{ "detail": "错误描述" }
```

### 2.3 坐标系统

所有几何坐标（计数线、锚点、ROI）均使用**归一化坐标**，取值范围 `[0, 1]`：

- `x = 0, y = 0` 对应画面**左上角**
- `x = 1, y = 1` 对应画面**右下角**
- `y` 轴向下为正方向

> 画面左上角为坐标原点，与浏览器 Canvas 坐标系一致，便于前端直接渲染。

### 2.4 CORS 跨域

后端通过环境变量 `CORS_ORIGINS` 配置允许的前端来源（逗号分隔），默认 `*`。

生产环境应配置具体域名，例如：
```
CORS_ORIGINS=https://dashboard.example.com,https://admin.example.com
```

---

## 3. 设备管理 API

管理摄像头设备的注册、列表、删除。注册时会自动转发配置到 AI 服务启动视频处理管道。

### 3.1 注册设备

```
POST /api/devices
```

**请求体**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | ✅ | 设备唯一标识 |
| `name` | string | ✅ | 设备名称 |
| `stream_url` | string | ✅ | 视频流地址（RTSP / 本地文件） |
| `line_coords` | string | ❌ | 计数线 `"x1,y1,x2,y2"`，归一化 0-1，默认 `"0.5,0.1,0.5,0.9"` |
| `anchor_coords` | string | ❌ | 内侧锚点 `"x,y"`，归一化 0-1。锚点所在侧为"内侧"，用于判定 Enter/Exit 方向 |
| `count_only` | string | ❌ | 计数方向过滤：`null`=双向计数，`"enter"`=只计 Enter，`"exit"`=只计 Exit（仅接受小写枚举值，Pydantic 校验拒绝 `Enter`/`in`/`both` 等） |
| `camera_type` | string | ❌ | 摄像头类型：`null`=全部检测，`"vehicle"`=只检测机动车，`"person"`=只检测人流（含非机动车）（仅接受小写枚举值） |
| `roi_coords` | string | ❌ | ROI 多边形 `"x1,y1,x2,y2,..."`，归一化 0-1，至少 3 个顶点。仅在多边形内的目标参与计数 |
| `max_vehicles` | int | ❌ | 拥挤判断阈值：ROI 内最大车辆数（`>0` 时开启该设备拥挤判断，见 §4.4） |
| `gb_device_id` | string | ❌ | 国标设备 ID（WVP 同步设备自动填写，手动注册留空）|
| `gb_channel_id` | string | ❌ | 国标通道 ID（WVP 同步设备自动填写，手动注册留空）|

> `gb_device_id` / `gb_channel_id` 仅 WVP 自动同步设备才有值，手动注册时留空即可。前端可通过这两个字段是否非空判断设备来源（WVP 同步 vs 手动注册）。

**请求示例**
```json
{
  "id": "cam-gate-north",
  "name": "北门摄像头",
  "stream_url": "rtsp://192.168.1.100:554/stream1",
  "line_coords": "0.1,0.75,0.9,0.75",
  "anchor_coords": "0.5,0.9",
  "count_only": null,
  "camera_type": "vehicle",
  "roi_coords": "0.05,0.6,0.95,0.6,0.95,0.95,0.05,0.95",
  "max_vehicles": 10
}
```

**响应** `201 Created`
```json
{ "id": "cam-gate-north", "status": "registered" }
```

> ⚠️ AI 服务不可达时不会阻塞配置落库，但视频管道不会启动。前端可通过 AI 服务的 `/devices` 接口或 `/health` 的 `active_devices` 确认管道是否运行。

> 📌 **`count_only` 与 `current_*` 语义**：`count_only` 仅过滤「是否生成事件」，不改变事件对实时统计的影响。车流单向车道（`count_only="enter"`）只产生 `VehicleEnter`，`current_vehicles` 即累计进入数（无 `Exit` 事件对冲，为单向场景的预期语义）；人流摄像头恒为双向计数（`count_only=null`），`Enter`/`Exit` 自然对冲 `current_persons`。今日累计 `today_*` 同样按实际产生的事件累加。

### 3.2 查询设备列表

```
GET /api/devices
```

**响应** `200 OK`

```json
[
  {
    "id": "cam-gate-north",
    "name": "北门摄像头",
    "stream_url": "rtsp://192.168.1.100:554/stream1",
    "line_coords": "0.1,0.75,0.9,0.75",
    "anchor_coords": "0.5,0.9",
    "count_only": "",
    "camera_type": "vehicle",
    "roi_coords": "0.05,0.6,0.95,0.6,0.95,0.95,0.05,0.95",
    "max_vehicles": 10,
    "gb_device_id": "",
    "gb_channel_id": "",
    "status": "registered",
    "last_heartbeat": "2026-08-08T10:00:00+00:00"
  }
]
```

### 3.3 删除设备

```
DELETE /api/devices/{device_id}
```

**路径参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| `device_id` | string | 设备 ID |

**响应** `200 OK`
```json
{ "status": "deleted", "id": "cam-gate-north" }
```

**错误** `404` 设备不存在：
```json
{ "detail": "device not found" }
```

### 3.4 WVP 设备同步

手动触发一次 WVP 设备同步（与后台 `wvp_sync` 定时任务同一逻辑，需 `WVP_ENABLED=true`）。
新增通道入表 `status=synced`（不自动启流）；WVP 侧离线则停 AI pipeline 并标 `offline`；恢复则重新启流。

```
POST /api/devices/sync
```

**响应** `200 OK`
```json
{ "added": 1, "started": 0, "stopped": 0, "recovered": 0 }
```

**错误** `503` WVP 未启用 (`wvp_enabled=false`)。

### 3.5 刷新流地址

AI 服务断流重连时调用，后端转调 WVP `play/start` 返回新的 HTTP-FLV/RTSP 地址。仅 WVP 同步设备可用。

```
GET /api/devices/{device_id}/stream
```

**响应** `200 OK`
```json
{ "device_id": "GB-34020000001320000001-34020000001320000002", "stream_url": "http://zlm/live/xxx.flv", "stream_id": "xxx" }
```

**错误** `503` WVP 未启用 / `404` 设备不存在 / `400` 非 WVP 同步设备 / `502` WVP 点播失败。

### 3.6 前端播放地址

返回浏览器可直接播放的流地址（前端大屏/预览用；AI 拉流请走 3.5 刷新流地址）。按设备类型三分支翻译：

| 设备类型 | 翻译逻辑 | protocol |
|---------|---------|----------|
| WVP 同步设备 | `play/start` 取 FLV；配置 `ZLM_PUBLIC_BASE` 时重写地址前缀 | `flv` |
| RTSP 设备（MediaMTX 推流） | `rtsp://host:8554/{path}` -> `http://{宿主}:8888/{path}/index.m3u8`（HLS，`MEDIAMTX_PUBLIC_BASE` 可覆盖，空则按请求 Host 自动推导） | `hls` |
| http(s) 直配流地址 | 原样返回 | `flv` |

```
GET /api/devices/{device_id}/play
```

**响应** `200 OK`（RTSP/MediaMTX 设备示例）
```json
{
  "device_id": "mock-vehicle-01",
  "play_url": "http://172.16.168.9:8888/vehicle/index.m3u8",
  "protocol": "hls",
  "source": "mediamtx",
  "source_stream_url": "rtsp://rtsp-server:8554/vehicle"
}
```

WVP 设备额外返回 `stream_id`。前端按 `protocol` 选择播放器：`flv` -> flv.js，`hls` -> hls.js（Safari 原生）。

**错误** `404` 设备不存在 / `400` 无可播放流地址 / `502` WVP 点播失败。

### 3.7 启用 WVP 同步设备

为 `wvp_sync` 自动入表（`status=synced`/`offline`）的设备配置计数线并启流（`status -> online`）。手动注册设备请直接用 `POST /api/devices`。

```
POST /api/devices/{device_id}/enable
```

**请求体**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `line_coords` | string | ✅ | `"x1,y1,x2,y2"` 归一化 0-1 |
| `anchor_coords` | string | ❌ | `"x,y"` 内侧锚点 |
| `count_only` | string | ❌ | `enter`/`exit` 单向过滤 |
| `camera_type` | string | ❌ | `vehicle`/`person` |
| `roi_coords` | string | ❌ | ROI 多边形 `"x1,y1,x2,y2,..."` |
| `max_vehicles` | int | ❌ | 拥挤判断阈值：ROI 内最大车辆数（`>0` 时开启拥挤判断，见 §4.4） |

**响应** `200 OK`
```json
{ "device_id": "GB-...", "status": "online", "stream_url": "http://zlm/live/xxx.flv" }
```

### 3.8 WVP Webhook（预留）

接收 WVP 定制回调（设备上下线等），透传后触发一次同步。WVP 默认无对外 HTTP webhook，此端点供定制对接（如在 WVP 侧配置事件转发）。

```
POST /api/devices/wvp-webhook
```

**请求体** 任意 JSON（透传记录日志）。
**响应** 同步结果（同 3.4）；WVP 未启用时返回 `{"status":"skipped","reason":"wvp_disabled"}`。

### 3.9 截取设备画面（配置计数线用）

对未配置计数线的 WVP 同步设备（`status=synced`），截取一帧画面返回 JPEG 图片，供前端绘制计数线和锚点。

```
GET /api/devices/{device_id}/snapshot
```

**响应** `200 OK` — 直接返回 `image/jpeg` 二进制图片，前端可用 `<img src="...">` 直接加载。

**错误**

| 状态码 | 说明 |
|--------|------|
| `503` | WVP 未启用 |
| `404` | 设备不存在 |
| `400` | 非 WVP 同步设备（无 gb_device_id）|
| `502` | WVP 点播失败或截帧失败（设备可能离线）|
| `504` | 截帧超时（流未就绪，建议重试）|

> 截帧过程：后端调 WVP `play/start` 拿 FLV → cv2 拉一帧 → 编码 JPEG → `play/stop` 释放资源。整个过程约 3-8 秒。

#### 前端画线交互流程

WVP 同步设备的完整配置流程为 **同步(§3.4) → 截帧(§3.8) → 画线 → 启用(§3.6)**：

```
1. POST /api/devices/sync              → 设备入表 status=synced
2. GET  /api/devices/{id}/snapshot     → 返回一帧 JPEG 图片
3. 前端在图片上绘制计数线（2个端点）+ 点击锚点（1个点）
4. 像素坐标 → 归一化坐标（÷ 图片宽高）
5. POST /api/devices/{id}/enable       → 传 line_coords + anchor_coords → 启流计数
```

#### 坐标转换

前端拿到的图片尺寸即为视频分辨率。用户在图片上操作的像素坐标按以下公式转为归一化坐标：

```
归一化x = 像素x / 图片宽度
归一化y = 像素y / 图片高度

例: 图片 1920×1080, 用户在 (960, 810) 画了一个点
   → 归一化 = (960/1920, 810/1080) = (0.5, 0.75)
```

- **计数线**：用户画一条线（拖拽或点击两个端点），取两个端点的归一化坐标 → `"x1,y1,x2,y2"`
- **锚点**：用户在计数线某一侧点击一个点，取归一化坐标 → `"x,y"`。锚点所在侧 = 内侧 = Enter 方向

#### 前端实现示例

```javascript
// 1. 加载截图
const img = document.getElementById('snapshot');
img.src = `http://backend:8000/api/devices/${deviceId}/snapshot`;

// 2. 图片加载完成后获取实际显示尺寸
img.onload = () => {
  const rect = img.getBoundingClientRect();
  // rect.width / rect.height = 图片在页面上的显示尺寸
};

// 3. 用户在图片上画线（两个端点）+ 点击锚点
//    点击坐标转归一化: x / rect.width, y / rect.height

// 4. 提交配置启流
await fetch(`http://backend:8000/api/devices/${deviceId}/enable`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    line_coords: `${x1},${y1},${x2},${y2}`,   // 归一化 0-1
    anchor_coords: `${ax},${ay}`,               // 归一化 0-1
    camera_type: 'vehicle',                      // 可选
    count_only: 'enter'                          // 可选
  })
});
```

> ⚠️ 计数线应画在车流/人流**必经的截面**上（如门口、路口横截面），锚点点击在你想计为"Enter（进）"的那一侧。详见 §10.3 方向判定逻辑。

---

## 4. 实时统计 API

获取当前实时车辆/人员数量、今日累计进出量、活跃设备数。

### 4.1 实时统计

```
GET /api/stats/realtime
```

**响应** `200 OK`

| 字段 | 类型 | 说明 |
|------|------|------|
| `current_vehicles` | int | 当前在场车辆数 |
| `current_persons` | int | 当前在场人员数 |
| `today_vehicle_in` | int | 今日车辆进入累计 |
| `today_vehicle_out` | int | 今日车辆离开累计 |
| `today_person_in` | int | 今日人员进入累计 |
| `today_person_out` | int | 今日人员离开累计 |
| `active_devices` | int | 活跃设备数（已产生过事件的设备） |
| `updated_at` | string | 最后更新时间（ISO 8601） |

```json
{
  "current_vehicles": 42,
  "current_persons": 156,
  "today_vehicle_in": 380,
  "today_vehicle_out": 338,
  "today_person_in": 2100,
  "today_person_out": 1944,
  "active_devices": 3,
  "updated_at": "2026-07-30T08:21:07+00:00"
}
```

> 💡 前端大屏可每隔 5~10 秒轮询此接口刷新数字。

### 4.2 各设备分别计数

返回所有注册设备的分别计数（当前在场 + 今日累计 + 拥挤状态），含设备名称和状态。

```
GET /api/stats/devices
```

**响应** `200 OK`（数组，每个元素一个设备）

```json
[
  {
    "device_id": "cam-gate-north",
    "name": "北门摄像头",
    "camera_type": "vehicle",
    "status": "online",
    "max_vehicles": 10,
    "current_vehicles": 12,
    "current_persons": 0,
    "today_vehicle_in": 85,
    "today_vehicle_out": 73,
    "today_person_in": 0,
    "today_person_out": 0,
    "roi_vehicles": 12,
    "vehicle_flow_per_min": 34.0,
    "congested": false
  },
  {
    "device_id": "cam-square-south",
    "name": "南广场人流",
    "camera_type": "person",
    "status": "online",
    "max_vehicles": null,
    "current_vehicles": 0,
    "current_persons": 156,
    "today_vehicle_in": 0,
    "today_vehicle_out": 0,
    "today_person_in": 2100,
    "today_person_out": 1944,
    "roi_vehicles": 0,
    "vehicle_flow_per_min": 0.0,
    "congested": false
  }
]
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `device_id` | string | 设备 ID |
| `name` | string | 设备名称 |
| `camera_type` | string | 摄像头类型（`vehicle`/`person`/空）|
| `status` | string | 设备状态（`online`/`offline`/`synced`/`registered`）|
| `max_vehicles` | int \| null | 拥挤判断阈值（未配置为 `null`）|
| `current_vehicles` | int | 该设备当前在场车辆数 |
| `current_persons` | int | 该设备当前在场人员数 |
| `today_vehicle_in` | int | 该设备今日车辆进入累计 |
| `today_vehicle_out` | int | 该设备今日车辆离开累计 |
| `today_person_in` | int | 该设备今日人员进入累计 |
| `today_person_out` | int | 该设备今日人员离开累计 |
| `roi_vehicles` | int | 最近一次上报的 ROI 内瞬时车辆数（AI 每 2 秒上报，见 §4.4）|
| `vehicle_flow_per_min` | float | 最近一次上报的车流速度：最近 60 秒跨线车辆数折算为每分钟车流量（辆/分钟）|
| `congested` | bool | 是否拥挤（双阈值判定，见 §4.4）|

> 未产生过事件的注册设备也会返回，计数为 0。各设备今日累计按天隔离，跨天自动清零。

### 4.3 单个设备计数

返回指定设备的分别计数。

```
GET /api/stats/devices/{device_id}
```

**路径参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| `device_id` | string | 设备 ID |

**响应** `200 OK`

```json
{
  "device_id": "cam-gate-north",
  "name": "北门摄像头",
  "camera_type": "vehicle",
  "status": "online",
  "max_vehicles": 10,
  "current_vehicles": 12,
  "current_persons": 0,
  "today_vehicle_in": 85,
  "today_vehicle_out": 73,
  "today_person_in": 0,
  "today_person_out": 0,
  "roi_vehicles": 12,
  "vehicle_flow_per_min": 34.0,
  "congested": false
}
```

**错误** `404` 设备不存在：
```json
{ "detail": "device not found" }
```

### 4.4 拥挤判断

结合**区域车辆个数**（ROI 内瞬时车辆数）与**车流速度**（每分钟车流量）按**双阈值**判定设备是否拥挤。

- **车流速度（每分钟车流量）**：AI 在 60 秒滑动窗口内统计 `VehicleEnter/Exit` 跨线事件次数，折算为辆/分钟（车流停滞/缓行时趋近 0）。
- **区域车辆个数**：AI 每 2 秒统计一次 ROI 内当前跟踪到的车辆（car/truck/bus）数，上报后端。
- **拥挤判定**：`roi_vehicles >= max_vehicles` 且 `vehicle_flow_per_min < CONGESTION_MIN_FLOW`（默认 5 辆/分钟）时判定为拥挤。两者均需超过阈值，避免把「车多但仍在流动」误判为拥堵。
- **状态机去抖**：后端记录各设备拥挤状态（`sc:congestion:state:{device_id}`），仅在状态转移时产生告警——进入拥挤触发 `critical` 告警（onset），解除拥挤触发 `info` 告警（recovery）。

> **开启方式**：设备注册（§3.1）或启流（§3.7）时设置 `max_vehicles > 0`（运维页面「最大车辆数」输入框）。未配置则不做拥挤判定，但 AI 仍会上报数据（供查询）。

#### 拥挤数据上报（AI → 后端，周期调用）

AI 服务内部调用，**前端通常不直接使用**。

```
POST /api/stats/congestion
```

**请求体**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `device_id` | string | ✅ | 设备 ID |
| `roi_vehicles` | int | ✅ | ROI 内瞬时车辆个数（≥0）|
| `vehicle_flow_per_min` | float | ✅ | 最近 60 秒跨线次数折算的每分钟车流量（≥0）|

**请求示例**
```json
{ "device_id": "cam-gate-north", "roi_vehicles": 12, "vehicle_flow_per_min": 34.0 }
```

**响应** `200 OK`
```json
{
  "device_id": "cam-gate-north",
  "congested": false,
  "roi_vehicles": 12,
  "vehicle_flow_per_min": 34.0,
  "max_vehicles": 10
}
```

**错误** `404` 设备不存在。

#### 查询最新拥挤数据

```
GET /api/stats/congestion?device_id={device_id}
```

**查询参数**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `device_id` | string | ❌ | 指定设备 ID；不传返回所有上报过数据的设备 |

**响应** `200 OK`（数组，按设备返回最近一次上报数据）

```json
[
  {
    "device_id": "cam-gate-north",
    "roi_vehicles": 12,
    "vehicle_flow_per_min": 34.0,
    "updated_at": "2026-08-13T08:00:00+00:00"
  }
]
```

> 拥挤状态同时反映在 §4.2 / §4.3 的 `congested` 字段；产生告警时经 §5 告警 API 与后端 `/ws`（`type: alert`，`category: congestion`）推送。

---

## 5. 告警 API

获取系统触发的告警记录。告警基于 `configs/rules.yaml` 规则评估，同一规则 5 分钟内去重，最多保留 1000 条。

### 5.1 告警列表

```
GET /api/alerts?limit={limit}
```

**查询参数**

| 参数 | 类型 | 必填 | 默认 | 范围 | 说明 |
|------|------|------|------|------|------|
| `limit` | int | ❌ | 100 | 1~1000 | 返回最近 N 条告警 |

**响应** `200 OK`（按时间倒序）

```json
[
  {
    "id": 15,
    "rule_id": "vehicle_saturate_critical",
    "level": "critical",
    "category": "vehicle_saturate",
    "message": "车辆饱和红色告警 (305/300)",
    "value": 305,
    "threshold": 300,
    "created_at": "2026-07-30T08:15:22+00:00"
  },
  {
    "id": 14,
    "rule_id": "person_saturate_warning",
    "level": "warning",
    "category": "person_saturate",
    "message": "游客数量接近饱和 (8120/8000)",
    "value": 8120,
    "threshold": 8000,
    "created_at": "2026-07-30T07:42:10+00:00"
  }
]
```

**告警字段说明**

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 告警自增 ID |
| `rule_id` | string | 触发的规则 ID |
| `level` | string | 告警级别：`warning` / `critical` |
| `category` | string | 告警类别 |
| `message` | string | 告警消息（已格式化，含实际值与阈值） |
| `value` | float | 触发时的实际值 |
| `threshold` | float | 规则阈值 |
| `created_at` | string | 告警生成时间（ISO 8601） |

### 5.2 内置告警规则

| rule_id | metric | threshold | level | 说明 |
|---------|--------|-----------|-------|------|
| `vehicle_saturate_warning` | `current_vehicles` | 200 | warning | 车辆接近饱和 |
| `vehicle_saturate_critical` | `current_vehicles` | 300 | critical | 车辆饱和红色告警 |
| `person_saturate_warning` | `current_persons` | 8000 | warning | 游客接近饱和 |
| `person_saturate_critical` | `current_persons` | 10000 | critical | 人流饱和 |

#### 拥挤告警（双阈值，事件驱动）

拥挤告警不走 `rules.yaml`，由 AI 周期上报 + 后端双阈值判定（见 §4.4）触发：

| rule_id | level | 触发时机 | 说明 |
|---------|-------|---------|------|
| `congestion_{device_id}` | critical | onset（进入拥挤） | `roi_vehicles >= max_vehicles` 且车流速度低于阈值 |
| `congestion_{device_id}` | info | recovery（解除拥挤） | 状态转移回正常 |

告警对象含额外字段：`category="congestion"`、`phase`（`onset`/`recovery`）、`device_id`、`vehicle_flow_per_min`。前端可经 `GET /api/alerts` 与后端 `/ws`（`type: alert`）接收，在设备卡片上展示「拥挤」/「已解除」状态。

### 5.3 视频异常上报（AI → 后端）

AI 分析服务在视频流中检测到**黑屏**或**花屏**异常时上报。异常属事件驱动型告警，不走 `rules.yaml` 阈值评估，由 AI 管道直接注入告警流。前端经 `GET /api/alerts` 与后端 `/ws` 即可看到，无需额外对接。

```
POST /api/alerts/anomaly
```

**请求体**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `device_id` | string | ✅ | 设备 ID |
| `anomaly_type` | string | ✅ | `black_screen`（黑屏）/ `flower_screen`（花屏） |
| `phase` | string | ❌ | `onset`（异常开始，默认）/ `recovery`（恢复正常） |
| `scores` | object | ❌ | 检测信号分数（亮度/噪声/相关性等，供诊断） |

**响应** `201 Created`

```json
{
  "status": "ok",
  "alert": {
    "id": 18,
    "rule_id": "video_black_screen",
    "level": "critical",
    "category": "video_anomaly",
    "message": "设备 CAM001 检测到黑屏异常",
    "device_id": "CAM001",
    "anomaly_type": "black_screen",
    "phase": "onset",
    "scores": { "brightness": 0.0, "black_ratio": 1.0 },
    "created_at": "2026-08-02T14:05:00+00:00"
  }
}
```

> **去重**：同一设备同一异常同一 `phase` 在 `ANOMALY_COOLDOWN_SECONDS`（默认 60s）内只落一条；冷却内重复上报返回 `{"status":"deduplicated"}`。
> **级别**：`onset` → `critical`，`recovery` → `info`。AI 管道层另有连续确认去抖（`ANOMALY_CONFIRM_FRAMES`），仅在状态转移时上报。

---

## 6. 时序预测 API

基于 Chronos-2 模型，以 N 分钟区间的历史总人数序列（长度 30）预测下一个 N 分钟的总人数。车流按 `random(2,5)` 转化为人流后与实际人流加总。预测路由挂载在 `/api/prediction` 下。

### 6.1 触发预测

```
POST /api/prediction/predict
```

**请求体**：无需参数（使用配置的 `prediction_interval_minutes` 和 `prediction_series_length`）

**响应** `200 OK`

```json
{
  "predicted_total": 505,
  "interval_minutes": 15,
  "series_length": 30,
  "degraded": false,
  "history": {
    "person": [12, 7, 17, "..."],
    "vehicle": [11, 9, 8, "..."],
    "converted_vehicle": [39, 31, 26, "..."],
    "total": [51, 38, 43, "..."]
  },
  "generated_at": "2026-08-02T12:25:00+00:00"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `predicted_total` | int | 预测的下一个 N 分钟总人数（已取整） |
| `interval_minutes` | int | 预测区间（分钟），默认 15 |
| `series_length` | int | 历史序列长度，默认 30 |
| `degraded` | bool | `true` 表示 Chronos 推理异常，降级为趋势外推 |
| `history` | object | 历史序列明细（人流/车流/转化车流/总人数） |

**错误**

| 状态码 | 说明 |
|--------|------|
| `504` | 预测超时（推理超过 60 秒） |

### 6.2 查询最近一次预测结果

```
GET /api/prediction/latest
```

**响应** `200 OK`（返回调度器最近缓存的预测结果，格式同 6.1）

**错误** `404` 无缓存预测：
```json
{ "detail": "no cached prediction, call POST /api/prediction/predict first" }
```

### 6.3 预测服务健康检查

```
GET /api/prediction/health
```

**响应**
```json
{
  "status": "ok",
  "service": "prediction",
  "degraded": false
}
```

> `degraded: true` 表示 Chronos 模型推理异常，已降级为趋势外推，预测精度下降。

---

## 7. 警力分配 API

基于「区域注册 + 总警力设置 + 三阶段分配算法」（需求计算 → 比例分配+最小保障 → 贪心最近优先调度），自动产出各区域目标警力与调动方案。所有接口挂载在 `/api/police` 下。

### 7.1 注册警力区域

```
POST /api/police/regions
```

**请求体**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | ✅ | 区域唯一标识 |
| `name` | string | ✅ | 区域名称 |
| `center_x` | float | ✅ | 区域中心点 x（用于区域间距离计算） |
| `center_y` | float | ✅ | 区域中心点 y |
| `device_id` | string | ✅ | 关联设备 ID，用于读取该区域在场人数 |

**请求示例**
```json
{
  "id": "r1",
  "name": "北广场",
  "center_x": 0.3,
  "center_y": 0.4,
  "device_id": "cam-gate-north"
}
```

**响应** `201 Created`
```json
{ "id": "r1", "status": "registered" }
```

### 7.2 查询区域列表

```
GET /api/police/regions
```

**响应** `200 OK`
```json
[
  {
    "id": "r1",
    "name": "北广场",
    "center_x": 0.3,
    "center_y": 0.4,
    "device_id": "cam-gate-north",
    "current_officers": 6,
    "current_persons": 156
  }
]
```

### 7.3 删除区域

```
DELETE /api/police/regions/{region_id}
```

**响应** `200 OK`
```json
{ "status": "deleted", "id": "r1" }
```

**错误** `404` 区域不存在：
```json
{ "detail": "region not found" }
```

### 7.4 设置总警力

```
POST /api/police/total
```

**请求体**
```json
{ "total": 20 }
```

**响应** `200 OK`
```json
{ "total": 20, "status": "ok" }
```

**错误** `400` `total` 为负数。

### 7.5 查询当前分配状态

```
GET /api/police/allocation
```

**响应** `200 OK`
```json
{
  "total_officers": 20,
  "regions": [
    {
      "region_id": "r1",
      "name": "北广场",
      "current_officers": 6,
      "current_persons": 156
    }
  ]
}
```

### 7.6 触发分配优化

```
POST /api/police/optimize
```

**请求体**：无需参数。基于当前各区域人数、Chronos-2 预测总人数与已设置总警力执行三阶段分配算法。

**响应** `200 OK`
```json
{
  "total_officers": 20,
  "regions": [
    {
      "region_id": "r1",
      "name": "北广场",
      "device_id": "cam-gate-north",
      "current_crowd": 156,
      "predicted_crowd": 180.5,
      "demand": 168.2,
      "current_officers": 6,
      "target_officers": 9,
      "delta": 3
    }
  ],
  "movements": [
    { "from_region": "r2", "to_region": "r1", "count": 3, "distance": 120.5 }
  ],
  "summary": {
    "total_movements": 3,
    "coverage_score": 0.912,
    "efficiency_score": 0.845,
    "overall_score": 0.887,
    "generated_at": "2026-08-02T14:05:00+00:00"
  }
}
```

**regions 元素字段**

| 字段 | 类型 | 说明 |
|------|------|------|
| `current_crowd` | int | 该区域关联设备当前在场人数 |
| `predicted_crowd` | float | 按占比分摊的预测人数 |
| `demand` | float | 综合需求 = α × 当前 + β × 预测 |
| `current_officers` | int | 优化前该区域警力 |
| `target_officers` | int | 优化后目标警力 |
| `delta` | int | 目标 - 当前（正=需调入，负=需调出） |

**movements 元素字段**

| 字段 | 类型 | 说明 |
|------|------|------|
| `from_region` | string | 警力调出区域 |
| `to_region` | string | 警力调入区域 |
| `count` | int | 调动人数 |
| `distance` | float | 两区域中心欧氏距离 |

> **评分**：`coverage_score`（需求覆盖度）+ `efficiency_score`（调度效率）加权得 `overall_score`，均越接近 1 越优。
> 优化后各区域 `current_officers` 自动更新为 `target_officers`，作为下一轮调度的"当前分配"。

**错误** `400` 无注册区域或总警力为 0：
```json
{ "detail": "无注册区域或总警力为 0, 无法优化" }
```

### 7.7 查询最近一次分配方案

```
GET /api/police/plan
```

**响应** `200 OK`（格式同 7.6，返回最近一次自动/手动优化的缓存方案）

**错误** `404` 暂无方案：
```json
{ "detail": "暂无分配方案, 请先调用 POST /api/police/optimize" }
```

> **自动调度**：后端按 `prediction_interval_minutes` 周期自动执行分配并缓存方案，同时通过后端 `/ws` 以 `police_plan` 消息推送（见 §9.2）。

---

## 8. 事件接收 API（AI → 后端）

> 此接口由 AI 服务内部调用，**前端通常不直接使用**。列出仅供理解数据流。
>
> 此外 AI 服务每 30 秒调用 `POST /api/devices/{device_id}/heartbeat` 上报心跳，后端据此更新设备列表的 `last_heartbeat` 字段（前端只读，无需调用）。

AI 检测到越线事件后，将事件推送到后端以更新实时统计并触发告警评估。

```
POST /api/events
```

**请求体**

| 字段 | 类型 | 说明 |
|------|------|------|
| `device_id` | string | 设备 ID |
| `event_type` | string | 事件类型：`VehicleEnter` / `VehicleExit` / `PersonEnter` / `PersonExit` |
| `occurred_at` | string | 事件发生时间（ISO 8601） |

**事件类型与统计影响**

| event_type | current 影响 | today 累计影响 |
|------------|-------------|---------------|
| `VehicleEnter` | current_vehicles +1 | today_vehicle_in +1 |
| `VehicleExit` | current_vehicles -1 | today_vehicle_out +1 |
| `PersonEnter` | current_persons +1 | today_person_in +1 |
| `PersonExit` | current_persons -1 | today_person_out +1 |

**响应** `201 Created`
```json
{ "status": "ok", "event_type": "VehicleEnter" }
```

> 每条越线事件同时持久化到 Redis List `{prefix}:events`（保留最近 2000 条），可通过下方 §8.1 查询接口回溯。

### 8.1 事件历史查询

查询最近 N 条越线事件历史（AI 推送的全部 `VehicleEnter/Exit`、`PersonEnter/Exit` 事件均会落库）。前端大屏「最近事件」滚动列表、审计回溯均使用此接口。

```
GET /api/events?limit={limit}
```

**查询参数**

| 参数 | 类型 | 必填 | 默认 | 范围 | 说明 |
|------|------|------|------|------|------|
| `limit` | int | ❌ | 100 | 1~2000 | 返回最近 N 条事件（按时间倒序） |

**响应** `200 OK`（数组，按时间倒序，最新事件在前）

```json
[
  {
    "device_id": "cam-gate-north",
    "event_type": "VehicleEnter",
    "occurred_at": "2026-08-09T08:21:07+00:00",
    "created_at": "2026-08-09T08:21:07.123456+00:00"
  },
  {
    "device_id": "cam-square-south",
    "event_type": "PersonExit",
    "occurred_at": "2026-08-09T08:21:05+00:00",
    "created_at": "2026-08-09T08:21:05.789012+00:00"
  }
]
```

**字段说明**

| 字段 | 类型 | 说明 |
|------|------|------|
| `device_id` | string | 产生事件的设备 ID |
| `event_type` | string | `VehicleEnter` / `VehicleExit` / `PersonEnter` / `PersonExit` |
| `occurred_at` | string | 事件实际发生时间（ISO 8601，由 AI 推送时携带；离线回放为视频时间） |
| `created_at` | string | 后端落库时间（ISO 8601，UTC） |

> 💡 存储介质为 Redis List，`LPUSH` 头插 + `LTRIM 0 1999` 保留最近 2000 条，超过自动淘汰最早记录。实时统计与今日累计不受历史淘汰影响（分别存于独立 Hash）。

---

## 9. WebSocket 实时推送

系统提供**两个** WebSocket 端点，前端按需连接：

| 端点 | 地址 | 推送内容 |
|------|------|---------|
| AI 服务 | `ws://<ai-host>:8001/ws` | 越线事件 `crossing_event`、跟踪轨迹 `tracks`、视频异常 `video_anomaly` |
| 业务后端 | `ws://<backend-host>:8000/ws` | 实时统计 `stats`、告警 `alert`、预测 `prediction`、警力方案 `police_plan` |

两个端点均为服务端单向推送，前端无需上行消息（服务端会忽略客户端上行）。连接断开后需前端自行重连（建议指数退避，并在断连时提示用户）。

### 9.1 AI 服务 WebSocket

```
ws://<ai-host>:8001/ws
```

连接建立后服务端持续推送 JSON 消息，前端无需发送任何消息（服务端会忽略客户端上行消息）。连接断开后需前端自行重连。

> 建议前端实现自动重连机制（指数退避），并在断连时提示用户。

#### 越线事件 `crossing_event`

每当有目标跨过计数线并满足去重条件时推送。

```json
{
  "type": "crossing_event",
  "device_id": "cam-gate-north",
  "event_type": "VehicleEnter",
  "track_id": "12",
  "class_name": "car",
  "timestamp": "2026-07-30T08:21:07.123456",
  "cross_point": [0.48, 0.75],
  "cross_line": "main",
  "direction": "enter",
  "confidence": 0.92
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | 固定 `"crossing_event"` |
| `device_id` | string | 设备 ID |
| `event_type` | string | `VehicleEnter` / `VehicleExit` / `PersonEnter` / `PersonExit` |
| `track_id` | string | 跟踪 ID |
| `class_name` | string | 目标类别：`car` / `truck` / `bus` / `person` |
| `timestamp` | string | 事件时间（ISO 8601） |
| `cross_point` | float[2] | 跨线点归一化坐标 `[x, y]` |
| `cross_line` | string | 计数线标识 |
| `direction` | string | 方向：`enter`（进入内侧）/ `exit`（离开内侧） |
| `confidence` | float | 检测置信度 0~1 |

#### 跟踪轨迹 `tracks`

每帧推送当前画面所有跟踪目标，用于前端实时渲染目标框。

```json
{
  "type": "tracks",
  "device_id": "cam-gate-north",
  "frame_id": 1024,
  "timestamp": "2026-07-30T08:21:07.234567",
  "tracks": [
    {
      "track_id": "12",
      "class_name": "car",
      "bbox": [120.5, 240.0, 180.0, 200.0],
      "center": [150.25, 340.0],
      "confidence": 0.91,
      "age": 45,
      "velocity": [2.3, 5.1]
    }
  ]
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | 固定 `"tracks"` |
| `device_id` | string | 设备 ID |
| `frame_id` | int | 帧序号 |
| `timestamp` | string | 当前时间（ISO 8601） |
| `tracks` | array | 跟踪目标列表 |

**tracks 数组元素**

| 字段 | 类型 | 说明 |
|------|------|------|
| `track_id` | string | 跟踪 ID（同一目标跨帧保持一致） |
| `class_name` | string | 目标类别 |
| `bbox` | float[4] | 像素坐标边界框 `[x, y, width, height]`（左上角 + 宽高） |
| `center` | float[2] | 像素坐标中心点 `[cx, cy]` |
| `confidence` | float | 检测置信度 0~1 |
| `age` | int | 跟踪已持续的帧数 |
| `velocity` | float[2] \| null | 像素速度 `[vx, vy]`（可能为 null） |

> ⚠️ `tracks` 消息中的 `bbox` / `center` 为**像素坐标**（非归一化），需按实际视频分辨率渲染。`crossing_event` 中的 `cross_point` 为**归一化坐标**。

#### 视频异常 `video_anomaly`

检测到黑屏/花屏异常状态转移时推送（仅在 onset/recovery 时发送，非每帧）。异常同时会经后端 `POST /api/alerts/anomaly` 持久化为告警，后端 `/ws` 也会以 `{"type":"alert","data":{...}}` 推送同一告警。

```json
{
  "type": "video_anomaly",
  "device_id": "cam-gate-north",
  "anomaly_type": "flower_screen",
  "phase": "onset",
  "scores": {
    "brightness": 125.58,
    "spatial_noise": 49.21,
    "uniformity": 0.9907,
    "channel_corr": 0.5699,
    "temporal_diff": 56.48
  },
  "timestamp": "2026-08-02T14:05:00.123456"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | 固定 `"video_anomaly"` |
| `device_id` | string | 设备 ID |
| `anomaly_type` | string | `black_screen`（黑屏）/ `flower_screen`（花屏） |
| `phase` | string | `onset`（异常开始）/ `recovery`（恢复正常） |
| `scores` | object | 检测信号分数（`brightness`/`black_ratio`/`spatial_noise`/`uniformity`/`channel_corr`/`temporal_diff`） |
| `timestamp` | string | 事件时间（ISO 8601） |

> 建议前端在 `onset` 时给对应摄像头画面叠加红色边框与异常标签，`recovery` 时移除。

### 9.2 后端 WebSocket

```
ws://<backend-host>:8000/ws
```

连接建立后立即推送一次当前统计，之后每 2 秒（无其他消息时）推送一次 `stats`；告警 / 预测 / 警力方案变化时即时推送对应消息。

#### 实时统计 `stats`

每 2 秒心跳推送（或无其他消息时填充），格式同 `GET /api/stats/realtime`。

```json
{
  "type": "stats",
  "data": {
    "current_vehicles": 42,
    "current_persons": 156,
    "today_vehicle_in": 380,
    "today_vehicle_out": 338,
    "today_person_in": 2100,
    "today_person_out": 1944,
    "active_devices": 3,
    "updated_at": "2026-08-02T14:05:00+00:00"
  },
  "timestamp": "2026-08-02T14:05:00+00:00"
}
```

> 💡 前端大屏可优先订阅此后端 `stats` 推送，无需轮询 `GET /api/stats/realtime`。

#### 告警 `alert`

规则告警与视频异常告警触发时推送，`data` 格式同 `GET /api/alerts` 单条；视频异常告警额外含 `device_id` / `anomaly_type` / `phase` / `scores` 字段。

```json
{
  "type": "alert",
  "data": {
    "id": 18,
    "rule_id": "video_black_screen",
    "level": "critical",
    "category": "video_anomaly",
    "message": "设备 CAM001 检测到黑屏异常",
    "created_at": "2026-08-02T14:05:00+00:00"
  }
}
```

#### 预测 `prediction`

定时预测完成时推送，`data` 格式同 `POST /api/prediction/predict`。

```json
{
  "type": "prediction",
  "data": {
    "predicted_total": 505,
    "interval_minutes": 15,
    "series_length": 30,
    "degraded": false,
    "generated_at": "2026-08-02T14:05:00+00:00"
  }
}
```

#### 警力方案 `police_plan`

自动警力分配完成时推送，`data` 格式同 `GET /api/police/plan`。

```json
{
  "type": "police_plan",
  "data": {
    "total_officers": 20,
    "regions": ["..."],
    "movements": ["..."],
    "summary": { "overall_score": 0.887, "generated_at": "2026-08-02T14:05:00+00:00" }
  }
}
```

### 9.3 前端处理建议

- 根据 `type` 字段分发处理；建议同时连接 AI `/ws`（事件流）与后端 `/ws`（统计/告警/预测/警力）。
- AI `/ws`：
  - `crossing_event`：用于实时事件流展示、弹窗告警、计数器动画。
  - `tracks`：用于在视频画面上叠加目标框（高频消息，建议用 `requestAnimationFrame` 节流渲染）。
  - `video_anomaly`：用于在摄像头画面层叠加异常提示（低频，仅在状态转移时推送）。
- 后端 `/ws`：
  - `stats`：刷新大屏数字卡片（替代轮询）。
  - `alert`：告警列表与弹窗（含视频异常告警）。
  - `prediction`：更新预测趋势展示。
  - `police_plan`：更新警力分配可视化。
- 按 `device_id` 区分多路摄像头。

---

## 10. 数据模型

### 10.1 检测类别

AI 服务支持的目标检测类别（`_DETECTION_CLASSES`）：

| class_name | 说明 | camera_type=vehicle | camera_type=person |
|-----------|------|---------------------|--------------------|
| `car` | 小汽车 | ✅ | ❌ |
| `truck` | 卡车 | ✅ | ❌ |
| `bus` | 公交车 | ✅ | ❌ |
| `person` | 行人/非机动车 | ❌ | ✅ |

> `camera_type` 为 `null` 时检测全部类别。`person` 类型包含行人、电动车、自行车等非机动车。

### 10.2 坐标系统说明

| 场景 | 坐标类型 | 取值 | 说明 |
|------|---------|------|------|
| 设备配置（line/anchor/roi） | 归一化 | [0, 1] | 前端配置页面使用 |
| WebSocket `cross_point` | 归一化 | [0, 1] | 直接按比例映射到画面尺寸 |
| WebSocket `bbox` / `center` | 像素 | 实际像素 | 需按视频原始分辨率渲染 |

### 10.3 方向判定逻辑

- **内侧**：锚点（`anchor`）所在的一侧定义为内侧。
- **enter**：目标从外侧跨越计数线进入内侧。
- **exit**：目标从内侧跨越计数线离开到外侧。
- 方向通过目标位置相对计数线的法向量投影变化判定，支持任意角度的计数线。

---

## 11. 错误码

| HTTP 状态码 | 含义 | 触发场景 |
|------------|------|---------|
| `200` | 成功 | GET 请求成功 |
| `201` | 创建成功 | POST 注册设备 / 接收事件成功 |
| `400` | 请求参数错误 | 参数格式非法、取值不允许 |
| `404` | 资源不存在 | 设备 ID 不存在 / 无缓存预测 |
| `409` | 冲突 | 设备已存在（AI 服务） |
| `504` | 网关超时 | 预测推理超时 |

错误响应统一格式：
```json
{ "detail": "错误描述信息" }
```

---

## 12. 接入示例

### 12.1 JavaScript — 注册设备并监听事件

```javascript
// 1. 注册设备
const res = await fetch('http://backend:8000/api/devices', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    id: 'cam-001',
    name: '南门摄像头',
    stream_url: 'rtsp://192.168.1.100:554/stream',
    line_coords: '0.1,0.75,0.9,0.75',
    anchor_coords: '0.5,0.9',
    camera_type: 'vehicle'
  })
});
const data = await res.json();
console.log('注册结果:', data);

// 2. 连接 WebSocket 接收实时事件
const ws = new WebSocket('ws://ai:8001/ws');

ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  
  if (msg.type === 'crossing_event') {
    console.log(`[${msg.device_id}] ${msg.event_type} - ${msg.class_name}`);
    // 更新事件列表 / 触发告警动画
  } else if (msg.type === 'tracks') {
    // 在画面上绘制目标框 (注意 bbox 为像素坐标)
    msg.tracks.forEach(t => {
      // drawBox(t.bbox, t.class_name, t.track_id);
    });
  }
};

ws.onclose = () => {
  console.log('WebSocket 断开, 3 秒后重连');
  setTimeout(connectWs, 3000); // 自动重连
};
```

### 12.2 轮询实时统计

```javascript
async function refreshStats() {
  const res = await fetch('http://backend:8000/api/stats/realtime');
  const stats = await res.json();
  
  document.getElementById('currentVehicles').textContent = stats.current_vehicles;
  document.getElementById('todayVehicleIn').textContent = stats.today_vehicle_in;
  // ...
}

// 每 5 秒刷新
setInterval(refreshStats, 5000);
```

### 12.3 获取预测结果

```javascript
async function getForecast() {
  const res = await fetch('http://backend:8000/api/prediction/predict', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' }
  });
  const data = await res.json();
  console.log('预测下个' + data.interval_minutes + '分钟总人数:', data.predicted_total);
}

// 或获取最近一次缓存预测
async function getLatestForecast() {
  const res = await fetch('http://backend:8000/api/prediction/latest');
  if (res.ok) {
    const data = await res.json();
    console.log('缓存预测:', data.predicted_total);
  }
}
```

### 12.4 订阅后端 WebSocket（统计/告警/预测/警力）

```javascript
// 后端 /ws: 连接后自动每 2 秒推送 stats，并按事件推送 alert/prediction/police_plan
const ws = new WebSocket('ws://backend:8000/ws');

ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  switch (msg.type) {
    case 'stats':
      // 刷新数字卡片 (替代轮询 GET /api/stats/realtime)
      updateDashboard(msg.data);
      break;
    case 'alert':
      // 新告警 (含视频异常告警) -> 弹窗 + 列表
      showAlert(msg.data);
      break;
    case 'prediction':
      // 预测更新 -> 趋势图
      updateForecast(msg.data);
      break;
    case 'police_plan':
      // 警力分配方案 -> 可视化
      updatePolicePlan(msg.data);
      break;
  }
};

ws.onclose = () => setTimeout(connectBackendWs, 3000); // 自动重连
```

---

## 附录：服务部署端口速查

| 服务 | 端口 | 关键路径 |
|------|------|---------|
| 业务后端 | 8000 | `/health`, `/api/devices`, `/api/stats/*`（含 `/api/stats/congestion`）, `/api/alerts`, `/api/alerts/anomaly`, `/api/prediction/*`, `/api/police/*`, `/api/events`, `/ws`, `/static/*` |
| AI 分析服务 | 8001 | `/health`, `/devices`, `/ws` |
| Redis | 16379 (宿主) | 内部使用，前端无需访问 |

> 配置可通过环境变量覆盖，详见 `app/common/config.py`。关键变量：`BACKEND_PORT`、`AI_PORT`、`CORS_ORIGINS`、`REDIS_URL`。
