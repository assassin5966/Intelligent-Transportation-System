# 智慧古城车辆人流监管平台 - 后端功能方案文档

> 基于项目实际代码编写，所有模块、接口、数据结构均来源于代码实现。
>
> 代码路径：`/Users/bianwei/Desktop/codes/DT`

| 组件 | 技术栈 | 端口 |
|------|--------|------|
| AI 分析服务 | YOLO11 + BoT-SORT + FastAPI | 8001 |
| 业务后端 | FastAPI + Pydantic v2 + Redis | 8000 |
| 数据存储 | Redis 7 (asyncio) | 6379 |
| 视频流网关 | WVP-Pro + ZLMediaKit (外部部署) | 18080 |
| 时序预测 | Chronos-2 (Chronos2Pipeline) | - |

---

## 一、系统架构设计

### 1.1 总体架构图

```
┌─────────────────────────────────────────────────────────────────────┐
│                         摄像头 / IPC (GB28181)                        │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ GB28181 SIP 信令 + RTP 媒体流
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    WVP-Pro (REST :18080) + ZLMediaKit                │
│                    设备管理 / 通道查询 / 点播控制                      │
│                    FLV/RTSP 流地址分发                                │
└──────────────┬──────────────────────────────┬───────────────────────┘
               │ REST API                      │ HTTP-FLV / RTSP
               ▼                               ▼
┌──────────────────────────┐    ┌─────────────────────────────────────┐
│    业务后端 (:8000)       │    │        AI 分析服务 (:8001)           │
│  FastAPI + Pydantic v2   │    │  YOLO11 + BoT-SORT + 越线计数        │
│                          │◄──►│  视频异常检测 (黑屏/花屏)             │
│  · 实时统计 / 告警引擎    │ HTTP│  · 拉流 -> 检测 -> 跟踪 -> 计数        │
│  · 设备管理 / WVP 同步    │推送│  · 事件推送 POST /api/events         │
│  · 警力分配 / 时序预测    │    │  · 心跳上报 POST /api/devices/.../hb │
│  · WebSocket 实时推送     │    │  · 异常上报 POST /api/alerts/anomaly │
└──────────┬───────────────┘    └──────────────────┬──────────────────┘
           │                                       │
           │          ┌──────────────┐             │
           └─────────►│  Redis (:6379)│◄────────────┘
                      │  实时状态/告警 │
                      │  设备配置/警力 │
                      │  预测缓存     │
                      └──────────────┘
                               │
                               ▼
                      ┌──────────────┐
                      │  前端大屏 WS  │
                      │  /ws 实时推送 │
                      └──────────────┘
```

**端口标注：**

| 服务 | 容器端口 | 宿主机映射 | 说明 |
|------|---------|-----------|------|
| 业务后端 | 8000 | 8000:8000 | REST API + WebSocket |
| AI 分析服务 | 8001 | 8001:8001 | 视频处理 + AI WebSocket |
| Redis | 6379 | 16379:6379 | 实时状态存储 |
| WVP-Pro | 18080 | 外部部署 | GB28181 设备管理 REST API |
| ZLMediaKit | 1935/554/80 | 外部部署 | RTMP/RTSP/HTTP-FLV 流媒体 |
| MediaMTX (测试) | 8554/1935/8888 | 同左 | 本地 RTSP 测试服务器 |

### 1.2 双服务架构说明

项目采用 **单镜像双服务** 架构，通过 Docker Compose 不同启动命令区分：

```yaml
# docker-compose.yml 核心配置
services:
  ai:
    image: smart-city-platform:latest      # 共用同一镜像
    command: ["python", "-m", "app.ai.service"]
    ports: ["8001:8001"]

  backend:
    image: smart-city-platform:latest      # 共用同一镜像
    command: ["uvicorn", "app.backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
    ports: ["8000:8000"]
```

| 维度 | 业务后端 (backend) | AI 分析服务 (ai) |
|------|-------------------|-----------------|
| 入口 | `app.backend.main:app` | `app.ai.service:app` |
| 端口 | 8000 | 8001 |
| 核心职责 | 实时统计/告警/设备管理/警力分配/预测调度 | 拉流/检测/跟踪/越线计数/异常检测 |
| GPU 需求 | 无 | 可选 (CUDA 12.1) |
| 后台调度器 | 4 个 (预测/心跳/警力/WVP) | 无 (每路管道独立 asyncio Task) |
| 对外通信 | REST + WebSocket -> 前端 | HTTP -> 后端 + WebSocket -> 前端 |
| 共享依赖 | Pydantic schemas / Redis / config | 同左 + YOLO / OpenCV / Chronos |

**单镜像优势：** 共享 `app/` 代码层（schemas、config、logger、redis_client），避免模型定义和配置重复维护；开发阶段通过 volume 挂载实现双服务热加载。

### 1.3 四个后台调度器

业务后端在 `lifespan` 启动周期中按序启动 4 个 asyncio 后台任务，关闭时逆序停止：

```
启动顺序:  预测调度器 -> 心跳检测 -> 警力调度器 -> WVP同步
关闭顺序:  WVP同步 -> 警力调度器 -> 心跳检测 -> 预测调度器 -> WVP客户端 -> Redis
```

| # | 调度器 | 源文件 | 触发间隔 | 核心职责 |
|---|--------|--------|---------|---------|
| 1 | 时序预测调度器 | `app/prediction/scheduler.py` | 每 N 分钟 (默认 15min) | 读历史区间序列 -> 车流转人流 -> Chronos-2 预测 -> 缓存 Redis -> 评估预测告警 -> WS 推送 |
| 2 | 摄像头离线检测 | `app/backend/core/heartbeat.py` | 每 30 秒 | 扫描设备心跳时间 -> 超 90s 标记离线 -> 触发 critical 告警 -> 恢复时标记 online |
| 3 | 警力分配调度器 | `app/backend/core/police_scheduler.py` | 每 N 分钟 (与预测同步) | 读各区域人数 + 预测缓存 -> 三阶段分配算法 -> 缓存方案 -> WS 推送 |
| 4 | WVP 设备同步器 | `app/backend/core/wvp_sync.py` | 每 30 秒 | 拉取 WVP 设备/通道 -> 比对本地 Redis -> 新增/启流/停流/恢复 |

> **容错设计：** 每个调度器启动包裹在独立 try/except 中，单个调度器启动失败不影响其他调度器和服务主体运行（`main.py` lifespan 实现）。

### 1.4 数据流架构

```
┌──────────┐     拉流      ┌──────────┐    POST /api/events     ┌──────────────┐
│ IPC/WVP  │─────────────►│ AI 服务  │────────────────────────►│  业务后端    │
│  (FLV)   │              │ (8001)   │    POST /api/alerts/     │  (8000)      │
└──────────┘              │          │    anomaly               │              │
                          │ YOLO11   │                          │ apply_event()│
                          │ BoT-SORT │    POST /api/devices/    │  ↓          │
                          │ 越线计数  │    .../heartbeat         │ Redis 更新   │
                          │ 异常检测  │────────────────────────►│  ↓          │
                          └──────────┘                          │ evaluate()  │
                                                                │  ↓          │
                          ┌──────────┐                          │ 告警去重     │
                          │  Redis   │◄─────────────────────────│  ↓          │
                          │ (6379)   │   读写实时状态/告警/配置   │ persist +   │
                          └──────────┘                          │ WS 推送     │
                                │                               └──────┬───────┘
                                │                                      │
                                │     ┌────────────────────────────────┘
                                │     │ WebSocket /ws
                                │     ▼
                                │  ┌──────────┐
                                └─►│ 前端大屏  │
                                   │ 4种消息   │
                                   └──────────┘
```

**核心数据流：**

1. **事件流：** AI 越线计数 -> `POST /api/events` -> `apply_event()` 更新 Redis -> `evaluate()` 评估告警 -> `broadcast_alert()` WS 推送
2. **异常流：** AI 异常检测 -> `POST /api/alerts/anomaly` -> 冷却去重 -> `persist_alert()` -> `broadcast_alert()` WS 推送
3. **预测流：** 调度器触发 -> `predict_total_persons()` -> Chronos-2 推理 -> 缓存 Redis -> `evaluate_prediction()` -> `broadcast_prediction()` WS 推送
4. **警力流：** 调度器触发 -> `optimize_allocation()` -> 三阶段算法 -> 缓存 Redis -> `broadcast_police()` WS 推送
5. **心跳流：** AI 每 30s -> `POST /api/devices/{id}/heartbeat` -> Redis 更新 -> 心跳检测器 30s 扫描 -> 离线告警

---

## 二、核心功能模块说明

### 2.1 实时统计模块（`app/backend/core/realtime.py`）

**职责：** 维护当前态（在场车辆/人员）、日累计（今日进出）、N 分钟区间计数（供预测）、逐设备在场人数（供警力分配）。

**输入：** 业务事件（VehicleEnter/Exit、PersonEnter/Exit）+ 事件发生时间

**输出：** `RealtimeStats` 统计字典

**关键逻辑：**

