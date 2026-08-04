# WVP-GB28181 设备接入与自动同步方案（方案及实现）

> 状态：**已实现完成** · 更新日期：2026-08-03
> 覆盖范围：IPC → WVP+ZLM → HTTP-FLV → AI 推理 全链路打通，含 P2 三大功能 —— 设备自动同步、webhook 预留、流地址自动刷新。

---

## 一、背景

当前系统 [stream.py](file:///Users/bianwei/Desktop/codes/DT/app/ai/stream.py) 的 `cv2.VideoCapture` 已能直接拉 HTTP-FLV（OpenCV+FFmpeg 通用），但缺失 IPC↔WVP↔ZLMediaKit 这一段的集成：

- 没有 WVP 对接代码，设备靠手填 `stream_url` 注册（[devices.py](file:///Users/bianwei/Desktop/codes/DT/app/backend/api/devices.py)）
- 没有设备自动同步、webhook、流地址自动刷新
- docker-compose 只有 ai/backend/redis，无 wvp/zlm

本方案打通「IPC → WVP+ZLM → HTTP-FLV → AI 推理」全链路，并实现 P2 —— WVP 设备自动同步、webhook 预留、流地址自动刷新。

### 关键技术事实

- **WVP 无标准对外 HTTP webhook**：WVP 的实时事件主要靠 WebSocket 推给它自己的前端，对外无通用 outbound HTTP 回调。因此采用「**定时轮询为主 + webhook 端点预留**」策略。
- **WVP-Pro 依赖 MySQL**：WVP 自身用 MySQL 存设备/通道/录像。本项目「不用 MySQL 存事件」的约束不受影响（MySQL 只服务于 WVP 容器）。
- **WVP `play/start` 返回结构跨版本不一致**：旧版 `data.stream.flv`，新版 wvp-pro `data.flv`。客户端需兼容两种。

### 架构决策

1. **后端统一对接 WVP**：WVP 客户端只放后端。AI 断流时调后端 `GET /api/devices/{id}/stream`，后端调 WVP `play/start` 拿新地址返回。AI 服务保持纯粹，不持有 WVP token。
2. **本项目 compose 一并部署 wvp+zlm**：生成 `configs/wvp/application.yml`、`configs/zlm/config.ini`。

---

## 二、总体架构

```
IPC 摄像头 (GB28181/H.264)
   │ ① SIP REGISTER 注册
   ▼
┌──────────────────────────────────┐
│  WVP-GB28181-pro (18080/5060)     │  信令平台：SIP 注册/INVITE 点播
│  + ZLMediaKit (80/30000-30500)    │  流媒体：收 RTP→重组 H.264→转 HTTP-FLV
│  + MySQL (WVP 专用)               │
└──────────▲───────────────────────┘
           │ ② REST API (设备查询/play/start)
           │
┌──────────┴───────────────────────┐
│  业务后端 (8000) ← WVP 唯一对接点  │  wvp_sync 定时同步 + 流地址刷新代理
│  · wvp_client.py  (REST 客户端)   │
│  · wvp_sync.py    (30s 轮询同步)  │
│  · /api/devices/{id}/stream       │
└──────────▲───────────────────────┘
           │ ③ GET /api/devices/{id}/stream (断流刷新)
           │
┌──────────┴───────────────────────┐
│  AI 分析服务 (8001)               │  stream_frames(url_provider=...) 拉流
│  YOLO11 + BoT-SORT + 越线计数     │  断流重连失败 → 回调后端刷新 FLV 地址
└──────────────────────────────────┘
```

### 调用链

- **正常启流**：WVP 设备上线 → 后端 `wvp_sync` 轮询发现 → 后端 `play/start` 拿 flv → 转发 `POST /devices` 到 AI（stream_url=flv）→ AI 拉流推理
- **断流刷新**：AI `cap.read` 连续失败 → 先重开同一 url（5 次退避）→ 仍失败调后端 `GET /api/devices/{id}/stream` → 后端 `play/start` 拿新 flv → 返回 AI → AI 用新地址重连（10s 冷却防频繁打 WVP）
- **设备下线**：WVP 设备离线 → `wvp_sync` 发现 → 转发 `DELETE /devices/{id}` 到 AI 停 pipeline → Redis 标记 offline
- **设备恢复**：WVP 重新在线 → `wvp_sync` 发现 offline 设备 → 重新 `play/start` 启流 → 标 online

---

## 三、功能模块（方案 + 实现）

### 模块 1：WVP 设备自动同步

**方案**：后台任务每 30s（`wvp_sync_interval`）拉取 WVP 设备/通道，与本地 Redis 设备表按 `gb_device_id + gb_channel_id` 比对，自动处理新增、启停、离线、恢复。

**实现**：[app/backend/core/wvp_sync.py](file:///Users/bianwei/Desktop/codes/DT/app/backend/core/wvp_sync.py)

`sync_once()`（[wvp_sync.py:96](file:///Users/bianwei/Desktop/codes/DT/app/backend/core/wvp_sync.py#L96)）核心逻辑：

| 场景 | 处理 |
|------|------|
| WVP 新通道（本地无） | 入表 `status=synced`（不自动启流，缺计数线，等 `POST /enable` 配置） |
| 已配置 + WVP 在线 + AI 侧未跑 | 重新启流（覆盖 WVP/ZLM 重启场景） |
| WVP 侧离线（本地 online/synced） | 停 AI pipeline，标 `offline` |
| WVP 恢复（本地 offline → 在线且已配线） | 重新 `play/start` 启流 |

- `camera_type` 从通道名启发式推断（[wvp_sync.py:27](file:///Users/bianwei/Desktop/codes/DT/app/backend/core/wvp_sync.py#L27)）：含「车」→vehicle、含「人」→person，否则 None
- 启停复用 [devices.py](file:///Users/bianwei/Desktop/codes/DT/app/backend/api/devices.py) 的 `_forward_to_ai()` 与坐标解析工具函数
- 一次性拉取 AI 在跑设备集合（`_get_ai_running_devices`），避免逐设备查询
- 后台任务仿 [heartbeat.py](file:///Users/bianwei/Desktop/codes/DT/app/backend/core/heartbeat.py) 模式（模块级 `_task`、`_loop()`、`start_syncer()`/`stop_syncer()`）
- 在 [main.py](file:///Users/bianwei/Desktop/codes/DT/app/backend/main.py#L48-L65) lifespan 启停（try/except 容错 + started 标志）

### 模块 2：流地址自动刷新

**方案**：AI 断流重连失败时，回调后端 API 获取新的流地址，后端转调 WVP `play/start`。AI 服务不直接持有 WVP token。

**实现链路**：

1. **流拉取层** [app/ai/stream.py](file:///Users/bianwei/Desktop/codes/DT/app/ai/stream.py#L35-L95)：`stream_frames()` 签名增加 `url_provider: Optional[Callable[[], Awaitable[str]]] = None`
   - 断流时先用原 URL 重连（tenacity 5 次指数退避）
   - 仍失败且提供了 `url_provider` → 调用其获取新地址再重连
   - **10s 冷却**（`refresh_cooldown`）防频繁打 WVP
   - `url_provider=None` 时行为不变（离线 `video_processor` 兼容）

2. **Pipeline 层** [app/ai/pipeline.py](file:///Users/bianwei/Desktop/codes/DT/app/ai/pipeline.py#L121-L137)：`_refresh_stream_url()` 调后端 `GET /api/devices/{id}/stream` 取新 flv 地址
   - `enable_url_refresh` 标志控制是否启用（[pipeline.py:65](file:///Users/bianwei/Desktop/codes/DT/app/ai/pipeline.py#L65)）

3. **服务层** [app/ai/service.py](file:///Users/bianwei/Desktop/codes/DT/app/ai/service.py#L86)：`enable_url_refresh=bool(dev.gb_device_id)` —— 仅 WVP 同步设备（有国标 ID）启用

4. **后端代理** [devices.py](file:///Users/bianwei/Desktop/codes/DT/app/backend/api/devices.py#L234-L258)：`GET /api/devices/{id}/stream` 转调 WVP `play/start` 返回新地址

### 模块 3：WVP Webhook（预留）

**方案**：WVP 默认无对外 HTTP webhook，主力靠轮询。预留 webhook 端点供定制对接（如在 WVP 侧配置事件转发）。

**实现**：[devices.py:220](file:///Users/bianwei/Desktop/codes/DT/app/backend/api/devices.py#L220-L231) `POST /api/devices/wvp-webhook`
- 接收任意 JSON body，透传记录日志
- 触发一次 `sync_once()`
- WVP 未启用时返回 `{"status":"skipped","reason":"wvp_disabled"}`

---

## 四、WVP REST 客户端

**实现**：[app/backend/core/wvp_client.py](file:///Users/bianwei/Desktop/codes/DT/app/backend/core/wvp_client.py)

`WVPClient` 类（模块级单例 `get_wvp_client()`）：

| 方法 | WVP 接口 | 说明 |
|------|---------|------|
| `_login()` | `POST /api/login` | 取 `access-token` 缓存内存；遇 401 自动重登 |
| `list_devices()` | `GET /api/device/query/devices` | 分页拉全量设备，返回 `[{deviceId, name, online}]` |
| `list_channels(dev)` | `GET /api/device/query/{dev}/channels` | 分页拉通道 |
| `start_play(dev, ch)` | `GET /api/play/start/{dev}/{ch}` | 兼容解析 `data.flv`/`data.stream.flv`/`data.rtsp`，按 `wvp_play_protocol` 取地址；子码流时带 `streamType=sub` |
| `stop_play(dev, ch)` | `GET /api/play/stop/{dev}/{ch}` | 停止点播 |
| `select_stream_url()` | — | 按 `wvp_play_protocol` 从点播结果取地址（flv 优先回退 rtsp） |

- `wvp_enabled=False` 时所有方法返回空值，调用方无需额外判空
- 跨版本返回结构兼容（[wvp_client.py:140-153](file:///Users/bianwei/Desktop/codes/DT/app/backend/core/wvp_client.py#L140-L153)）：新版 `data.flv` 与旧版 `data.stream.flv` 两种

---

## 五、REST API 端点

均在 [app/backend/api/devices.py](file:///Users/bianwei/Desktop/codes/DT/app/backend/api/devices.py)，挂载于 `/api/devices`：

| 方法 | 路径 | 说明 | 错误码 |
|------|------|------|--------|
| `POST` | `/api/devices/sync` | 手动触发一次 WVP 同步，返回 `{added, started, stopped, recovered}` | `503` WVP 未启用 |
| `GET` | `/api/devices/{id}/stream` | 刷新流地址（AI 断流重连时调），返回 `{stream_url, stream_id}` | `503`/`404`/`400`/`502` |
| `POST` | `/api/devices/{id}/enable` | 为同步设备配计数线并启流（synced→online） | `503`/`404`/`400`/`502` |
| `POST` | `/api/devices/wvp-webhook` | WVP 定制回调入口（预留），透传后触发同步 | — |

`DeviceIn`/`DeviceOut` 模型新增 `gb_device_id`、`gb_channel_id` 字段（[devices.py:49-50](file:///Users/bianwei/Desktop/codes/DT/app/backend/api/devices.py#L49-L50)），手动注册设备时留空，WVP 同步设备自动填写。

> 详细请求/响应格式见 [.trae/documents/api.md](file:///Users/bianwei/Desktop/codes/DT/.trae/documents/api.md) §3.4–3.7。

---

## 六、配置项

**实现**：[app/common/config.py](file:///Users/bianwei/Desktop/codes/DT/app/common/config.py#L63-L70)

```python
# ---- WVP-GB28181 对接 ----
wvp_enabled: bool = False          # 总开关; False 时跳过自动同步与流地址刷新
wvp_api_url: str = "http://wvp:18080"  # WVP 管理后台地址 (REST API)
wvp_username: str = "admin"
wvp_password: str = "admin"
wvp_sync_interval: int = 30        # 设备轮询同步间隔 (秒)
wvp_play_protocol: str = "flv"     # AI 拉流协议: flv | rtsp
wvp_stream_sub: bool = True        # 拉子码流降低推理压力 (False=主码流)
```

均可用环境变量覆盖（`WVP_ENABLED`/`WVP_API_URL`/`WVP_USERNAME`/`WVP_PASSWORD`/`WVP_SYNC_INTERVAL`/`WVP_PLAY_PROTOCOL`/`WVP_STREAM_SUB`），见 [.env.example](file:///Users/bianwei/Desktop/codes/DT/.env.example)。

---

## 七、部署编排

**实现**：[docker-compose.yml](file:///Users/bianwei/Desktop/codes/DT/docker-compose.yml) 新增 3 个服务：

| 服务 | 镜像 | 端口 | 说明 |
|------|------|------|------|
| `mysql` | `mysql:8.0` | —（仅 WVP 内部访问） | WVP 专用，与本项目事件存储隔离 |
| `zlm` | `zlmediakit/zlmediakit:master` | `80`(http-flv)、`30000-30500/udp`(RTP) | 收 RTP 重组 H.264 转 HTTP-FLV |
| `wvp` | `648540858/wvp_pro:latest` | `18080`(管理)、`5060`(SIP TCP/UDP) | 信令平台，`depends_on: [mysql, redis, zlm]` |

backend 服务环境变量注入 WVP 配置（[docker-compose.yml:50-57](file:///Users/bianwei/Desktop/codes/DT/docker-compose.yml#L50-L57)）。

**配置文件**：

- [configs/zlm/config.ini](file:///Users/bianwei/Desktop/codes/DT/configs/zlm/config.ini)：`general.mediaServerId`、`api.secret`（与 WVP 一致）、`[hook]` 各项指向 `http://wvp:18080/index/hook/*`、RTP 端口段
- [configs/wvp/application.yml](file:///Users/bianwei/Desktop/codes/DT/configs/wvp/application.yml)：`sip(ip/domain/port/id/password)`、`media(id/ip/http-port/secret/hook-ip/stream-ip/sdp-ip)`、`user-settings(auto-apply-play=true, stream-on-demand=false)`、`redis(host=redis)`、`datasource(mysql)`

**关键配置**：`stream-on-demand=false` 让流持续不断（AI 持续拉即持续推，避免 ZLM 无人观看断流）；`mediaServerId`/`secret` 在 ZLM `config.ini` 与 WVP `application.yml` 间保持一致。

### 启用步骤

1. `.env` 设 `WVP_ENABLED=true`（并可调 `WVP_USERNAME`/`WVP_PASSWORD`/`WVP_SYNC_INTERVAL` 等）
2. 启动 WVP 全家桶：`docker compose -p smartcity up -d mysql zlm wvp`，再（重）启 `backend`
3. 在 IPC 侧配置 GB28181 指向 WVP（SIP 域 `3402000000`、SIP ID `34020000002000000001`、端口 `5060`、密码 `12345678`，见 `configs/wvp/application.yml`），建议拉子码流降低推理压力
4. IPC 在 WVP 后台显示在线后，`POST /api/devices/sync` 同步入表（status=synced）
5. `POST /api/devices/{id}/enable`（带计数线）→ AI pipeline 启动
6. 之后设备上下线/断流由 `wvp_sync`（每 30s）与 AI 断流刷新自动维护

> 跨网部署：`configs/wvp/application.yml` 的 `media.stream-ip`/`sdp-ip` 需改为 IPC 可达的宿主机/公网 IP（默认 `zlm` 仅容器内可达）。

---

## 八、关键决策取舍

| 决策 | 选择 | 理由 |
|---|---|---|
| WVP 实时性 | 定时轮询(30s)为主 | WVP 无标准对外 HTTP webhook，轮询简单可靠；30s 延迟对摄像头管理可接受 |
| webhook | 端点预留 + 轮询兜底 | 供 WVP 定制对接，不依赖 WVP 默认行为 |
| 流地址刷新归属 | 后端中转 | 单一 WVP 对接点，AI 不持 token |
| 自动同步的计数线 | 不自动启流，标记 synced 等配置 | WVP 不提供计数线；首次配线后自动维护 |
| 拉流协议 | 默认 flv，可配 rtsp | flv 稳定；rtsp 低延迟但 UDP 易丢包 |
| 码流 | 默认子码流 | 降低推理压力，主码流供前端预览 |
| 持续推流 | stream-on-demand=false | AI 持续拉即持续推，避免 ZLM 无人观看就断 |
| 流刷新冷却 | 10s | 防止断流风暴频繁打 WVP play/start |
| 启用刷新条件 | 仅 gb_device_id 非空 | 手动注册/离线处理不受影响，向后兼容 |

---

## 九、验证结果

### 单元测试 — [tests/test_wvp_client.py](file:///Users/bianwei/Desktop/codes/DT/tests/test_wvp_client.py)

mock httpx + Redis，10 个用例全部通过（`10 passed in 0.52s`）：

| 用例 | 覆盖点 |
|------|--------|
| `test_disabled_returns_empty` | `wvp_enabled=False` 禁用降级 |
| `test_login_caches_token` | 登录 token 缓存 |
| `test_list_devices_pagination` | 设备分页拉取 |
| `test_start_play_new_format` | 新版 `data.flv` 解析 |
| `test_start_play_old_format` | 旧版 `data.stream.flv` 解析 |
| `test_start_play_failure_returns_none` | 点播失败返回 None |
| `test_401_triggers_relogin` | 401 自动重登重试 |
| `test_select_stream_url_protocol` | 协议选择（flv/rtsp） |
| `test_sync_once_disabled` | 禁用时同步跳过 |
| `test_infer_camera_type` | 通道名类型推断 |

### 编译与回归

- ✅ `py_compile` 全模块通过
- ✅ 离线兼容：`url_provider=None` 时 `video_processor` 行为不变
- ✅ 手动注册兼容：`POST /api/devices` 直填 `stream_url` 路径不受影响

### 端到端验证（部署后）

1. `docker compose -p smartcity up -d redis mysql zlm wvp ai backend`
2. 用 GB28181 模拟设备或真实 IPC 注册到 WVP（或用 WVP 拉流代理把 `data/*.mp4` 代理成 GB28181 通道）
3. `POST /api/devices/sync` → 验证设备入表（status=synced）
4. `POST /api/devices/{id}/enable`（带计数线）→ 验证 AI pipeline 启动、WS 推 tracks、越线事件入库
5. 断 ZLM 流 → 验证 AI 调 `/api/devices/{id}/stream` 刷新重连
6. WVP 停设备 → 30s 内 `wvp_sync` 标 offline、AI pipeline 停

---

## 十、文件清单

### 新建

| 文件 | 说明 |
|------|------|
| [app/backend/core/wvp_client.py](file:///Users/bianwei/Desktop/codes/DT/app/backend/core/wvp_client.py) | WVP REST 客户端（登录/设备/通道/点播） |
| [app/backend/core/wvp_sync.py](file:///Users/bianwei/Desktop/codes/DT/app/backend/core/wvp_sync.py) | 30s 定时同步任务 |
| [configs/wvp/application.yml](file:///Users/bianwei/Desktop/codes/DT/configs/wvp/application.yml) | WVP SIP/媒体配置 |
| [configs/zlm/config.ini](file:///Users/bianwei/Desktop/codes/DT/configs/zlm/config.ini) | ZLMediaKit 配置 |
| [tests/test_wvp_client.py](file:///Users/bianwei/Desktop/codes/DT/tests/test_wvp_client.py) | 10 个单元测试 |

### 修改

| 文件 | 改动 |
|------|------|
| [app/common/config.py](file:///Users/bianwei/Desktop/codes/DT/app/common/config.py#L63-L70) | WVP 配置段（7 项） |
| [app/backend/main.py](file:///Users/bianwei/Desktop/codes/DT/app/backend/main.py#L48-L65) | lifespan 启停 syncer + 关闭 client |
| [app/backend/api/devices.py](file:///Users/bianwei/Desktop/codes/DT/app/backend/api/devices.py) | gb 字段 + 4 个新端点（sync/stream/enable/webhook） |
| [app/ai/stream.py](file:///Users/bianwei/Desktop/codes/DT/app/ai/stream.py#L35-L95) | `url_provider` 回调 + 重连逻辑 |
| [app/ai/pipeline.py](file:///Users/bianwei/Desktop/codes/DT/app/ai/pipeline.py#L121-L137) | `_refresh_stream_url` 流刷新 |
| [app/ai/service.py](file:///Users/bianwei/Desktop/codes/DT/app/ai/service.py#L43-L86) | `gb_device_id` 字段透传 + `enable_url_refresh` |
| [docker-compose.yml](file:///Users/bianwei/Desktop/codes/DT/docker-compose.yml#L76-L121) | mysql/zlm/wvp 服务 |
| [.env.example](file:///Users/bianwei/Desktop/codes/DT/.env.example) | WVP 环境变量示例 |

### 文档同步

| 文档 | 内容 |
|------|------|
| [.trae/documents/api.md](file:///Users/bianwei/Desktop/codes/DT/.trae/documents/api.md) | §3.4–3.7 WVP 端点说明 |
| [README.md](file:///Users/bianwei/Desktop/codes/DT/README.md) | 架构图补 WVP+ZLM 节点、启用 GB28181/WVP 章节 |
| [docs/algorithm_plan.md](file:///Users/bianwei/Desktop/codes/DT/docs/algorithm_plan.md#L70-L79) | §1.4 视频接入：GB28181/WVP 自动同步 |
