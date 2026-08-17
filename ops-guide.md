# 运维操作指南

## 一、WVP 设备从拉流到计数的全流程

### 整体架构

```
WVP (GB28181) ──→ 后端定时同步 ──→ Redis 设备表 ──→ 用户配置计数线 ──→ AI 管道 ──→ 事件 ──→ Redis 统计
                      │                                               │
                      └── play/start 拿流地址 ──────────────────────────┘
```

### 阶段一：WVP 定时同步

**入口**：`app/backend/core/wvp_sync.py` → `sync_once()`

每 `wvp_sync_interval` 秒由后台任务执行一次：

| 步骤 | 操作 | 对应代码 |
|------|------|---------|
| 1 | WVP 登录，取 token（遇 401 自动重登） | `wvp_client.py:login()` |
| 2 | 拉取全量在线设备 `GET /api/device/query/devices` | `wvp_client.py:list_devices()` |
| 3 | 每台设备拉取通道列表 `GET /api/device/query/devices/{id}/channels` | `wvp_client.py:list_channels()` |
| 4 | 与本地 Redis 设备表比对（按 `gb_device_id + gb_channel_id` 作为唯一键） | `wvp_sync.py:sync_once()` |

**比对结果处理**：

| 情况 | 操作 |
|------|------|
| WVP 新通道 | 入表 `status=synced`，**不启流**（等待用户画线配置） |
| WVP 已离线 | 停 AI 管道，标 `status=offline` |
| WVP 恢复在线 | `play/start` 拿流地址 → 启 AI 管道 |
| 在线但 AI 侧没跑（WVP/ZLM 重启后） | 重新 `play/start` → 重新启流 |

### 阶段二：用户配置启流

**入口**：`device-config.html` → `POST /api/devices/{id}/enable`

| 步骤 | 说明 |
|------|------|
| 1 | 前端截帧 `GET /api/devices/{id}/snapshot`（非 WVP 设备直接拉 RTSP 截帧） |
| 2 | 在画面上点击红线两端 + 锚点，形成计数线 |
| 3 | 选择类型（车辆/人流）和方向（单向/双向） |
| 4 | 提交 → `POST /api/devices/{id}/enable` 携带 `{line_coords, anchor_coords, camera_type, count_only}` |
| 5 | 后端保存配置到 Redis → 获取流地址 → 转发到 AI 服务 |

**流地址获取选择逻辑** (`devices.py:enable_device()`):

```python
is_wvp = bool(settings.wvp_enabled and gb_dev and gb_ch)
```

| 条件 | 流地址来源 | 适用场景 |
|------|-----------|---------|
| `WVP_ENABLED=true` + 有 `gb_device_id` + 有 `gb_channel_id` | WVP `play/start` 接口 | 真实 WVP 同步的摄像头 |
| 以上任一不满足 | 直接使用设备注册时填的 `stream_url` | Mock 设备、手动注册设备 |

**向 AI 服务发送的请求体**：

```json
{
  "device_id": "mock-cam-vehicle",
  "stream_url": "rtsp://rtsp-server:8554/vehicle",
  "line": [[0.3,0.5],[0.7,0.5]],
  "anchor": [0.5,0.6],
  "count_only": "enter",
  "camera_type": "vehicle",
  "roi": [[0.1,0.1],[0.9,0.1],[0.9,0.9],[0.1,0.9]],
  "gb_device_id": "xxx",
  "gb_channel_id": "yyy"
}
```

### 阶段三：AI 管道处理循环

**入口**：`pipeline.py:DevicePipeline._run()`

```
stream_frames() ──→ ByteTracker.track() ──→ LineCrossingCounter.process_tracks() ──→ 事件推送
      │                                      │
      │                                      └── 每帧：检测跟踪 → 越线判定 → 去重 → 事件
      │
      └── 断流自动重连（5 次退避），WVP 设备回调后端刷新流地址
```

| 环节 | 组件 | 说明 |
|------|------|------|
| **拉流** | `stream.py:stream_frames()` | OpenCV 异步拉帧，支持 RTSP/FLV，自动重连 |
| **检测跟踪** | `tracker.py:ByteTracker.track()` | BoT-SORT 算法，保持 track_id 稳定 |
| **越线计数** | `counter.py:LineCrossingCounter.process_tracks()` | 内侧锚点法判定越线方向，含防抖去重 |
| **事件推送** | `pipeline.py:_run()` 循环内 | `POST /api/events` 到后端 + WebSocket 广播 |
| **心跳** | 每 30 秒 | `POST /api/devices/{id}/heartbeat` |

### 阶段四：事件落库

**入口**：`realtime.py:apply_event()`