| 维度 | Redis Key 模式 | 数据类型 | 字段 | TTL |
|------|---------------|---------|------|-----|
| 当前态 | `sc:realtime:current` | Hash | current_vehicles, current_persons, updated_at | 无（持久） |
| 日累计 | `sc:realtime:daily:{YYYYMMDD}` | Hash | today_vehicle_in/out, today_person_in/out | 90 天 |
| N 分钟区间 | `sc:realtime:interval:{YYYYMMDDHHMM}` | Hash | vehicle_in/out, person_in/out | (30+10)×15×60 ≈ 6.7h |
| 逐设备 | `sc:realtime:device:{device_id}` | Hash | current_vehicles, current_persons | 24 小时 |
| 活跃设备 | `sc:devices:active` | Set | device_id 集合 | 无（持久） |

**`apply_event()` 处理流程：**
1. 根据 `EVENT_DELTA` 映射获取字段增量
2. 当前态字段 -> `HINCRBY sc:realtime:current` + `HINCRBY sc:realtime:device:{id}`
3. 日累计字段 -> `HINCRBY sc:realtime:daily:{date}` + `HINCRBY sc:realtime:interval:{time}`
4. 更新 `updated_at` 时间戳，`SADD` 活跃设备集合
5. 设置各 key 的 TTL（Pipeline 批量执行）
6. **负值钳位：** 当前态字段不允许为负（`_clamp_negatives` 异常事件/重启漂移修正）

**区间对齐算法：** `_interval_key()` 将时间向下取整到 N 分钟区间起点（`minute = (dt.minute // interval) * interval`），确保事件按发生时间落入正确时段（支持离线回放/延迟事件）。

### 2.2 告警引擎（`app/backend/core/alerts.py`）

**职责：** 读取 `rules.yaml` 规则 -> 评估实时/预测指标 -> 5 分钟去重 -> 持久化到 Redis List -> WebSocket 推送。

**输入：** 实时统计（`get_stats()`）/ 预测结果（`predict_total_persons()` 返回值）

**输出：** 新触发告警列表

**规则热重载机制：**

```
load_rules() / load_predict_rules()
  │
  ├── 缓存检查: _rules_cache != None?
  │     └── 是 -> 检查文件 mtime
  │           ├── mtime 未变 -> 返回缓存
  │           └── mtime 已变 -> 重新读取 YAML, 更新缓存 + mtime
  └── 否 -> 首次加载, 读取 YAML, 缓存
```

修改 `configs/rules.yaml` 后无需重启服务，下次 `evaluate()` 调用时自动重载。

**双规则体系：**

| 规则类型 | 加载函数 | 评估函数 | 去重 TTL | 触发条件 |
|---------|---------|---------|---------|---------|
| 实时规则 | `load_rules()` | `evaluate()` | 300 秒 (5 分钟) | `value >= threshold` |
| 预测规则 | `load_predict_rules()` | `evaluate_prediction()` | `interval_minutes × 60` | `predicted_value >= threshold` |

**现有规则（`configs/rules.yaml`）：**

| 规则 ID | 类别 | 指标 | 阈值 | 级别 |
|---------|------|------|------|------|
| vehicle_saturate_warning | vehicle_saturate | current_vehicles | 200 | warning |
| vehicle_saturate_critical | vehicle_saturate | current_vehicles | 300 | critical |
| person_saturate_warning | person_saturate | current_persons | 8000 | warning |
| person_saturate_critical | person_saturate | current_persons | 10000 | critical |
| predict_total_warning | predict_saturate | predicted_total | 500 | warning |
| predict_total_critical | predict_saturate | predicted_total | 1000 | critical |

**去重机制：** 使用 Redis `SET key "1" EX 300 NX`，同一规则 5 分钟内只触发一次。预测规则去重 TTL 为一个预测周期（默认 15 分钟）。

**持久化：** `persist_alert()` -> Redis `INCR` 分配自增 ID -> `LPUSH` 到 `sc:alerts` 列表 -> `LTRIM` 保留最近 1000 条。

### 2.3 设备管理（`app/backend/api/devices.py`）

**职责：** 手动注册/列表/删除设备、WVP 同步触发、截帧、启流、流刷新、心跳接收。

**设备配置字段（`DeviceIn`/`DeviceOut`）：**

| 字段 | 类型 | 说明 |
|------|------|------|
| id | str | 设备 ID |
| name | str | 设备名称 |
| stream_url | str | 视频流地址 |
| line_coords | str? | 计数线 "x1,y1,x2,y2" 归一化 0-1 |
| anchor_coords | str? | 内侧锚点 "x,y" 归一化 0-1 |
| count_only | str? | None=双向, "enter"=只进, "exit"=只出 |
| camera_type | str? | None=全部, "vehicle"=机动车, "person"=人流 |
| roi_coords | str? | ROI 多边形 "x1,y1,x2,y2,..." 归一化 0-1, ≥3 顶点 |
| gb_device_id | str? | 国标设备 ID (WVP 同步设备) |
| gb_channel_id | str? | 国标通道 ID (WVP 同步设备) |
| status | str | 设备状态 (registered/synced/online/offline) |
| last_heartbeat | str? | 最后心跳时间 ISO |

**关键功能：**

1. **手动注册（POST /api/devices）：** Redis HSET 落库设备配置（`status=registered`），**不启流**。启流统一由 `POST /{id}/enable` 负责（见第 3 条）。
2. **截帧（GET /{id}/snapshot）：** WVP `play/start` -> `cv2.VideoCapture` 拉一帧 -> JPEG 编码（质量 85）-> `play/stop` 释放。超时 15 秒。
3. **启流（POST /{id}/enable）：** 落库计数线配置。WVP 设备：`play/start` 取流地址 -> 转发 AI 启动管道；手动注册设备：先转发 AI `DELETE` 停旧管道 -> 再启动新管道。两种模式均标记 `status=online`。
4. **流刷新（GET /{id}/stream）：** WVP `play/start` -> 按协议选地址 -> 返回 `{stream_url, stream_id}`。AI 断流重连时调用。
5. **心跳（POST /{id}/heartbeat）：** 更新 `last_heartbeat` + `status=online`。AI 服务每 30 秒上报。
6. **坐标解析：** `_line_from_coords` / `_anchor_from_coords` / `_roi_from_coords` 解析字符串为坐标列表，格式非法时使用默认值并告警，坐标超出 [0,1] 时告警。

### 2.4 WVP 对接（`app/backend/core/wvp_client.py` + `app/backend/core/wvp_sync.py`）

#### WVPClient（REST 客户端）

**职责：** 封装 WVP-Pro REST API，异步单例，token 缓存 + 401 自动重登。

| 方法 | WVP API | 说明 |
|------|---------|------|
| `_login()` | `POST /api/login` | 获取 access-token，兼容 accessToken/access-token 字段名 |
| `list_devices()` | `GET /api/device/query/devices` | 分页拉取全部设备（每页 100），返回 `[{deviceId, name, online, ...}]` |
| `list_channels(dev_id)` | `GET /api/device/query/devices/{id}/channels` | 分页拉取通道列表 |
| `start_play(dev, ch)` | `GET /api/play/start/{dev}/{ch}` | 点播，返回 `{flv, rtsp, stream_id}`，兼容新版/旧版嵌套结构 |
| `stop_play(dev, ch)` | `GET /api/play/stop/{dev}/{ch}` | 停止点播 |
| `select_stream_url(play)` | - | 按 `wvp_play_protocol` 选地址，缺失时回退另一协议 |

**认证流程：**
- 首次请求无 token -> 自动 `_login()`
- 请求返回 401 -> 重新 `_login()` -> 重试一次
- token 缓存在内存（`self._token`），不落盘

**`wvp_enabled=False` 降级：** 所有方法返回空值（`[]` / `None` / `False`），调用方无需额外判空。

#### WVP 同步器（`wvp_sync.py`）

**职责：** 每 30 秒轮询 WVP，与本地 Redis 设备表比对，执行四态机状态转换。

**四态机模型：**

```
                    POST /api/devices
          ┌─────────────────────────────────────┐
          │                                     ▼
    ┌───────────┐  WVP同步新增   ┌───────────┐
    │ registered │               │  synced   │
    │ (手动注册)  │               │ (WVP入表)  │
    └─────┬─────┘               └─────┬─────┘
          │ POST /{id}/enable           │ POST /{id}/enable
          │ (配线+启流)                 │ (配线+启流)
          ▼                             ▼
    ┌───────────────────────────────────────┐
    │              online                   │
    │          (AI管道运行中)                 │
    └──────────────────┬────────────────────┘
                       │ WVP侧离线
                       ▼
    ┌───────────────────────────────────────┐
    │              offline                  │
    │          (AI管道已停止)                 │
    └───────────────────────────────────────┘
                       │ WVP恢复 + has_line
                       │ -> 重新启流
                       └─────────► online
```

**同步逻辑（`sync_once()`）：**

| 步骤 | 操作 | 结果 |
|------|------|------|
| 1 | 拉取 WVP 全量在线设备 + 通道 | `wvp_channels: {(gb_dev, gb_ch): name}` |
| 2 | 读本地 Redis 设备表（含 gb 映射） | `local: {(gb_dev, gb_ch): data}` |
| 3 | 查询 AI 服务在跑设备集合 | `running: {device_id, ...}` |
| 4 | 新通道入表 | `status=synced`，不启流（缺计数线） |
| 5 | 已有设备：WVP 离线 -> 停 AI + 标 offline | stopped++ |
| 6 | 已有设备：offline + WVP 恢复 + has_line -> 启流 + 标 online | recovered++ |
| 7 | 已有设备：online + AI 未跑（ZLM 重启） -> 重新启流 | started++ |

