# 前端对接 API 文档

> 智慧交管拥堵治理预警监控平台
> 版本: 0.1.0 · 更新日期: 2026-07-30

---

## 目录

1. [服务概述](#1-服务概述)
2. [通用约定](#2-通用约定)
3. [设备管理 API](#3-设备管理-api)
4. [实时统计 API](#4-实时统计-api)
5. [历史趋势 API](#5-历史趋势-api)
6. [告警 API](#6-告警-api)
7. [时序预测 API](#7-时序预测-api)
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
| 业务后端 (backend) | `8000` | 设备管理、统计、告警、预测、CORS | ✅ 是 |
| AI 分析服务 (ai) | `8001` | 视频拉流、检测跟踪、越线计数、WebSocket 推送 | ⚠️ 仅 WebSocket |

> **建议**：前端 REST 请求全部发往后端 `8000`；实时画面/事件流连接 AI 服务 `8001` 的 WebSocket。

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
| `count_only` | string | ❌ | 计数方向过滤：`null`=双向计数，`"enter"`=只计 Enter，`"exit"`=只计 Exit |
| `camera_type` | string | ❌ | 摄像头类型：`null`=全部检测，`"vehicle"`=只检测机动车，`"person"`=只检测人流（含非机动车） |
| `roi_coords` | string | ❌ | ROI 多边形 `"x1,y1,x2,y2,..."`，归一化 0-1，至少 3 个顶点。仅在多边形内的目标参与计数 |

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
  "roi_coords": "0.05,0.6,0.95,0.6,0.95,0.95,0.05,0.95"
}
```

**响应** `201 Created`
```json
{ "id": "cam-gate-north", "status": "registered" }
```

> ⚠️ AI 服务不可达时不会阻塞配置落库，但视频管道不会启动。前端可通过 AI 服务的 `/devices` 接口或 `/health` 的 `active_devices` 确认管道是否运行。

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
    "status": "registered"
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

## 7. 事件接收 API（AI → 后端）

> 此接口由 AI 服务内部调用，**前端通常不直接使用**。列出仅供理解数据流。

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

---

## 8. WebSocket 实时推送

AI 服务通过 WebSocket 向前端实时推送三类消息：**越线事件**、**跟踪轨迹**和**视频异常**。

### 8.1 连接

```
ws://<ai-host>:8001/ws
```

连接建立后服务端持续推送 JSON 消息，前端无需发送任何消息（服务端会忽略客户端上行消息）。连接断开后需前端自行重连。

> 建议前端实现自动重连机制（指数退避），并在断连时提示用户。

### 8.2 消息类型一：越线事件 `crossing_event`

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

### 8.3 消息类型二：跟踪轨迹 `tracks`

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

### 8.4 消息类型三：视频异常 `video_anomaly`

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

### 8.5 前端处理建议

- 根据 `type` 字段分发处理。
- `crossing_event`：用于实时事件流展示、弹窗告警、计数器动画。
- `tracks`：用于在视频画面上叠加目标框（高频消息，建议用 `requestAnimationFrame` 节流渲染）。
- `video_anomaly`：用于在摄像头画面层叠加异常提示（低频，仅在状态转移时推送）。
- 按 `device_id` 区分多路摄像头。

---

## 9. 数据模型

### 9.1 检测类别

AI 服务支持的目标检测类别（`_DETECTION_CLASSES`）：

| class_name | 说明 | camera_type=vehicle | camera_type=person |
|-----------|------|---------------------|--------------------|
| `car` | 小汽车 | ✅ | ❌ |
| `truck` | 卡车 | ✅ | ❌ |
| `bus` | 公交车 | ✅ | ❌ |
| `person` | 行人/非机动车 | ❌ | ✅ |

> `camera_type` 为 `null` 时检测全部类别。`person` 类型包含行人、电动车、自行车等非机动车。

### 9.2 坐标系统说明

| 场景 | 坐标类型 | 取值 | 说明 |
|------|---------|------|------|
| 设备配置（line/anchor/roi） | 归一化 | [0, 1] | 前端配置页面使用 |
| WebSocket `cross_point` | 归一化 | [0, 1] | 直接按比例映射到画面尺寸 |
| WebSocket `bbox` / `center` | 像素 | 实际像素 | 需按视频原始分辨率渲染 |

### 9.3 方向判定逻辑

- **内侧**：锚点（`anchor`）所在的一侧定义为内侧。
- **enter**：目标从外侧跨越计数线进入内侧。
- **exit**：目标从内侧跨越计数线离开到外侧。
- 方向通过目标位置相对计数线的法向量投影变化判定，支持任意角度的计数线。

---

## 10. 错误码

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

## 11. 接入示例

### 11.1 JavaScript — 注册设备并监听事件

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

### 11.2 轮询实时统计

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

### 11.3 获取预测结果

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

---

## 附录：服务部署端口速查

| 服务 | 端口 | 关键路径 |
|------|------|---------|
| 业务后端 | 8000 | `/health`, `/api/devices`, `/api/stats/*`, `/api/alerts`, `/api/prediction/*`, `/api/events` |
| AI 分析服务 | 8001 | `/health`, `/devices`, `/ws` |
| Redis | 6379 | 内部使用，前端无需访问 |

> 配置可通过环境变量覆盖，详见 `app/common/config.py`。关键变量：`BACKEND_PORT`、`AI_PORT`、`CORS_ORIGINS`、`REDIS_URL`。
