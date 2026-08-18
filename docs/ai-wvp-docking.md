# WVP/ZLM 与 AI 服务对接文档

> **前提**：WVP-GB28181 + ZLMediaKit 已外部部署并配置完成。本文档只覆盖**本项目（DT）与 WVP/ZLM 的对接逻辑**及 **AI 侧 H.264 码流处理与推理计数**，不涉及 WVP/ZLM 自身的部署配置。
>
> **本项目角色**：消费方。后端调 WVP REST API 取流地址，AI 拉流推理计数。不参与 SIP 信令、RTP 收包、流媒体转封装。

---

## 一、本项目需要配置的参数（`.env`）

这是本项目唯一需要填的部分，全部是对接外部 WVP 的连接信息：

```bash
# 对接外部 WVP（⚠️ 唯一需要你填的部分）
WVP_ENABLED=true
WVP_API_URL=http://<WVP管理地址>:<WVP管理端口>     # 例: http://10.0.0.5:18080
WVP_USERNAME=<WVP后台账号>                          # 例: admin
WVP_PASSWORD=<WVP后台密码>                          # 例: admin
WVP_SYNC_INTERVAL=30                                # 设备同步间隔(秒)
WVP_PLAY_PROTOCOL=flv                               # AI 拉流协议 flv/rtsp
WVP_STREAM_SUB=true                                 # 拉子码流降推理压力

# 本项目内部（默认即可）
REDIS_URL=redis://redis:6379/0
BACKEND_URL=http://backend:8000
AI_SERVICE_URL=http://ai:8001
YOLO_MODEL=models/yolo11n.pt
YOLO_CONF=0.4
YOLO_IOU=0.5
```

本项目自身端口：backend `8000` / AI `8001` / Redis 宿主映射 `16379`。

> `WVP_API_URL` / `WVP_USERNAME` / `WVP_PASSWORD` 三项向 WVP 部署方索取。其余有默认值。

---

## 二、端口来源机制（流地址的端口从哪来）

AI 拉的 FLV 地址里的端口**不是写死 80**，而是来自 ZLM 的 HTTP 端口配置，WVP 拼地址时取同一个值：

```
ZLM config.ini  [http] port=N      ◀──┐
                                     │ 两边必须一致
WVP application.yml media.http-port=N◀─┘
                    │
                    ▼
WVP play/start 返回:  http://<stream-ip>:<http-port>/flv?app=live&stream=xxx
                                       ↑
                                  这个端口 = 上面配的 N
                    │
                    ▼
AI 直接拉这个完整地址 (本项目不写死端口)
```

- `<stream-ip>` = WVP `media.stream-ip`（ZLM 对外 IP）
- `<http-port>` = ZLM `[http]port` = WVP `media.http-port`（两边一致）
- `stream=xxx` = `gb_device_id` + `_` + `gb_channel_id`

**本项目无需知道这个端口具体是多少**——`play/start` 返回的地址自带正确端口，AI 直接拉返回的完整地址。

**若 WVP/ZLM 侧 80 被占用**：由 WVP/ZLM 部署方改 ZLM `[http]port` + WVP `media.http-port` + docker 映射三处为同一空闲端口。改完 `play/start` 返回的地址自带新端口，**本项目零改动**。

> ⚠️ WVP/ZLM 侧常见踩坑：只改 docker 映射（`8088:80`）不改配置——WVP 仍返回 `:80`，AI 拉 `:80` 失败。必须三处一起改。

---

## 三、本项目与外部的两个连接点

整个对接只连两个地址：

| # | 连接方向 | 地址 | 凭据/ID | 用途 |
|---|---------|------|---------|------|
| ① | 后端 → WVP（REST API）| `WVP_API_URL` | `WVP_USERNAME`/`PASSWORD` | 同步设备、`play/start` 取流地址、断流刷新 |
| ② | AI → ZLM（HTTP-FLV）| play/start 返回的 `data.flv` | stream 自带 `gb_device_id_gb_channel_id` | 拉 H.264 码流送推理 |

**本项目不碰**：SIP（5060）、RTP（30000-30500）、RTSP（554）、SIP ID/domain、mediaServerId/secret——均为 WVP/ZLM/IPC 内部使用。

---

## 四、play/start 自动返回机制

### 4.1 它是什么

WVP 的 REST 接口 `GET /api/play/start/{deviceId}/{channelId}`。调用后 WVP 内部自动完成 SIP 点播 → IPC 推 RTP → ZLM 转 HTTP-FLV，然后返回流地址：