**camera_type 推断：** `_infer_camera_type(name)` 从设备名启发式推断（含"车"->vehicle，含"人"->person）。

### 2.5 警力分配（`app/backend/core/allocator.py`）

**职责：** 三阶段算法计算各区域目标警力 + 调度移动方案，缓存到 Redis。

**三阶段算法：**

```
阶段 1: 需求计算                    阶段 2: 目标分配                   阶段 3: 调度规划
┌─────────────────────┐          ┌─────────────────────┐          ┌─────────────────────┐
│ demand = α×current  │          │ base = min(min_per, │          │ surplus/deficit     │
│         + β×predict │──────►   │   total//m)         │──────►   │ 按距离升序排列      │
│                     │          │                     │          │ 贪心最近优先分配    │
│ α=0.3, β=0.7        │          │ remaining 按比例分  │          │ move_limit =        │
│ 预测按人流占比分摊   │          │ 取整修正到 total    │          │  ceil(total×0.5)   │
└─────────────────────┘          └─────────────────────┘          └─────────────────────┘
```

**阶段 1 - 需求计算：**
- 读取各区域关联设备的当前在场人数（`get_all_device_crowds()`）
- 读取 Chronos-2 预测缓存总人数（`_get_predicted_total()`）
- 预测值按各区域人流占比分摊：`predicted_crowd[rid] = predicted_total × (crowd[rid] / total_crowd)`
- 需求值：`demand = α × current_crowd + β × predicted_crowd`（α=0.3, β=0.7）
- 无数据时均匀分配（demand=1.0）

**阶段 2 - 目标分配（`_compute_targets()`）：**
- 警力不足（total < m）：按需求降序每区域最多 1 人
- 正常情况：基础保障 `base = min(min_per_region, total // m)` + 余量按需求比例分配
- 取整修正：Σ target ≠ total 时，按需求降序逐区域 ±1 调整

**阶段 3 - 调度规划（`_plan_movements()`）：**
- 计算 surplus（盈余）和 deficit（缺口）区域
- 所有 (surplus, deficit) 对按欧氏距离升序排列
- 贪心分配：`actual = min(surplus, deficit, remaining_limit)`
- 移动上限：`move_limit = ceil(total_officers × 0.5)`

**方案评分：**
- 覆盖率 `coverage = Σ(demand/max(1,target)) / m`
- 效率 `efficiency = 1 - total_move_dist / (total × max_dist)`
- 综合 `score = 0.7 × min(coverage,1) + 0.3 × max(0, efficiency)`

**缓存：** 方案 JSON 存入 `sc:police:plan:latest`，TTL = `prediction_interval_minutes × 60 × 2`。同时更新各区域 `current_officers` 为 target 值（下一轮的"当前分配"）。

### 2.6 时序预测（`app/prediction/chronos_model.py` + `app/prediction/scheduler.py`）

**职责：** 每 N 分钟读取历史序列 -> 车流转人流 -> Chronos-2 预测 -> 缓存 + 告警评估 + WS 推送。

#### ChronosPredictor（单例，懒加载）

| 属性 | 说明 |
|------|------|
| 模型 | Chronos-2-Small（28M 参数，`Chronos2Pipeline`） |
| 模型路径 | `models/` 目录（`config.json` 指定 `chronos_pipeline_class`） |
| 设备 | `cuda` if available else `cpu` |
| 加载方式 | 懒加载（首次 `predict()` 时触发 `_ensure_loaded()`） |

**预测流程：**
```
predict(history, horizon=1)
  │
  ├── _ensure_loaded()
  │     ├── 尝试 from chronos import Chronos2Pipeline
  │     │     └── 成功 -> _mode = "chronos"
  │     └── 失败 -> _mode = "naive", _degraded = True
  │
  ├── mode == "chronos"?
  │     ├── 是 -> _predict_chronos()
  │     │         ├── 输入: tensor[1, 1, len] (batch=1, n_variates=1)
  │     │         ├── pipeline.predict(prediction_length=horizon)
  │     │         ├── 沿 quantiles 维取均值 -> [horizon]
  │     │         └── 推理异常 -> 降级 _predict_naive()
  │     └── 否 -> _predict_naive()
  └── 返回 [max(0.0, float(v)) for v in forecast]
```

**降级方案（`_predict_naive()`）：**
- 取最近 24 个区间的线性趋势外推
- `trend = (last - first) / max(1, len-1)`
- `forecast[i] = max(0.0, last + trend × (i+1))`

#### 车流转人流（`convert_vehicle_to_person()`）

```
对每个区间的车流量:
  n = int(vehicle_count)
  if n <= 0: person = 0
  else: person = Σ random.randint(2, 5) for _ in range(n)  # 每车 2~5 人

总人数序列 = 人流序列 + 转化后车流序列
```

#### 预测调度器（`scheduler.py`）

**`_run_once()` 流程：**
1. 调用 `predict_total_persons()` 执行预测
2. 结果缓存到 `sc:prediction:latest:total`（TTL = 4 个区间）
3. `broadcast_prediction()` WebSocket 推送
4. `evaluate_prediction()` 预测告警联动

**`predict_total_persons()` 返回结构：**
```json
{
  "predicted_total": 350,
  "interval_minutes": 15,
  "series_length": 30,
  "history": {
    "timestamps": ["202608081000", "..."],
    "person": [120, 135],
    "vehicle": [40, 50],
    "converted_vehicle": [120, 150],
    "total": [240, 285]
  },
  "latest_interval": {"person": 135, "vehicle": 50, "converted_vehicle": 150, "total": 285},
  "degraded": false,
  "generated_at": "2026-08-08T10:15:00+00:00"
}
```

### 2.7 视频异常检测（`app/ai/anomaly.py`）

**职责：** 黑屏（black_screen）+ 花屏（flower_screen）检测，周期采样 + 连续确认去抖 + 状态机，仅在状态转移时产出 onset/recovery 事件。

#### AnomalyDetector（单帧检测）

**预处理：** 等比缩放到 `analysis_width`（默认 480px），使用 `INTER_NEAREST` 保留噪声（避免 `INTER_AREA` 平均化削弱花屏信号）。

| 异常类型 | 判定条件 | 信号 |
|---------|---------|------|
| 黑屏 | 灰度均值 < brightness(20) **AND** 近黑像素占比 > ratio(0.95) | brightness, black_ratio |
| 花屏 | 空间噪声 > std(35) **AND** 均匀度 > uniformity(0.6) **AND** (通道相关 < corr(0.5) **OR** 时域差分 > diff(25)) | spatial_noise, uniformity, channel_corr, temporal_diff |

**花屏多信号复合 AND 设计：**
- 空间噪声：NxN 块均标准差（8×8 网格）
- 均匀度：1 - 变异系数(CV)，噪声越均匀越接近 1
- 通道相关性：R-G 与 G-B 相关系数均值绝对值，花屏随机色->低相关
- 时域差分：与前一帧灰度均绝对差（无前帧时仅靠去相关）

#### AnomalyMonitor（状态机 + 去抖）

```
帧计数器 % check_interval(30) == 0?
  │ 否 -> 跳过 (每 ~1s@30fps 检测一次)
  │ 是
  ▼
detect(frame) -> anomaly_type
  │
  ├── anomaly_type != None
  │     ├── consecutive++
  │     ├── consecutive >= confirm_frames(2) AND state != type?
  │     │     └── state = type -> 返回 AnomalyEvent(type, "onset")
  │     └── 否 -> 返回 None
  │
  └── anomaly_type == None
        ├── consecutive = 0
        ├── state != None?
        │     └── old = state, state = None -> 返回 AnomalyEvent(old, "recovery")
        └── 否 -> 返回 None
```

**后端冷却去重：** `POST /api/alerts/anomaly` 端点使用 Redis `SET key "1" EX cooldown(60) NX`，同设备同异常同 phase 在 60 秒内只落一条告警。onset=critical，recovery=info。

### 2.8 越线计数（`app/ai/counter.py`）

**职责：** 单计数线 + 内侧锚点方案，通过法向量投影判定方向，7 层防抖 + 双向去重。

**核心数学：**
```
line = P1 -> P2,  anchor 位于内侧
内侧法向 n_inner: 左旋90° n=(-dy,dx), 用锚点归一化使法向指向锚点侧
offset(Q) = n_inner · (Q - P1):  >0 内侧, <0 外侧
跨线: offset 变号 (prev_off × curr_off < 0)
方向: 起始 offset < 0 且确认 offset > 0 -> Enter (外->内)
      起始 offset > 0 且确认 offset < 0 -> Exit  (内->外)
```

**7 层防抖机制：**