```
越线事件 → Redis 管道：
  1. hincrby 全局当前计数（current_vehicles/persons）
  2. hincrby 逐设备当前计数（device:{id}）
  3. hincrby 全局今日累计（today_vehicle_in/out/person_in/out）
  4. hincrby 逐设备今日累计（device:{id}:daily:{yyyymmdd}）
  5. hincrby N 分钟区间计数（供 Chronos 预测）
  6. lpush 事件历史列表（保留 2000 条）
```

### 阶段五：数据消费

| 方式 | 接口 | 频率 |
|------|------|------|
| REST API | `GET /api/stats/realtime` | 按需 |
| REST API | `GET /api/stats/devices` | 按需 |
| REST API | `GET /api/events?limit=N` | 按需 |
| WebSocket | `ws://backend:8000/ws` | 每 2 秒推送全局 + 逐设备统计 |
| AI WebSocket | `ws://ai:8001/ws` | 实时推送 tracking 数据和越线事件 |

---

## 二、Mock 模式启动

```bash
# 构建并启动（首次或镜像有变更）
bash scripts/mock_start.sh --build

# 快速启动（已有镜像，代码挂载 volume 无需 rebuild）
bash scripts/mock_start.sh

# 停止所有服务
bash scripts/mock_start.sh --down
```

### 服务地址

| 服务 | 地址 |
|------|------|
| 后端 API | `http://localhost:8000` |
| 健康检查 | `http://localhost:8000/health` |
| 设备列表 | `http://localhost:8000/api/devices` |
| 实时统计 | `http://localhost:8000/api/stats/realtime` |
| 设备统计 | `http://localhost:8000/api/stats/devices` |
| 事件列表 | `http://localhost:8000/api/events` |
| 告警列表 | `http://localhost:8000/api/alerts` |
| 警力分配 | `http://localhost:8000/api/police` |
| 运维工具 | `http://localhost:8000/static/device-config.html` |
| WebSocket | `ws://localhost:8000/ws` |
| AI 服务 | `http://localhost:8001` |
| RTSP 流 | `rtsp://localhost:8554/vehicle` |
| RTSP 流 | `rtsp://localhost:8554/person` |

### 全接口测试

```bash
# 默认 localhost
bash scripts/interface_test.sh

# 指定 IP
bash scripts/interface_test.sh 192.168.1.41
```

### 日志查看

```bash
docker compose logs -f backend
docker compose logs -f ai
docker compose logs -f rtsp-streamer-vehicle
docker compose logs -f rtsp-streamer-person
```

### 快速重启（代码变更后）

```bash
docker compose restart backend
# 或同时重启 AI
docker compose restart backend ai
```

---

## 三、ROI（感兴趣区域）

### 作用

ROI 是一个多边形区域，限制只有**中心点落在该多边形内**的轨迹才参与越线计数。ROI 外的物体即使穿过计数线也不会触发事件。

适用于：
- 排除画面边缘干扰（如路边行人、远处车辆）
- 聚焦特定车道/出入口
- 降低无效检测的算力消耗

### 格式

归一化坐标字符串 `"x1,y1,x2,y2,x3,y3,..."`，坐标值范围 0~1，至少 3 个顶点（6 个值）。

示例——矩形 ROI（覆盖画面 10%~90%）：

```
"0.1,0.1,0.9,0.1,0.9,0.9,0.1,0.9"
```

### ROI 与计数线的交互

当 ROI 启用时，计数器会将**计数线裁剪到 ROI 多边形内**，只保留 ROI 内的线段部分作为有效计数段。跨线点投影落在 ROI 外的线段上不触发计数。

```
         ┌───────────────┐
         │   ROI 区域     │
         │   ──●────●──  │  ← 有效计数线段（裁剪后）
         │       ↑锚点    │
         └───────────────┘
         ←── 无效段（不计数）
```

### 设置方式

ROI 可通过 API 设置，device-config.html 前端暂未提供 ROI 绘制功能：

```bash
# 注册设备时设置
curl -X POST http://localhost:8000/api/devices \
  -H "Content-Type: application/json" \
  -d '{
    "id": "cam-01", "name": "出入口", "stream_url": "rtsp://...",
    "roi_coords": "0.1,0.1,0.9,0.1,0.9,0.9,0.1,0.9"
  }'

# 启流时设置
curl -X POST http://localhost:8000/api/devices/cam-01/enable \
  -H "Content-Type: application/json" \
  -d '{
    "line_coords": "0.3,0.5,0.7,0.5",
    "anchor_coords": "0.5,0.6",
    "roi_coords": "0.1,0.1,0.9,0.1,0.9,0.9,0.1,0.9"
  }'

# 清除 ROI（全画面计数）
curl -X POST http://localhost:8000/api/devices/cam-01/enable \
  -H "Content-Type: application/json" \
  -d '{"line_coords": "0.3,0.5,0.7,0.5", "anchor_coords": "0.5,0.6"}'
```