```json
{
  "code": 0,
  "data": {
    "flv": "http://<stream-ip>:<http-port>/flv?app=live&stream=<gb_device_id>_<gb_channel_id>",
    "rtsp": "rtsp://...",
    "streamId": "<gb_device_id>_<gb_channel_id>"
  }
}
```

`data.flv` 是**完整可拉流地址**，IP/端口/stream 全自带，本项目不用拼。

### 4.2 本项目封装

- [wvp_client.py:123](file:///Users/bianwei/Desktop/codes/DT/app/backend/core/wvp_client.py#L123) `start_play()` 封装调用，兼容新旧返回结构，返回 `{flv, rtsp, stream_id}`
- [wvp_client.py:167](file:///Users/bianwei/Desktop/codes/DT/app/backend/core/wvp_client.py#L167) `select_stream_url()` 按 `WVP_PLAY_PROTOCOL` 取 flv 或 rtsp

### 4.3 "自动"两层含义

1. **地址 WVP 算好返回**：不用本项目拼 host/port/stream
2. **调用由代码自动触发**：设备同步/启流时、断流刷新时自动调，不用人手动调

---

## 五、AI 拉流链路详解

### 5.1 拉流函数

[app/ai/stream.py:35](file:///Users/bianwei/Desktop/codes/DT/app/ai/stream.py#L35) `stream_frames(url, stop_event, url_provider)`：

- `cv2.VideoCapture(url)` 拉流（OpenCV 内部 FFmpeg 解 FLV 容器 → 解 H.264 → BGR 帧）
- cv2 同步读帧用 `asyncio.to_thread` 包装，**不阻塞事件循环**
- `url_provider`：断流重连失败时回调取新地址（WVP 流刷新用）

### 5.2 管道接入

[app/ai/pipeline.py:72](file:///Users/bianwei/Desktop/codes/DT/app/ai/pipeline.py#L72) `DevicePipeline._run()`：

```python
url_provider = self._refresh_stream_url if self.enable_url_refresh else None
async for frame, _idx in stream_frames(self.stream_url, self._stop, url_provider=url_provider):
    # 1. 首帧/重连后更新计数器帧尺寸
    # 2. 视频异常检测 (黑屏/花屏)
    # 3. YOLO11 + BoT-SORT 跟踪 (asyncio.to_thread 不阻塞)
    # 4. 越线计数 (ROI 过滤 + 双向计数 + 防抖链)
    # 5. 事件 POST /api/events + WebSocket 推送
    # 6. 每 30s 心跳 POST /api/devices/{id}/heartbeat
```

### 5.3 流地址刷新回调

[app/ai/pipeline.py:121](file:///Users/bianwei/Desktop/codes/DT/app/ai/pipeline.py#L121) `_refresh_stream_url()`：

```python
resp = await self._client.get(f"{settings.backend_url}/api/devices/{self.device_id}/stream")
return resp.json().get("stream_url", "")
```

调后端 `GET /api/devices/{id}/stream`，后端转调 WVP `play/start` 返回新 FLV 地址。

### 5.4 启用条件

[app/ai/service.py:86](file:///Users/bianwei/Desktop/codes/DT/app/ai/service.py#L86) `enable_url_refresh=bool(dev.gb_device_id)`：

- WVP 同步设备（有 `gb_device_id`）→ 自动启用流刷新
- 手填 `stream_url` 注册 → `gb_device_id` 为空 → 不启用（`url_provider=None`，同离线处理）

---

## 六、断流重连机制（两级）

[app/ai/stream.py:58-91](file:///Users/bianwei/Desktop/codes/DT/app/ai/stream.py#L58-L91)：

```
读流失败 (cap.read 返回 False/None)
  │
  ├─ 第1级: 当前 url 重连
  │         _open() 内含 tenacity 5 次指数退避 (2-30s)
  │         成功 → 继续 / 失败 ↓
  │
  ├─ 第2级: url_provider 刷新地址 (仅 WVP 同步设备)
  │         10s 冷却 防频繁打 WVP
  │         调后端 /api/devices/{id}/stream → WVP play/start → 新 flv
  │         新地址重连 → 成功继续 / 失败放弃
  │
  └─ 放弃 → 等下轮 wvp_sync 重新启流
```

---

## 七、设备同步流程

[app/backend/core/wvp_sync.py](file:///Users/bianwei/Desktop/codes/DT/app/backend/core/wvp_sync.py) 每 `WVP_SYNC_INTERVAL`（30s）执行 `sync_once()`：

| 场景 | 处理 |
|------|------|
| WVP 新通道 | 入表 `status=synced`（不自动启流，缺计数线）|
| 已配置 + WVP 在线 + AI 未跑 | 重新启流（play/start → 启 AI 管道）|
| WVP 侧离线 | 停 AI 管道，标 `offline` |
| WVP 恢复 | 重新 play/start 启流 |

- 按 `gb_device_id`+`gb_channel_id` 与本地 Redis 设备表比对
- `camera_type` 从通道名启发式推断（含「车」→vehicle、含「人」→person）

---

## 八、设备注册计数参数

WVP 同步设备入表后（`status=synced`），调 `POST /api/devices/{id}/enable` 补计数参数启流：

| 参数 | 必填 | 说明 | 示例 |
|------|------|------|------|
| `line_coords` | ✅ | 计数线 `x1,y1,x2,y2` 归一化 0-1 | `0.1,0.75,0.9,0.75` |
| `anchor_coords` | ❌ | 内侧锚点 `x,y`，定 Enter/Exit 方向 | `0.5,0.9` |
| `camera_type` | ❌ | `vehicle`/`person`/空 | `vehicle` |
| `count_only` | ❌ | `enter`/`exit`/空（单向过滤）| `enter` |
| `roi_coords` | ❌ | ROI 多边形 `x1,y1,...` 至少3顶点 | `0.05,0.6,0.95,0.6,0.95,0.95,0.05,0.95` |

> `gb_device_id`/`gb_channel_id` 由 WVP 同步自动写入，无需手填。

---

## 九、端到端调用时序

### 首次启流

```
POST /api/devices/{id}/enable {line_coords, anchor...}
  → 后端读设备 gb_device_id/gb_channel_id
  → wvp_client.start_play()                   # 调 WVP play/start
  → WVP 返回 data.flv
  → 启动 DevicePipeline(stream_url=flv, enable_url_refresh=True)
  → AI stream_frames(flv, url_provider=_refresh_stream_url)
  → cv2 拉流 → YOLO → 计数 → POST /api/events + WS 推送
```

### 断流自愈

```
AI cap.read() 失败
  → 当前 url 重连 5 次 (退避 2-30s)  仍失败
  → url_provider() = GET 后端 /api/devices/{id}/stream  (10s 冷却)
     → 后端 wvp_client.start_play()                  # 重新点播
     → WVP 返回新 data.flv
  → AI 用新 flv 重连 → 继续推理
```

---

## 十、FAQ

**Q：本项目要配哪些 IP/port/ID？**
只需 `.env` 里 `WVP_API_URL`（WVP 管理地址）、`WVP_USERNAME`/`WVP_PASSWORD`（账号密码），向 WVP 部署方索取。流地址的 IP/port 由 WVP play/start 自动返回，ID 由设备同步自动获取，本项目都不用填。

**Q：play/start "自动返回"是什么意思？**
调 WVP 的 play/start 接口，WVP 完成点播+转流后返回一个完整 FLV 地址（含 host/port/stream）。本项目代码自动调它，不用手拼地址。

**Q：80 端口被占用怎么办？**
由 WVP/ZLM 部署方改 ZLM `[http]port` + WVP `media.http-port` + docker 映射三处为同一端口。本项目零改动。

**Q：本项目要不要配 SIP ID / mediaServerId / secret？**
不要。这些是 WVP↔ZLM↔IPC 内部用的，本项目走 REST + HTTP-FLV 不碰。

**Q：手填模式和 WVP 自动模式区别？**
- `WVP_ENABLED=false`：手动 `POST /api/devices` 填 `stream_url`，AI 不自动刷新地址
- `WVP_ENABLED=true`：后端自动同步设备、自动取地址、断流自动刷新，无需手填流地址

---

## 十一、运维工具：设备计数配置页面

WVP 同步的设备入表后 `status=synced`，但没有计数线配置，AI 不会启流。计数线必须贴合摄像头实际画面，无法自动推断。运维工具页面提供**截帧 -> 画线 -> 启流**的可视化操作流程。

### 11.1 访问

backend 启动后，浏览器打开：

```
http://<backend-host>:8000/static/device-config.html
```

> 页面是单 HTML 文件（[static/device-config.html](file:///Users/bianwei/Desktop/codes/DT/static/device-config.html)），零依赖零构建，由 backend 的 `/static` 静态服务提供。

### 11.2 前置条件

| 条件 | 说明 |
|------|------|
| backend + redis 运行中 | `docker compose -p smartcity up -d backend redis` |
| `WVP_ENABLED=true` | `.env` 中开启，指向已部署的 WVP 地址 |
| WVP 后台有在线设备 | IPC 已注册到 WVP 且状态在线 |

### 11.3 操作流程

```
① 同步设备          ② 截帧              ③ 画线+锚点          ④ 启流计数
   │                   │                    │                   │
   ▼                   ▼                    ▼                   ▼
点「同步设备」     点设备按钮          Canvas 上点 3 下      点「启流计数」
POST /sync          GET /snapshot        画线(2点)+锚点(1点)   POST /enable
   │                   │                    │                   │
   ▼                   ▼                    ▼                   ▼
设备入表 synced     返回一帧 JPEG        自动算归一化坐标      AI 启动拉流推理
```

#### 步骤详解

**第一步：同步设备**

页面顶部输入后端地址（默认 `http://localhost:8000`），点「同步设备」。

- 调 `POST /api/devices/sync`，WVP 平台的在线设备自动入表
- 新设备状态为 `synced`（橙边按钮），`camera_type` 从通道名自动推断
- 点「刷新列表」可随时查看设备状态

**第二步：截帧**

点击 `synced` 状态的设备按钮，页面自动调 `GET /api/devices/{id}/snapshot` 截取一帧画面。

- 后端流程：WVP `play/start` -> cv2 拉一帧 -> JPEG -> `play/stop` 释放资源
- 约需 3-8 秒，状态栏显示进度
- 截帧成功后画面显示在 Canvas 上，分辨率即视频原始分辨率（如 1280x720）

**第三步：画计数线 + 锚点**

在画面上点击 3 次：

| 点击 | 作用 | 颜色 |
|------|------|------|
| 第 1 点 | 计数线端点 A | 绿点 |
| 第 2 点 | 计数线端点 B（与 A 连成绿线） | 绿线 |
| 第 3 点 | 锚点（红色，虚线指向线中点） | 红点 |

- **计数线**：画在车流/人流必经的截面（门口、路口横线）
- **锚点**：点在你想计为 `Enter（进）`的那一侧。锚点所在侧 = 内侧

> 画完 3 个点后，「启流计数」按钮自动启用，底部显示归一化坐标。
> 点「清除画线」可重画。

**第四步：启流计数**

选择类型（全部/只检车/只检人）和方向（双向/只计进/只计出），点「启流计数」。

- 调 `POST /api/devices/{id}/enable`，提交计数线 + 锚点 + 类型 + 方向
- 后端保存配置 -> WVP `play/start` 取流地址 -> 转发 AI 启动管道
- 设备状态变为 `online`（绿边按钮），AI 开始拉流推理计数
- 事件经 `POST /api/events` 回写后端，`GET /api/stats/realtime` 可查看计数

### 11.4 坐标转换（自动）

页面自动处理像素坐标 -> 归一化坐标的转换，用户无需计算：

```
归一化x = 点击像素x / Canvas宽度
归一化y = 点击像素y / Canvas高度

例: 1280x720 画面, 用户在 (200, 400) 点击
   -> 归一化 = (200/1280, 400/720) = (0.156, 0.556)
```

### 11.5 截帧端点 API

```
GET /api/devices/{device_id}/snapshot
```

| 状态码 | 说明 |
|--------|------|
| 200 | 返回 `image/jpeg` 二进制图片 |
| 503 | WVP 未启用 |
| 404 | 设备不存在 |
| 400 | 非 WVP 同步设备 |
| 502 | WVP 点播失败或截帧失败 |
| 504 | 截帧超时（15s，建议重试）|

### 11.6 测试验证

完整流程已通过端到端测试（Mock WVP + 无头浏览器）：

| 测试项 | 结果 |
|--------|------|
| 设备同步（2 台设备入表） | ✅ |
| 截帧（1280x720 JPEG） | ✅ |
| Canvas 画线交互（3 点点击） | ✅ |
| 归一化坐标输出 | ✅ |
| 启流计数（status -> online） | ✅ |
| UI 状态联动（按钮启用/禁用） | ✅ |

> 测试用 Mock WVP 脚本：[mock_wvp.py](file:///Users/bianwei/Desktop/codes/DT/mock_wvp.py)，用本地视频文件模拟摄像头流。