| 层 | 名称 | 机制 | 参数 |
|----|------|------|------|
| 1 | ROI 区域过滤 | 中心点不在多边形内（射线法）的轨迹跳过 | `roi_polygon` |
| 2 | ID 切换检测 | 速度 > 历史平均 N 倍且差距 ≥ M 像素 -> 清除跨线状态 | `id_switch_speed_ratio=3.0`, `id_switch_min_pixel=30` |
| 3 | 夹角过滤 | 运动向量在法向投影 < min_motion -> 视为沿线滑动 | `min_motion=2` |
| 4 | 投影范围检查 | 跨线点参数 t 需在有效段 [clip_t0, clip_t1] 内 | ROI 裁剪 |
| 5 | 远离计数线确认 | 轨迹距线距离 < min_distance_threshold -> 不确认 | `min_distance_ratio=0.02` |
| 6 | 端点误判过滤 | 连续两帧靠近线段端点 -> 过滤（仅经过不过滤） | `endpoint_sensitivity=0.05` |
| 7 | 滞留确认 + 滞回防抖 | 连续 hold_frames 帧保持在跨线后侧；侧别反转需超 hysteresis_threshold | `hold_frames=3`, `hysteresis_ratio=0.04` |

**双向去重：**
- `counted_tracks` 以 `track_id|direction` 为 key，允许同一轨迹来回各计一次
- 反向跨线冷却：同轨迹反向事件需间隔 `reverse_crossing_cooldown=3.0` 秒
- TTL 淘汰：已计数轨迹 300 秒后清除（防内存泄漏 + 流重连 ID 重用漏计）

**类别映射：** person/bicycle/motorcycle -> 人流（PersonEnter/Exit）；car/truck/bus -> 车流（VehicleEnter/Exit）

**ROI 裁剪：** 计数线 P1->P2 裁剪到 ROI 多边形内，取最长有效子段作为计数有效区间，越过 ROI 外线段不触发计数。

### 2.9 WebSocket 推送（`app/backend/api/ws.py`）

**职责：** 向前端大屏实时推送 4 种消息类型，队列管理 + 超时统计补推。

**4 种消息类型：**

| 类型 | type 字段 | 触发来源 | 数据内容 |
|------|----------|---------|---------|
| 实时统计 | `stats` | 连接时 + 每 2 秒超时补推 | current_vehicles/persons, today_*, active_devices |
| 告警 | `alert` | 告警引擎/异常上报/离线检测 | rule_id, level, category, message, value, threshold |
| 预测 | `prediction` | 预测调度器 | predicted_total, interval_minutes, history, degraded |
| 警力方案 | `police_plan` | 警力调度器 | total_officers, regions[], movements[], summary |

**队列管理：**
- 每个连接创建独立 `asyncio.Queue(maxsize=100)`
- `broadcast()` 遍历所有队列 `put_nowait()`，队列满时丢弃（`QueueFull`）
- `sender()` 协程：2 秒内无消息 -> 推送最新统计（超时补推机制）
- `receiver()` 协程：接收客户端消息保持连接（心跳/订阅）
- 连接建立时立即推送一次当前统计

**AI 服务 WebSocket（`/ws` 端口 8001）：**
- 推送 `crossing_event`（越线事件）、`tracks`（跟踪轨迹）、`video_anomaly`（视频异常）
- 供前端画面层实时渲染检测框和异常提示

---

## 三、API接口规范

### 3.1 业务后端 REST API（端口 8000）

#### 系统健康

| # | 方法 | 路径 | 说明 |
|---|------|------|------|
| 1 | GET | `/health` | 健康检查 |

**GET /health**
- 请求：无
- 响应：`{"status": "ok", "service": "backend"}`

#### 实时统计

| # | 方法 | 路径 | 说明 |
|---|------|------|------|
| 2 | GET | `/api/stats/realtime` | 当前车辆/人员数量、今日累计、活跃设备数 |

**GET /api/stats/realtime**
- 请求：无
- 响应（`RealtimeStats`）：
```json
{
  "current_vehicles": 150,
  "current_persons": 3200,
  "today_vehicle_in": 800,
  "today_vehicle_out": 650,
  "today_person_in": 15000,
  "today_person_out": 11800,
  "active_devices": 12,
  "updated_at": "2026-08-08T10:30:00+00:00"
}
```

#### 事件接收

| # | 方法 | 路径 | 说明 |
|---|------|------|------|
| 3 | POST | `/api/events` | AI 推送业务事件（越线计数） |

**POST /api/events**
- 请求体（`EventIn`）：
```json
{
  "device_id": "CAM001",
  "event_type": "VehicleEnter",
  "occurred_at": "2026-08-08T10:30:00+00:00"
}
```
- event_type 枚举：`VehicleEnter` / `VehicleExit` / `PersonEnter` / `PersonExit`
- 响应 201：`{"status": "ok", "event_type": "VehicleEnter"}`
- 错误码：400 - `invalid event_type`

#### 告警

| # | 方法 | 路径 | 说明 |
|---|------|------|------|
| 4 | GET | `/api/alerts` | 告警列表（Redis List 最近 1000 条，时间倒序） |
| 5 | POST | `/api/alerts/anomaly` | AI 上报视频异常（黑屏/花屏） |

**GET /api/alerts**
- 参数：`limit` (int, 默认 100, 范围 1-1000)
- 响应：`[{id, rule_id, level, category, message, value, threshold, created_at}, ...]`

**POST /api/alerts/anomaly**
- 请求体（`AnomalyAlertIn`）：
```json
{
  "device_id": "CAM001",
  "anomaly_type": "black_screen",
  "phase": "onset",
  "scores": {"brightness": 2.5, "black_ratio": 0.98}
}
```
- anomaly_type 枚举：`black_screen` / `flower_screen`
- phase 枚举：`onset`（异常开始）/ `recovery`（恢复正常）
- 响应 201：`{"status": "ok", "alert": {...}}` 或 `{"status": "deduplicated", "device_id": "CAM001"}`
- 错误码：400 - anomaly_type 或 phase 非法

#### 设备管理

| # | 方法 | 路径 | 说明 |
|---|------|------|------|
| 6 | GET | `/api/devices` | 设备列表 |
| 7 | POST | `/api/devices` | 注册设备（手动，仅保存配置不启流） |
| 8 | DELETE | `/api/devices/{device_id}` | 删除设备（转发 AI 停流） |
| 9 | POST | `/api/devices/{device_id}/heartbeat` | AI 心跳上报 |
| 10 | POST | `/api/devices/sync` | 手动触发 WVP 设备同步 |
| 11 | GET | `/api/devices/{device_id}/snapshot` | 截取一帧画面（JPEG） |
| 12 | GET | `/api/devices/{device_id}/stream` | 刷新并返回流地址 |
| 13 | POST | `/api/devices/{device_id}/enable` | 配置计数线并启流 |

**POST /api/devices** 请求体（`DeviceIn`）：
```json
{
  "id": "CAM001",
  "name": "北门摄像头",
  "stream_url": "http://zlm/live/x.flv",
  "line_coords": "0.5,0.1,0.5,0.9",
  "anchor_coords": "0.9,0.5",
  "count_only": null,
  "camera_type": "vehicle",
  "roi_coords": "0.1,0.1,0.9,0.1,0.9,0.9,0.1,0.9",
  "gb_device_id": null,
  "gb_channel_id": null
}
```
- 响应 201：`{"id": "CAM001", "status": "registered"}`

**POST /api/devices/{device_id}/enable** 请求体（`DeviceEnableIn`）：
```json
{
  "line_coords": "0.5,0.1,0.5,0.9",
  "anchor_coords": "0.9,0.5",
  "count_only": null,
  "camera_type": "vehicle",
  "roi_coords": null
}
```
- 响应 200：`{"device_id": "GB-xxx", "status": "online", "stream_url": "http://..."}`
- 错误码：400 - 非 WVP 同步设备；404 - 设备不存在；502 - WVP 点播失败；503 - WVP 未启用

**GET /api/devices/{device_id}/snapshot**
- 响应：`image/jpeg` 二进制
- 错误码：400 - 非 WVP 设备；404 - 设备不存在；502 - 截帧失败；503 - WVP 未启用；504 - 截帧超时

**GET /api/devices/{device_id}/stream**
- 响应：`{"device_id": "...", "stream_url": "http://...", "stream_id": "..."}`

**POST /api/devices/sync**
- 响应：`{"added": 1, "started": 0, "stopped": 0, "recovered": 0}`
- 错误码：503 - WVP 未启用

**POST /api/devices/{device_id}/heartbeat**
- 响应：`{"device_id": "CAM001", "heartbeat": "2026-08-08T10:30:00+00:00"}`
- 错误码：404 - 设备不存在

#### 警力管理

| # | 方法 | 路径 | 说明 |
|---|------|------|------|
| 15 | POST | `/api/police/regions` | 注册警力区域 |
| 16 | GET | `/api/police/regions` | 区域列表（含当前人数和警力） |
| 17 | DELETE | `/api/police/regions/{region_id}` | 删除警力区域 |
| 18 | POST | `/api/police/total` | 设置总警力数 |
| 19 | GET | `/api/police/allocation` | 当前分配状态 |
| 20 | POST | `/api/police/optimize` | 手动触发分配优化 |
| 21 | GET | `/api/police/plan` | 获取最近分配方案 |

**POST /api/police/regions** 请求体（`RegionIn`）：
```json
{
  "id": "R001",
  "name": "北门区域",
  "center_x": 100.5,
  "center_y": 200.3,
  "device_id": "CAM001"
}
```

