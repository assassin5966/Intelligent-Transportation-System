# 前端对接接口文档

> 智慧交管拥堵治理预警监控平台 · 前端专用
> 对应完整后端文档: [docs/api.md](./api.md) (v0.11.0) · 更新日期: 2026-08-27

本文档仅收录**前端需要对接**的接口，按前端使用场景组织。完整接口清单（含设备注册、WVP 同步、AI 内部上报等运维/后端向接口）见 `docs/api.md`。

---

## 目录

1. [服务接入总览](#1-服务接入总览)
2. [实时数据：WebSocket（推荐主通道）](#2-实时数据websocket推荐主通道)
3. [REST 接口（长期归档 / 预测）](#3-rest-接口长期归档--预测)
4. [数据约定速查](#4-数据约定速查)
5. [完整参考](#5-完整参考)

---

## 1. 服务接入总览

| 服务 | 地址 | 用途 | 前端对接 |
|------|------|------|---------|
| 业务后端 | `http://<backend-host>:8000` | WebSocket（统计/告警/预测/警力方案）+ REST（长期归档/预测）| ✅ 唯一入口 |

> ⚠️ **AI 分析服务（8001）不对前端开放**：前端所有数据一律通过业务后端 `8000` 获取，无需也不应直接连接 AI 服务。

- **健康检查**：`GET http://<backend-host>:8000/health` → `{"status":"ok","service":"backend"}`
- **跨域**：后端按环境变量 `CORS_ORIGINS`（逗号分隔）放行前端域名，生产环境应配置具体前端来源。
- 所有 REST 接口均为 JSON；成功直接返回数据体（无 `{code,data}` 包装），错误统一 `{"detail":"..."}`。

---

## 2. 实时数据：WebSocket（推荐主通道）

前端只连接**业务后端**的 WebSocket（AI 服务不对前端开放）。服务端单向推送，前端无需上行消息。建议前端实现指数退避自动重连。

### 2.1 连接 `ws://<backend-host>:8000/ws`（核心）

连接后**立即推送一次** `stats`，之后**每 2 秒**推送一次 `stats`；告警 / 预测 / 警力方案变化时**即时**推送对应消息。

消息按 `type` 字段分发，共 4 种：

| type | 触发时机 | 内容 |
|------|---------|------|
| `stats` | 连接即推 + 每 2 秒 | 全局统计（总人流/总车流）+ 各设备完整明细 |
| `alert` | 新告警触发（含视频异常、拥挤） | 单条告警 |
| `prediction` | 定时预测完成 | 预测结果 |
| `police_plan` | 自动警力分配完成 | 分配方案 |

#### `stats` 消息结构（前端核心）

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
  "devices": [
    {
      "device_id": "cam-gate-north",
      "name": "北门摄像头",
      "camera_type": "vehicle",
      "status": "online",
      "max_vehicles": 10,
      "max_persons": null,
      "longitude": 116.397128,
      "latitude": 39.916527,
      "category": "城墙出入口便道监控点位",
      "current_vehicles": 12,
      "current_persons": 0,
      "today_vehicle_in": 85,
      "today_vehicle_out": 73,
      "today_person_in": 0,
      "today_person_out": 0,
      "hour": 14,
      "hour_vehicle_in": 8,
      "hour_vehicle_out": 5,
      "hour_person_in": 0,
      "hour_person_out": 0,
      "roi_vehicles": 12,
      "roi_persons": 0,
      "vehicle_flow_per_min": 34.0,
      "person_flow_per_min": 0.0,
      "vehicle_congested": false,
      "person_congested": false,
      "vehicle_score": 0.5,
      "person_score": 0.0,
      "congestion_score": 0.5,
      "congested": false
    }
  ],
  "timestamp": "2026-08-02T14:05:00+00:00"
}
```

**`data`（全局合计，大屏数字卡片）**

| 字段 | 类型 | 说明 |
|------|------|------|
| `current_vehicles` | int | 当前在场车辆数（**总车流**）|
| `current_persons` | int | 当前在场人员数（**总人流**）|
| `today_vehicle_in` / `today_vehicle_out` | int | 今日车辆进入/离开累计 |
| `today_person_in` / `today_person_out` | int | 今日人员进入/离开累计 |
| `active_devices` | int | 活跃设备数（已产生过事件的设备）|
| `updated_at` | string | 最后更新时间（ISO 8601）|

**`devices`（每设备完整明细，地图打点 / 设备卡片 / 拥挤标记）**

| 字段 | 类型 | 说明 |
|------|------|------|
| `device_id` | string | 设备 ID |
| `name` | string | 设备名称 |
| `camera_type` | string | 摄像头类型：`vehicle`（只计车）/ `person`（只计人）/ 空（全检测）|
| `status` | string | 设备状态：`online` / `offline` / `synced` / `registered` |
| `max_vehicles` | int \| null | 车辆拥挤阈值（未配置为 `null`，`>0` 才做车辆拥挤判断）|
| `max_persons` | int \| null | 人流拥挤阈值（未配置为 `null`，`>0` 才做人流拥挤判断）|
| `longitude` / `latitude` | float \| null | 设备经纬度（按设备名称匹配 `device_info` 表，未匹配为 `null`）|
| `category` | string \| null | 点位分类（如「城墙出入口便道监控点位」「城墙入口车辆卡口点位」，按设备名称匹配 `device_info` 表，未匹配为 `null`）|
| `current_vehicles` / `current_persons` | int | 该设备当前在场车辆数 / 人员数 |
| `today_*` | int | 该设备今日进出累计（`today_vehicle_in/out`、`today_person_in/out`）|
| `hour` | int | 当前小时（0-23）|
| `hour_*` | int | 该设备**当前小时内**进出累计（`hour_vehicle_in/out`、`hour_person_in/out`）|
| `roi_vehicles` | int | 最近一次上报的 ROI 内瞬时车辆数 |
| `roi_persons` | int | 最近一次上报的 ROI 内瞬时人员数 |
| `vehicle_flow_per_min` | float | 车流速度：最近 60 秒跨线车辆数折算的每分钟车流量（辆/分钟）|
| `person_flow_per_min` | float | 人流速度：最近 60 秒跨线人数折算的每分钟人流量（人/分钟）|
| `vehicle_congested` | bool | 车辆维度拥挤（`roi_vehicles >= max_vehicles` 且车流速度低于阈值；未配 `max_vehicles` 恒为 `false`）|
| `person_congested` | bool | 人流维度拥挤（`roi_persons >= max_persons` 且人流速度低于阈值；未配 `max_persons` 恒为 `false`）|
| `vehicle_score` | float | 车辆维度拥挤度（0-1）|
| `person_score` | float | 人流维度拥挤度（0-1）|
| `congestion_score` | float | 加权综合拥挤度（0-1，人车混合按权重加权；可用于色阶展示）|
| `congested` | bool | 综合是否拥挤（只配车辆/人流时等于对应维度布尔；人车混合时 `congestion_score >= 0.5`）|

> 💡 前端大屏**只需订阅此 `stats` 消息**即可同时获得总人流/总车流（`data`）与各设备人流/车流/设备信息/拥挤状态/经纬度（`devices`），无需再轮询 REST。
> 未产生过事件的注册设备也会出现在 `devices` 中，计数为 0。

#### `alert` 消息

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

`data` 单条格式同 `api.md` §5.1 告警列表。**拥挤告警**：`category="congestion"`，额外含 `device_id`、`phase`（`onset`=进入拥挤 / `recovery`=解除）、`congestion_score`（综合拥挤度 0-1）、`vehicle_congested` / `person_congested`（触发维度），可用于设备卡片展示「拥挤 / 已解除」及维度标记。**视频异常告警**：`category="video_anomaly"`，额外含 `device_id` / `anomaly_type` / `phase` / `scores`。

#### `prediction` 消息

`data` 格式同 `POST /api/prediction/predict`（见 §3.2），用于更新预测趋势展示。

#### `police_plan` 消息

`data` 格式同 `GET /api/police/plan`（区域目标警力 + 调动方案 + 综合评分），用于警力分配可视化。

> 💡 实时越线事件、目标轨迹等**原始事件流由 AI 服务产生，不对前端开放**。前端所需的告警（拥挤、视频异常等）经后端 `alert` 推送获取；越线事件历史接口见 `api.md` §8.1。

---

## 3. REST 接口（长期归档 / 预测）

实时统计、设备信息、拥挤状态、经纬度、事件/告警均已由 WebSocket `stats` / `alert` 消息覆盖，**无需 REST**。REST 仅用于以下两个**低频查询**：

### 3.1 长期数据归档（小时级历史车流/人流）

```
GET /api/stats/hourly/history?device_id={device_id}&start_date={start}&end_date={end}
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `device_id` | string | ❌ | 指定设备；不传返回**全局合计**（聚合所有设备）|
| `start_date` | string | ✅ | 开始：`YYYY-MM-DD`（整日）或 `YYYY-MM-DD:HH`（精确到小时）|
| `end_date` | string | ✅ | 结束：同上，须不早于开始 |

**示例**：`GET /api/stats/hourly/history?start_date=2026-08-01:01&end_date=2026-08-22:08`

**响应**

```json
{
  "device_id": null,
  "start": "2026-08-01:01",
  "end": "2026-08-22:08",
  "total": { "vehicle_in": 100, "vehicle_out": 80, "person_in": 200, "person_out": 190 }
}
```

- `total`：整个时间范围（闭区间）四项计数的总和（`vehicle_in/out`、`person_in/out`）
- 数据来自 MySQL 长期归档（不受 Redis 30 天窗口限制）；后端未启用 MySQL 归档时返回 `503`

> 用途：日报/月报统计。逐小时明细/实时近 30 天的当前小时累计可直接用 WS `devices` 中的 `hour_*` 字段。

### 3.2 预测

| 接口 | 说明 |
|------|------|
| `POST /api/prediction/predict` | 触发预测（无需参数），返回下一个 N 分钟**总人数**预测 |
| `GET /api/prediction/latest` | 最近一次缓存预测结果（无缓存返回 `404`）|
| `GET /api/prediction/health` | 预测服务健康状态（含 `degraded` 降级标志）|

**`POST /api/prediction/predict` 响应**

```json
{
  "predicted_total": 505,
  "interval_minutes": 15,
  "series_length": 30,
  "degraded": false,
  "history": {
    "person": [12, 7, 17],
    "vehicle": [11, 9, 8],
    "converted_vehicle": [39, 31, 26],
    "total": [51, 38, 43]
  },
  "generated_at": "2026-08-02T12:25:00+00:00"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `predicted_total` | int | 预测的下一个 N 分钟**总人数**（已取整，含车流按 `random(2,5)` 转化的人流）|
| `interval_minutes` | int | 预测区间（分钟）|
| `degraded` | bool | `true` 表示 Chronos 推理异常，降级为趋势外推 |
| `history` | object | 历史序列明细（人流/车流/转化车流/总人数），用于画趋势对比图 |

> 定时预测完成后后端 WS 也会推 `prediction` 消息，前端连接 `/ws` 时无需再轮询 `latest`。

---

## 4. 数据约定速查

| 约定 | 说明 |
|------|------|
| 时间格式 | ISO 8601（含时区），如 `2026-08-02T14:05:00+00:00` |
| 设备状态 | `online`（计数中）/ `offline`（WVP 离线）/ `synced`（已同步未启流）/ `registered`（已注册未启流）|
| `camera_type` | `vehicle`=只计机动车，`person`=只计人流（含非机动车），空=全检测 |
| 经纬度 | 按设备**名称**匹配 `device_info` 表（数据源自 `data/device_geo.json` 固化导入，未启用 MySQL 时回落 JSON），未匹配为 `null`；前端应做空值兜底 |
| 点位分类 | `category` 按设备**名称**匹配 `device_info` 表（数据源自 `data/device_category.json` 固化导入），未匹配为 `null`，前端按 `null` 显示「未分类」 |
| 拥挤度 | `congestion_score` 0-1 连续分（色阶展示）；`congested` 综合布尔。判定：配置 `max_vehicles` 才判车辆拥挤、配置 `max_persons` 才判人流拥挤，两者都配置则按权重（默认 0.5/0.5）加权综合，均未配置则 `congested=false`、`congestion_score=0` |

---

## 5. 完整参考

- 全量接口文档（含设备注册、WVP 同步、业务规则配置、AI 内部上报）：[docs/api.md](./api.md)
- 设备计数线配置运维页面：`http://<backend-host>:8000/static/device-config.html`
- 业务规则配置运维页面：`http://<backend-host>:8000/static/business-rules.html`

> AI 分析服务（8001）的 WebSocket 消息结构（越线事件 / 目标轨迹 / 视频异常）见 `api.md` §9.1，**仅供后端集成参考，前端不直接对接**。