**POST /api/police/optimize**
- 请求：无
- 响应（分配方案）：
```json
{
  "total_officers": 50,
  "regions": [
    {
      "region_id": "R001",
      "name": "北门区域",
      "device_id": "CAM001",
      "current_crowd": 320,
      "predicted_crowd": 450.0,
      "demand": 411.0,
      "current_officers": 10,
      "target_officers": 15,
      "delta": 5
    }
  ],
  "movements": [
    {"from_region": "R002", "to_region": "R001", "count": 5, "distance": 120.5}
  ],
  "summary": {
    "total_movements": 5,
    "coverage_score": 0.85,
    "efficiency_score": 0.92,
    "overall_score": 0.871,
    "generated_at": "2026-08-08T10:30:00+00:00"
  }
}
```
- 错误码：400 - 无注册区域或总警力为 0

#### 时序预测

| # | 方法 | 路径 | 说明 |
|---|------|------|------|
| 22 | GET | `/api/prediction/health` | 预测服务健康 + 降级状态 |
| 23 | POST | `/api/prediction/predict` | 手动触发预测 |
| 24 | GET | `/api/prediction/latest` | 获取缓存预测结果 |

**GET /api/prediction/health**
- 响应：`{"status": "ok", "service": "prediction", "degraded": false, "interval_minutes": 15, "series_length": 30, "vehicle_person_range": [2, 5]}`

**POST /api/prediction/predict**
- 请求：无
- 响应：见 [2.6 节预测返回结构](#26-时序预测apppredictionchronos_modelpy--apppredictionschedulerpy)

**GET /api/prediction/latest**
- 错误码：404 - 无缓存预测

### 3.2 AI 分析服务 REST API（端口 8001）

| # | 方法 | 路径 | 说明 |
|---|------|------|------|
| 25 | GET | `/health` | AI 服务健康 + 活跃设备数 |
| 26 | GET | `/devices` | AI 在跑设备列表 |
| 27 | POST | `/devices` | 注册设备启动管道 |
| 28 | DELETE | `/devices/{device_id}` | 停止设备管道 |

**POST /devices** 请求体（`DeviceRegister`）：
```json
{
  "device_id": "CAM001",
  "stream_url": "http://zlm/live/x.flv",
  "line": [[0.5, 0.1], [0.5, 0.9]],
  "anchor": [0.9, 0.5],
  "count_only": null,
  "camera_type": "vehicle",
  "roi": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]],
  "gb_device_id": null,
  "gb_channel_id": null
}
```
- 响应 201：`{"device_id": "CAM001", "status": "started"}`
- 错误码：400 - line 格式非法 / roi 顶点不足；409 - 设备已在运行；404 - 设备不存在（DELETE）

### 3.3 WebSocket 端点

| # | 路径 | 服务 | 说明 |
|---|------|------|------|
| 29 | `ws://host:8000/ws` | 业务后端 | 4 种消息（stats/alert/prediction/police_plan） |
| 30 | `ws://host:8001/ws` | AI 服务 | 3 种消息（crossing_event/tracks/video_anomaly） |

**后端 WS 消息格式：**
```json
{"type": "stats", "data": {"current_vehicles": 150, "...": "..."}, "timestamp": "2026-08-08T10:30:00+00:00"}
{"type": "alert", "data": {"id": 1, "level": "critical", "message": "..."}}
{"type": "prediction", "data": {"predicted_total": 350, "...": "..."}}
{"type": "police_plan", "data": {"total_officers": 50, "regions": ["..."], "...": "..."}}
```

---

## 四、数据模型设计

### 4.1 Pydantic 模型

| 模型 | 源文件 | 用途 | 关键字段 |
|------|--------|------|---------|
| `Detection` | `schemas/events.py` | 单帧检测结果 | id, class_name, bbox, confidence, center |
| `Track` | `schemas/events.py` | 跟踪轨迹 | track_id, class_name, bbox, center, confidence, age, velocity, history |
| `CrossingEvent` | `schemas/events.py` | 越线事件 | event_type, track_id, class_name, timestamp, camera_id, cross_point, cross_line, direction, confidence |
| `EventIn` | `schemas/events.py` | AI->后端事件输入 | device_id, event_type, occurred_at |
| `RealtimeStats` | `schemas/events.py` | 实时统计输出 | current_vehicles/persons, today_*, active_devices, updated_at |
| `AlertOut` | `schemas/events.py` | 告警输出 | id, level, category, message, value, threshold, created_at |
| `DeviceIn` | `api/devices.py` | 设备注册输入 | id, name, stream_url, line_coords, anchor_coords, count_only, camera_type, roi_coords, gb_device_id, gb_channel_id |
| `DeviceOut` | `api/devices.py` | 设备列表输出 | DeviceIn 字段 + status, last_heartbeat |
| `DeviceEnableIn` | `api/devices.py` | 启流配置输入 | line_coords(必填), anchor_coords, count_only, camera_type, roi_coords |
| `AnomalyAlertIn` | `api/alerts.py` | 异常上报输入 | device_id, anomaly_type, phase, scores |

**Pydantic v2 特性使用：**
- `BaseSettings` + `SettingsConfigDict`（`pydantic-settings`）
- `Field(..., description="...")` 描述字段
- `model_dump(mode="json")` 序列化（AI 推送事件时使用）

### 4.2 Redis 数据结构

**Key 命名规范：** `{redis_prefix}:{module}:{submodule}:{id}`，默认 prefix = `sc`

| Key | 类型 | 说明 | TTL |
|-----|------|------|-----|
| `sc:realtime:current` | Hash | 当前态: current_vehicles, current_persons, updated_at | 无（持久） |
| `sc:realtime:daily:{YYYYMMDD}` | Hash | 日累计: today_vehicle_in/out, today_person_in/out | 90 天 |
| `sc:realtime:interval:{YYYYMMDDHHMM}` | Hash | N分钟区间: vehicle_in/out, person_in/out | (30+10)×15×60s |
| `sc:realtime:device:{device_id}` | Hash | 逐设备在场: current_vehicles, current_persons | 24 小时 |
| `sc:devices:active` | Set | 活跃设备 ID 集合 | 无（持久） |
| `sc:alerts` | List | 告警列表 (JSON), 最近 1000 条 | 无（LTRIM 裁剪） |
| `sc:alert:seq` | String | 告警自增 ID 序列 | 无（持久） |
| `sc:alert:{rule_id}` | String | 实时告警去重 | 300s (5 分钟) |
| `sc:alert:{predict_rule_id}` | String | 预测告警去重 | interval×60s |
| `sc:alert:anomaly:{dev}:{type}:{phase}` | String | 异常告警去重 | 60s (cooldown) |
| `sc:alert:device_offline:{dev}` | String | 离线告警去重 | 300s (5 分钟) |
| `sc:device:{device_id}` | Hash | 设备配置 (全字段) | 无（持久） |
| `sc:police:regions` | Hash | 区域配置: field=region_id, value=JSON | 无（持久） |
| `sc:police:total` | String | 总警力数 | 无（持久） |
| `sc:police:plan:latest` | String (JSON) | 最新分配方案 | interval×60×2s |
| `sc:prediction:latest:total` | String (JSON) | 最新预测结果 | interval×60×4s |

**连接池配置：** `aioredis.ConnectionPool(max_connections=32, decode_responses=True)`，全局单例复用。

### 4.3 事件类型与增量映射（EVENT_DELTA）

```python
EVENT_DELTA = {
    "VehicleEnter": {"current_vehicles": +1, "today_vehicle_in": +1},
    "VehicleExit":  {"current_vehicles": -1, "today_vehicle_out": +1},
    "PersonEnter":  {"current_persons": +1, "today_person_in": +1},
    "PersonExit":   {"current_persons": -1, "today_person_out": +1},
}
```

| 事件类型 | 当前态增量 | 日累计增量 | 区间字段映射 |
|---------|-----------|-----------|-------------|
| VehicleEnter | current_vehicles +1 | today_vehicle_in +1 | vehicle_in +1 |
| VehicleExit | current_vehicles -1 | today_vehicle_out +1 | vehicle_out +1 |
| PersonEnter | current_persons +1 | today_person_in +1 | person_in +1 |
| PersonExit | current_persons -1 | today_person_out +1 | person_out +1 |

**字段分类处理：**
- `_CURRENT_FIELDS`（current_vehicles, current_persons）-> 写入 `sc:realtime:current` + `sc:realtime:device:{id}`，可正可负（钳位到 0）
- `_DAILY_FIELDS`（today_*）-> 写入 `sc:realtime:daily:{date}` + `sc:realtime:interval:{time}`，只增不减

---

## 五、业务流程说明

### 5.1 设备接入流程

```
┌─────────┐     WVP同步(30s)     ┌──────────┐
│  WVP    │────────────────────►│  后端     │
│ 设备/通道 │                     │ sync_once│
└─────────┘                     └────┬─────┘
                                     │ 新通道入表 status=synced
                                     ▼
┌──────────────────────────────────────────────────┐
│  前端 GET /api/devices/{id}/snapshot              │
│  -> WVP play/start -> cv2 拉一帧 -> 返回 JPEG        │
│  -> 前端在画面上绘制计数线 + 锚点 + ROI             │
└──────────────────────┬───────────────────────────┘
                       │ 前端 POST /api/devices/{id}/enable
                       │ {line_coords, anchor_coords, ...}
                       ▼
┌──────────────────────────────────────────────────┐
│  后端 enable_device()                              │
│  1. Redis HSET 落库计数线配置                      │
│  2. 取流: WVP设备 play/start 获取流地址;           │
│     手动设备直接用 stream_url (先停旧管道)         │
│  3. _start_ai_pipeline() -> POST ai:8001/devices   │
│  4. Redis HSET status=online                      │
└──────────────────────┬───────────────────────────┘
                       │ AI 服务收到 POST /devices
                       ▼
┌──────────────────────────────────────────────────┐
│  AI 服务 register()                                │
│  1. 创建 DevicePipeline                            │
│     - ByteTracker (YOLO11 + BoT-SORT)             │
│     - LineCrossingCounter (线+锚点+ROI)           │
│     - AnomalyMonitor (黑屏/花屏)                   │
│  2. 启动 asyncio Task: _run()                      │
│     - 心跳任务: 每 30s POST /api/devices/.../hb    │
│     - 拉流 -> 检测 -> 跟踪 -> 越线计数 -> 推送事件     │
└──────────────────────────────────────────────────┘
```

### 5.2 事件处理流程

```
AI 管道 _run() 每帧处理:
  │
  ├── stream_frames() 拉取一帧
  ├── AnomalyMonitor.check(frame) -> 异常事件?
  │     └── 是 -> POST /api/alerts/anomaly -> 后端冷却去重 -> 告警 + WS
  ├── tracker.track(frame) -> TrackResult (YOLO+BoT-SORT)
  ├── counter.process_tracks(tracks) -> List[CrossingEvent]
  │     └── 7层防抖 + 双向去重
  │
  └── 对每个 CrossingEvent:
        ├── _push(event): POST /api/events -> 后端
        │     │
        │     ▼
        │  后端 receive_event():
        │     ├── apply_event() -> Redis Pipeline 更新
        │     │     ├── HINCRBY current (当前态 + 逐设备)
        │     │     ├── HINCRBY daily (日累计)
        │     │     ├── HINCRBY interval (N分钟区间)
        │     │     ├── HSET updated_at
        │     │     ├── SADD active devices
        │     │     └── EXPIRE 各 key TTL
        │     ├── _clamp_negatives() -> 当前态负值钳位
        │     └── BackgroundTasks: evaluate()
        │           │
        │           ▼
        │        告警引擎 evaluate():
        │           ├── get_stats() 读实时统计
        │           ├── load_rules() 热重载规则
        │           ├── 遍历规则: value >= threshold?
        │           ├── SET dedup_key EX 300 NX -> 去重
        │           ├── persist_alert() -> LPUSH + LTRIM
        │           └── broadcast_alert() -> WS 推送
        │
        └── _broadcast_ws(event): AI WS 推送 crossing_event
```

### 5.3 断流自愈流程

```
stream_frames() 读流:
  │
  ├── cap.read() 失败 (ok=False / frame=None)
  │     │
  │     ▼
  │  ┌─────────────────────────────────────┐
  │  │ 步骤 1: 用当前 url 重连              │
  │  │  _open(url) 内含 tenacity:          │
  │  │    stop_after_attempt(5)            │
  │  │    wait_exponential(min=2, max=30)  │
  │  │    retry_if_exception_type(Stream)  │
  │  └──────────────┬──────────────────────┘
  │                 │
  │           重连成功? ──是──► continue 读流
  │                 │ 否
  │                 ▼
  │  ┌─────────────────────────────────────┐
  │  │ 步骤 2: url_provider 刷新地址        │
  │  │  (仅 WVP 同步设备, enable_url_refresh)│
  │  │                                      │
  │  │  refresh_cooldown = 10s (防频繁打WVP)│
  │  │  GET /api/devices/{id}/stream        │
  │  │    -> 后端 WVP play/start -> 新 FLV    │
  │  │  new_url != current_url?            │
  │  │    -> _open(new_url) 重连             │
  │  └──────────────┬──────────────────────┘
  │                 │
  │           重连成功? ──是──► continue 读流
  │                 │ 否
  │                 ▼
  │  logger.error("重连失败, 放弃")
  │  -> 管道停止 (心跳任务取消)
  │  -> 下次 WVP 同步(30s)检测到 AI 未运行 -> 重新启流
  │
  └── yield frame, idx (正常帧)
```

### 5.4 预测调度流程

```
预测调度器 _loop() (每 N=15 分钟):
  │
  ▼
_run_once():
  │
  ├── predict_total_persons()
  │     │
  │     ├── load_interval_history()
  │     │     ├── 读取近 30 个 N 分钟区间 (Pipeline 批量 HGETALL)
  │     │     ├── 每区间: person = person_in + person_out
  │     │     ├── 每区间: vehicle = vehicle_in + vehicle_out
  │     │     ├── 空区间补 0 (规则采样)
  │     │     └── 返回 (person_series, vehicle_series, timestamps) 时间升序
  │     │
  │     ├── convert_vehicle_to_person(vehicle_series)
  │     │     └── 每辆车 random.randint(2, 5) 人, 求和
  │     │
  │     ├── total_series = [p + v for p, v in zip(person, converted_vehicle)]
  │     │
  │     └── ChronosPredictor.instance().predict(total_series, horizon=1)
  │           ├── Chronos-2 推理 (60s 超时, asyncio.to_thread)
  │           └── 失败 -> 降级趋势外推
  │
  ├── Redis SET sc:prediction:latest:total (TTL=4区间)
  ├── broadcast_prediction() -> WS 推送
  └── evaluate_prediction(result)
        ├── load_predict_rules() 热重载
        ├── predicted_total >= threshold?
        ├── SET dedup_key EX interval×60 NX
        ├── persist_alert() -> Redis List
        └── broadcast_alert() -> WS 推送
```

### 5.5 警力分配流程

```
警力调度器 _loop() (每 N 分钟, 启动后等一个间隔):
  │
  ▼
_run_once():
  │
  ├── optimize_allocation()
  │     │
  │     │  ── 阶段 1: 需求计算 ──
  │     ├── _get_regions() -> Redis HGETALL sc:police:regions
  │     ├── _get_total_officers() -> Redis GET sc:police:total
  │     ├── get_all_device_crowds() -> 逐设备在场人数
  │     ├── _get_predicted_total() -> 读预测缓存
  │     ├── 预测按人流占比分摊到各区域
  │     └── demand[rid] = 0.3×current + 0.7×predicted
  │
  │     │  ── 阶段 2: 目标分配 ──
  │     ├── _compute_targets(demands, current, total, min_per_region)
  │     │     ├── base = min(1, total//m) 最小保障
  │     │     ├── remaining 按需求比例分配
  │     │     └── 取整修正 (Σ = total)
  │     │
  │     │  ── 阶段 3: 调度规划 ──
  │     ├── _plan_movements(targets, current, centers, move_limit)
  │     │     ├── 计算 surplus/deficit
  │     │     ├── (surplus,deficit) 对按距离升序
  │     │     └── 贪心: actual = min(surplus, deficit, limit)
  │     │
  │     ├── 评分: coverage + efficiency -> overall_score
  │     ├── 组装 plan JSON
  │     ├── Redis SET sc:police:plan:latest (TTL=2区间)
  │     └── 更新各区域 current_officers = target
  │
  └── broadcast_police(plan) -> WS 推送
```

---

## 六、技术选型依据

### 6.1 Web 框架

| 维度 | FastAPI（选用） | Flask | Django |
|------|-----------|-------|--------|
| 异步支持 | 原生 asyncio | 需 async 扩展 | ASGI 支持有限 |
| WebSocket | 原生支持 | 需 Flask-SocketIO | 需 Channels |
| 类型校验 | Pydantic v2 自动 | 手动 | DRF Serializer |
| 性能 | Starlette/Uvicorn 高 | 同步模型中等 | 全栈较重 |
| API 文档 | 自动 OpenAPI | 需扩展 | 需扩展 |
| **选型理由** | 原生异步 + WS + Pydantic 一体化，适合实时推送场景 | | |

### 6.2 数据存储

| 维度 | Redis（选用） | MySQL | MongoDB |
|------|---------|-------|---------|
| 读写延迟 | ~0.1ms | ~1ms | ~1ms |
| 数据模型 | Hash/List/Set/String | 关系表 | 文档 |
| TTL 支持 | 原生 | 需定时清理 | 需 TTL 索引 |
| 实时计数 | HINCRBY 原子 | UPDATE 行锁 | $inc 原子 |
| 发布订阅 | Pub/Sub | 无 | Change Streams |
| **选型理由** | 实时状态高频读写 + HINCRBY 原子计数 + TTL 自动过期，无需持久化历史 | | |

### 6.3 目标跟踪

| 维度 | BoT-SORT（选用） | ByteTrack | DeepSORT |
|------|-----------|-----------|----------|
| ID 切换率 | 低 (match_thresh=0.4) | 中 | 低 (需 ReID) |
| 外观特征 | 可选 ReID | 无 | 必须 ReID |
| 运动补偿 | GMC (固定摄像头关闭) | 无 | 卡尔曼 |
| 计算开销 | 中 | 低 | 高 (ReID 推理) |
| **选型理由** | BoT-SORT match_thresh 降至 0.4 大幅降低 IOU 匹配门槛，固定摄像头关闭 GMC 节省计算，无需额外 ReID 模型 | | |

### 6.4 时序预测

| 维度 | Chronos-2（选用） | ARIMA | Prophet |
|------|-------------|-------|---------|
| 模型类型 | 预训练大模型 (28M) | 统计模型 | 加法模型 |
| 多变量 | 支持 (n_variates) | 单变量 | 单变量 |
| 非线性 | 强 | 弱 | 中 |
| 降级方案 | 线性趋势外推 | - | - |
| **选型理由** | Chronos-2 预训练大模型适配多场景时序，懒加载 + 降级方案保证可用性 | | |

### 6.5 视频流协议

| 维度 | HTTP-FLV（选用） | RTSP | WebRTC |
|------|-----------|------|--------|
| 延迟 | 1-3s | 1-2s | <1s |
| 浏览器播放 | flv.js 直接 | 需插件 | 原生支持 |
| 服务端复杂度 | 低 (ZLM 原生) | 中 | 高 (STUN/TURN) |
| 穿透性 | HTTP 穿透好 | 需开端口 | 需信令服务器 |
| **选型理由** | HTTP-FLV 延迟可接受 + 浏览器 flv.js 直接播放 + ZLM 原生支持 + HTTP 穿透好 | | |

### 6.6 数据校验

| 维度 | Pydantic v2（选用） | Pydantic v1 |
|------|---------------|-------------|
| 性能 | Rust 核心, 5-50x | Python |
| 类型推断 | 原生 Python 类型 | 需 `Field` |
| 序列化 | `model_dump(mode="json")` | `.dict()` |
| Settings | `pydantic-settings` 独立包 | 内置 |
| **选型理由** | Rust 核心高性能 + 原生类型推断 + `model_dump` 灵活序列化 | |

### 6.7 部署架构

| 维度 | 单镜像双服务（选用） | 多镜像 |
|------|---------------|--------|
| 镜像构建 | 一次构建 | AI + 后端各一次 |
| 代码共享 | 同一 `app/` 层 | 需复制 schemas/config |
| 模型共享 | 同一 `models/` 挂载 | 各自挂载 |
| 独立扩缩 | Docker Compose command 区分 | 天然独立 |
| **选型理由** | 共享 schemas/config/logger/redis_client 代码层避免重复维护，compose 挂载实现热加载，command 区分服务 | |

---

## 七、性能与安全要求

### 7.1 性能指标

| 指标 | 目标值 | 依据 |
|------|--------|------|
| 单路推理延迟 | < 100ms | YOLO11n + BoT-SORT (CPU/GPU) |
| 并发路数 (CPU) | 4-8 路 | YOLO11n CPU 推理 + 帧跳过 |
| 并发路数 (GPU) | 16-32 路 | CUDA 加速 + 批处理 |
| Redis 读写延迟 | < 1ms | Pipeline 批量 + 连接池 (max=32) |
| WS 推送频率 | 0.5 Hz (统计) + 事件触发 | `_STATS_INTERVAL=2.0s` |
| 告警评估延迟 | < 50ms | BackgroundTasks 异步评估 |
| 预测推理延迟 | < 60s | `_PREDICT_TIMEOUT=60` 超时保护 |
| 截帧延迟 | < 15s | `asyncio.wait_for` 超时 15s |
| HTTP 客户端超时 | 10s | httpx.AsyncClient(timeout=10.0) |

### 7.2 资源限制

| 资源 | CPU 模式 | GPU 模式 | 说明 |
|------|---------|---------|------|
| CPU | 4-8 核 | 2-4 核 | AI 推理 + 后端服务 |
| GPU | 无 | 1× NVIDIA (共享) | YOLO11n + Chronos-2 推理 |
| 内存 | 4-8 GB | 4-8 GB | YOLO 模型 + Chronos 模型 + Redis 缓存 |
| 网络带宽 (单路) | 2-4 Mbps | 2-4 Mbps | HTTP-FLV 子码流 (wvp_stream_sub=true) |
| 网络带宽 (8 路) | ~32 Mbps | ~32 Mbps | 8 路并发拉流 |
| Redis 内存 | 128-512 MB | 同左 | 实时状态 + 告警(1000条) + 区间序列(40个) |
| 磁盘 (日志) | 100 MB/天 | 同左 | loguru 100MB 轮转 + 30 天保留 |
| 磁盘 (模型) | ~500 MB | ~500 MB | yolo11n.pt + Chronos-2-Small |

### 7.3 安全要求

| 安全维度 | 措施 | 实现位置 |
|---------|------|---------|
| CORS 配置 | 环境变量 `CORS_ORIGINS` 控制允许来源（逗号分隔），生产环境应限定前端域名 | `main.py` CORSMiddleware |
| WVP 认证 | token 缓存在内存（不落盘），401 自动重登，兼容 accessToken/access-token 字段 | `wvp_client.py` `_login()` / `_request()` |
| Redis ACL | 连接池通过 URL 认证（`redis://:password@host:port/db`），生产环境启用 Redis ACL | `redis_client.py` `ConnectionPool.from_url()` |
| API 输入校验 | Pydantic v2 自动校验请求体类型/枚举/范围；`event_type` 白名单校验；`anomaly_type`/`phase` 枚举校验 | 各 API 路由 + `schemas/events.py` |
| 坐标范围校验 | `_warn_if_out_of_range` 检测归一化坐标超出 [0,1] 并告警 | `devices.py` |
| 日志脱敏 | WVP 密码仅传入 httpx 请求体，不记录到日志；`diagnose=False` 避免异常堆栈泄露变量值 | `wvp_client.py` / `logger.py` |
| allow_credentials | 设为 `False`，避免 CORS 凭证泄露 | `main.py` |
| HTTP 超时 | 所有 httpx 客户端设置 10s 超时，防止慢速攻击 | 全局 |

### 7.4 高可用设计

| 机制 | 实现 | 说明 |
|------|------|------|
| 断流自愈 | tenacity 5 次退避重连 + url_provider 刷新 WVP 流地址 + WVP 同步 30s 兜底重启 | `stream.py` + `wvp_sync.py` |
| 心跳检测 | AI 每 30s 上报心跳，后端每 30s 扫描，超 90s（3 周期）标记离线 | `pipeline.py` + `heartbeat.py` |
| 降级方案 | Chronos-2 加载/推理失败 -> 线性趋势外推；WVP 未启用 -> 手动注册设备 | `chronos_model.py` + `wvp_client.py` |
| 告警去重 | 实时告警 5 分钟 NX 去重；异常告警 60s 冷却去重；离线告警 5 分钟去重 | `alerts.py` + `heartbeat.py` |
| 调度器容错 | 每个调度器独立 try/except，单个失败不影响其他 | `main.py` lifespan |
| AI 转发容错 | 启流/删除设备时转发 AI 失败仅告警不阻塞配置落库 | `devices.py` `_forward_to_ai()` |
| WS 推送容错 | 推送失败仅记日志不影响告警/预测流程 | `alerts.py` / `scheduler.py` |
| 负值钳位 | 当前态计数不允许为负，异常事件/重启漂移自动修正 | `realtime.py` `_clamp_negatives()` |
| 轨迹清理 | 已计数轨迹 300s TTL 淘汰，防内存泄漏 + 流重连 ID 重用漏计 | `counter.py` `_cleanup_old_tracks()` |
| Redis 持久化 | `appendonly yes` 开启 AOF，容器重启不丢实时状态 | `docker-compose.yml` |

---

## 八、开发与测试计划

### 8.1 开发环境配置

**Docker Compose 启动：**

```bash
# 构建镜像 (CPU)
docker build -t smart-city-platform:latest .

# 启动全部服务 (AI + 后端 + Redis + MediaMTX + 测试推流)
docker compose up -d

# 启用 GPU (取消 docker-compose.yml ai 服务 deploy 注释)
docker compose up -d
```

**热加载：** `docker-compose.yml` 通过 volume 挂载 `./app:/app/app:cached`，修改 Python 代码后：
- 后端：uvicorn `--reload` 自动重启（开发时手动添加）
- AI 服务：需重启容器（`docker compose restart ai`）

**Mock WVP 服务器（`mock_wvp.py`）：**

```bash
# 启动 Mock WVP (模拟设备/通道/点播, 挂载测试视频)
python3 mock_wvp.py
# API: http://localhost:18080
# 模拟设备: 北门车辆摄像头 / 南门人流摄像头
# 点播返回: 本地测试视频文件路径
```

| Mock 端点 | 模拟 WVP API | 返回 |
|-----------|-------------|------|
| `POST /api/login` | 登录 | `{"data": {"accessToken": "mock-token-12345"}}` |
| `GET /api/device/query/devices` | 设备列表 | 2 台模拟设备 (北门车辆/南门人流) |
| `GET /api/device/query/devices/{id}/channels` | 通道列表 | 各 1 个通道 |
| `GET /api/play/start/{dev}/{ch}` | 点播 | FLV 指向本地测试视频 |
| `GET /api/play/stop/{dev}/{ch}` | 停播 | 成功 |

**环境变量配置（`.env`）：**

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `REDIS_URL` | `redis://redis:6379/0` | Redis 连接地址 |
| `WVP_ENABLED` | `false` | WVP 同步总开关 |
| `WVP_API_URL` | `http://wvp:18080` | WVP REST API 地址 |
| `WVP_PLAY_PROTOCOL` | `flv` | AI 拉流协议 |
| `WVP_STREAM_SUB` | `true` | 拉子码流降推理压力 |
| `PREDICTION_INTERVAL_MINUTES` | `15` | 预测间隔（分钟） |
| `CORS_ORIGINS` | `*` | CORS 允许来源 |

### 8.2 单元测试覆盖

**`tests/test_wvp_client.py`（10 个用例，mock httpx，不依赖真实 WVP/Redis/网络）：**

| # | 测试函数 | 覆盖场景 |
|---|---------|---------|
| 1 | `test_disabled_returns_empty` | wvp_enabled=False 时所有方法返回空值 |
| 2 | `test_login_caches_token` | 登录获取 token，兼容 accessToken/access-token 字段名 |
| 3 | `test_list_devices_pagination` | 分页拉取（total=150，每页 100，应拉 2 页 150 条） |
| 4 | `test_start_play_new_format` | 新版 WVP: data.flv 直接含地址 |
| 5 | `test_start_play_old_format` | 旧版 WVP: data.stream.flv 嵌套结构 |
| 6 | `test_start_play_failure_returns_none` | WVP 点播报错时返回 None（不抛异常） |
| 7 | `test_401_triggers_relogin` | token 失效(401): 自动重登一次后重试成功 |
| 8 | `test_select_stream_url_protocol` | 按 wvp_play_protocol 选地址，缺失时回退 |
| 9 | `test_sync_once_disabled` | wvp_enabled=False: sync_once 直接返回 skipped |
| 10 | `test_infer_camera_type` | 设备名启发式推断 camera_type |

**`tests/test_anomaly.py`（9 个用例，合成帧验证，不依赖 Redis/模型/真实视频）：**

| # | 测试函数 | 覆盖场景 |
|---|---------|---------|
| 1 | `test_black_screen_detected` | 全黑帧检测为黑屏 |
| 2 | `test_flower_screen_detected` | 随机噪声帧检测为花屏（含无前帧/有前帧两种） |
| 3 | `test_normal_frame_not_flagged` | 正常渐变帧不误报，通道相关性应高 |
| 4 | `test_dark_but_not_black_not_flagged` | 低光但非纯黑（均值 25 > 阈值 20）不判黑屏 |
| 5 | `test_temporal_none_on_first_frame` | 第一帧无前帧 -> temporal_diff 为 None |
| 6 | `test_monitor_debounce_and_onset` | 连续确认去抖 + onset 事件产出 |
| 7 | `test_monitor_recovery` | 恢复正常 -> recovery 事件 |
| 8 | `test_monitor_sampling_interval` | 周期采样（check_interval=3，前两帧不采样） |
| 9 | `test_monitor_normal_segment_no_events` | 连续正常帧无事件产出 |

**运行方式：**
```bash
python -m pytest tests/test_wvp_client.py -v
python -m pytest tests/test_anomaly.py -v
# 或直接运行 (无需 pytest):
python tests/test_wvp_client.py
python tests/test_anomaly.py
```

### 8.3 集成测试

**端到端流程（WVP 同步 -> 截帧 -> 画线 -> 启流 -> 计数）：**

```
1. 启动 Mock WVP (mock_wvp.py) + Redis + 后端 + AI 服务
   环境变量: WVP_ENABLED=true, WVP_API_URL=http://host:18080

2. 等待 WVP 同步 (30s)
   -> 验证: GET /api/devices 返回 2 台 synced 状态设备

3. 截帧
   -> GET /api/devices/GB-xxx/snapshot
   -> 验证: 返回 JPEG 二进制 (Content-Type: image/jpeg)

4. 配置计数线并启流
   -> POST /api/devices/GB-xxx/enable
   -> {line_coords: "0.5,0.1,0.5,0.9", anchor_coords: "0.9,0.5"}
   -> 验证: 返回 status=online + stream_url

5. 等待 AI 计数 (10-30s)
   -> 验证: GET /api/stats/realtime 返回非零计数
   -> 验证: GET /api/alerts 可见告警 (如触发)
   -> 验证: WS /ws 收到 stats 消息
```

### 8.4 冒烟测试

**`scripts/smoke_test.sh`：** 独立 Docker 容器验证后端核心 API，不依赖 AI 服务和 WVP。

```bash
bash scripts/smoke_test.sh
```

| 步骤 | 验证内容 | 预期结果 |
|------|---------|---------|
| 1 | 创建独立网络 + 启动 Redis | 容器正常运行 |
| 2 | 启动后端 (Redis 模式, 端口 18000) | 容器正常运行 |
| 3 | 等待 12s 后端启动 | - |
| 4 | `GET /health` | `{"status":"ok"}` HTTP 200 |
| 5 | `GET /api/stats/realtime` | 统计 JSON HTTP 200 |
| 6 | `GET /api/alerts` | `[]` HTTP 200 |
| 7 | `POST /api/devices` (注册测试设备) | `{"status":"registered"}` HTTP 201 |
| 8 | `GET /api/devices` | 含测试设备 HTTP 200 |
| 9 | 查看后端日志 (最后 15 行) | 无异常错误 |
| 10 | 清理容器和网络 | - |

**其他测试脚本：**

| 脚本 | 用途 |
|------|------|
| `scripts/stream_test.sh` | RTSP 流拉取测试 |
| `scripts/anomaly_test.sh` | 视频异常检测测试 |
| `scripts/offline_test.sh` | 离线视频处理测试 |
| `scripts/cut_video.sh` | 视频裁剪工具 |
| `scripts/gen_anomaly_video.py` | 生成异常测试视频（黑屏/花屏） |

### 8.5 性能测试计划

| 测试项 | 方法 | 目标 |
|--------|------|------|
| 单路推理延迟 | 计时 `tracker.track(frame)` 单次调用 | < 100ms (CPU) / < 30ms (GPU) |
| 并发路数 | 逐步增加 AI 注册设备数，监控帧率下降 | CPU 4-8 路 / GPU 16-32 路维持 > 10fps |
| Redis 吞吐 | 模拟高频事件推送（1000 事件/秒），监控 HINCRBY 延迟 | < 1ms |
| WS 推送 | 多客户端同时连接，监控消息延迟和丢失率 | 100 客户端 < 100ms 延迟 |
| 告警去重 | 短时间大量相同事件，验证 5 分钟去重有效 | 同规则 5 分钟内只触发 1 次 |
| 预测延迟 | 计时 `predict_total_persons()` 全流程 | < 60s (含 Chronos 推理) |
| 断流恢复 | 手动断开视频流，计时恢复时间 | < 120s (5 次重连 + url 刷新) |
| 内存稳定性 | 7×24h 持续运行，监控容器内存 | 无持续增长（轨迹 TTL 淘汰生效） |

### 8.6 部署检查清单

| # | 检查项 | 命令/方法 | 通过标准 |
|---|--------|---------|---------|
| 1 | 镜像构建成功 | `docker build -t smart-city-platform:latest .` | 无报错，YOLO 权重可加载 |
| 2 | 后端健康检查 | `curl http://localhost:8000/health` | `{"status":"ok"}` |
| 3 | AI 服务健康检查 | `curl http://localhost:8001/health` | `{"status":"ok"}` |
| 4 | Redis 连通性 | 后端日志无 Redis 连接错误 | 日志无 ERROR |
| 5 | 预测调度器启动 | 后端日志 "预测调度器已启动" | 日志可见 |
| 6 | 心跳检测启动 | 后端日志 "摄像头离线检测已启动" | 日志可见 |
| 7 | 警力调度器启动 | 后端日志 "警力分配调度器已启动" | 日志可见 |
| 8 | WVP 同步状态 | `WVP_ENABLED=true` 时日志 "WVP 设备同步已启动" | 日志可见 |
| 9 | CORS 配置 | 生产环境 `CORS_ORIGINS` 非通配符 | 限定前端域名 |
| 10 | WVP 凭证 | `WVP_API_URL` 指向真实 WVP 地址 | 非 localhost/wvp 容器名 |
| 11 | 实时统计可用 | `GET /api/stats/realtime` 返回有效 JSON | HTTP 200 |
| 12 | WebSocket 可连接 | `ws://localhost:8000/ws` 连接成功 | 收到 stats 消息 |
| 13 | 预测降级状态 | `GET /api/prediction/health` 检查 degraded | 部署时确认模型已加载 |
| 14 | 告警规则加载 | 修改 `rules.yaml` 后触发告警 | 热重载生效 |
| 15 | 日志轮转 | loguru 100MB 轮转 + 30 天保留 | 磁盘不溢 |
| 16 | Redis AOF | `docker-compose.yml` appendonly=yes | 持久化开启 |
| 17 | GPU 可用性 (如启用) | AI 日志 "device=cuda" | GPU 推理模式 |
| 18 | 冒烟测试通过 | `bash scripts/smoke_test.sh` | 全部 HTTP 200/201 |