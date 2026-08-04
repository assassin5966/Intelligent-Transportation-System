# Intelligent Transportation System / 智慧古城车辆人流监管平台

基于 GB28181 视频平台 → AI 分析 → 业务后端 → Redis → 展示大屏 的轻量化架构。
AI 与后端均采用 Python，**共用一个 Docker 镜像**，以不同启动命令运行两个服务。

## 系统架构

```
IPC 摄像头 (GB28181/H.264)
      │ ① SIP INVITE + RTP
      ▼
┌──────────────────────┐  ⑤ HTTP-FLV/RTSP  ┌─────────────────┐  ④ HTTP POST 事件  ┌─────────────────┐
│  WVP + ZLMediaKit    │ ─────────────────▶│  AI 分析服务     │ ──────────────────▶│  业务后端        │
│  信令 + 流媒体        │                   │  YOLO11+BoT-SORT │  VehicleEnter/Exit │  (FastAPI)       │
│  18080 / 80 / 5060   │                   │  +越线计数        │  PersonEnter/Exit  │  端口 8000       │
└──────────▲───────────┘                   └─────────────────┘                    └────────┬────────┘
           │                                                                            │
           │ ② REST 设备同步 / play/start / 流地址刷新                                  │
           └────────────────────────────────────────────────────────────────────────────┘
                                  │
                      ┌───────────┴───────────┐
                      ▼                       ▼
                ┌──────────┐            ┌──────────┐
                │  Redis   │            │ Chronos  │
                │ 状态/告警 │            │ 时序预测 │
                └──────────┘            └──────────┘
```

- **WVP + ZLMediaKit**：GB28181 信令平台 + 流媒体。IPC 经 SIP 注册到 WVP，点播时 WVP 向 IPC 发 INVITE，ZLM 收 RTP 重组 H.264 并转封装为 HTTP-FLV 供 AI 拉流。后端通过 WVP REST API 自动同步设备、获取/刷新流地址（`WVP_ENABLED=true` 时启用）。
- **AI 分析服务**：YOLO11 检测 + BoT-SORT 跟踪 + 越线计数 + 视频异常识别（黑屏/花屏），仅输出业务事件（不传视频），大幅降低通信压力。断流时回调后端 `/api/devices/{id}/stream` 刷新 FLV 地址。
- **业务后端**：实时统计、规则告警、REST API、时序预测、警力分配，并作为 WVP 唯一对接点（设备同步、点播、流地址刷新）。
- **时序预测**：接入 Chronos-2 本地模型，基于 N 分钟区间历史总人数序列预测未来 N 分钟总人数。

## 程序流程

### 后端启动（main.py lifespan）

后端启动时按顺序拉起 4 个后台调度器，任一失败不影响其余：

| 调度器 | 频率 | 作用 |
|--------|------|------|
| 时序预测 | 15 分钟 | 读 hourly 序列 → Chronos 预测 → 缓存 Redis（模型缺失降级线性外推）|
| 离线检测 | 30 秒 | 检查 AI 心跳，90s 无心跳标记设备离线 |
| 警力分配 | 15 分钟 | 按区域人流/车流估算需求 → 比例分配警力 → 输出 allocation 计划 |
| WVP 同步 | 30 秒 | `WVP_ENABLED=true` 时同步 WVP 设备/通道，启停 AI 管道 |

### 端到端数据流

```
GB28181 摄像头 --SIP注册--> WVP + ZLMediaKit
                                │ 后端每30s同步设备/通道
                                ▼
业务后端(8000) ── /api/devices/{id}/stream ──▶ AI 拉流(FLV)
  ▲                                              │
  │ ④ 越线事件 POST /api/events                  │ ⑤ 异常事件 POST /api/alerts/anomaly
  │    心跳   POST /api/devices/{id}/heartbeat   │    轨迹   WebSocket /ws 实时推送
  │                                              ▼
  └── apply_event 写Redis ◀── 实时统计/日/小时聚合
       + alerts.evaluate(告警评估, 5分钟去重)
       + WebSocket /ws 推送前端大屏
```

### AI 管道（DevicePipeline._run）

每路摄像头的处理流水线：

1. **拉流** `stream_frames(url, url_provider)` — 支持 WVP FLV 地址刷新回调
2. **首帧** `counter.set_frame_size()` — 重算 ROI 像素坐标 + 计数线裁剪区间
3. **异常检测** `AnomalyMonitor.check(frame)` — 黑屏/花屏，状态转移(onset/recovery)时上报
4. **检测跟踪** `tracker.track(frame)` — YOLO11 + BoT-SORT（`asyncio.to_thread` 不阻塞事件循环）
5. **越线计数** `counter.process_tracks()` — ROI 过滤 + 计数线裁剪 + 双向计数 + 防抖链
6. **事件输出** 越线事件 → POST `/api/events` + WebSocket；异常事件 → POST `/api/alerts/anomaly` + WebSocket
7. **心跳** 每 30s POST `/api/devices/{id}/heartbeat`（供离线检测）

### 越线计数（counter.py）

单计数线 + 内侧锚点方案，Enter/Exit 为绝对语义（与线绘制方向无关）：

- **ROI 过滤**：轨迹中心点不在多边形内 → 跳过（减算力、过滤画面边缘干扰）
- **计数线裁剪**：ROI 启用时自动裁剪线段到 ROI 内，ROI 外线段不触发计数
- **双向计数**：同一轨迹来回跨线独立计数（不因单向去重漏计反向跨线）
- **防抖链**：跨线检测(offset异号) → 夹角过滤 → 投影落在线段内 → 远离线 → 端点过滤 → 滞留确认(3帧) → 方向判定 → ID 切换检测 → 去重(TTL 3600s)

### 断流重连（stream.py）

```
读流失败
  ├─ 第1级: 当前 url 重连 (tenacity 5次指数退避 2-30s)
  ├─ 第2级: url_provider 回调后端 /api/devices/{id}/stream (WVP 重新 play/start)
  │         刷新冷却 10s 防频繁打 WVP
  └─ 新地址重连 → 成功继续 / 失败放弃
```

### WVP 同步（wvp_sync.py，每 30s）

`WVP_ENABLED=true` 时，后端作为 WVP 唯一对接点：

- 拉取 WVP 设备/通道 → 与 Redis 按 `gb_device_id + gb_channel_id` 比对
- **新通道**：入表（status=synced），不启流（缺计数线，等用户 `POST /enable` 配置）
- **已配置 + WVP 在线**：AI 管道未运行 → 重新启流
- **WVP 离线**：停 AI 管道，标记 offline
- **WVP 恢复**：重新启流
- 启发式推断 camera_type（名含"车"→vehicle，"人"→person）

### 事件处理（events.py → realtime.py → alerts.py）

```
POST /api/events {device_id, event_type, occurred_at}
  ├─ apply_event() 写 Redis:
  │    ├─ sc:realtime:current            当前车辆/人员数（负值钳位）
  │    ├─ sc:realtime:daily:YYYYMMDD      当日累计（90天TTL）
  │    └─ sc:realtime:hourly:YYYYMMDDHH   小时聚合（8天TTL，供预测读取）
  └─ BackgroundTask: alerts.evaluate()
       ├─ rules.yaml 热加载（按 mtime，改规则无需重启）
       ├─ 评估指标 vs 阈值
       └─ 触发告警（SET NX EX 300 去重）→ sc:alerts（保留最近1000条）
```

## 技术栈

| 层 | 技术 |
|----|------|
| AI 视觉 | ultralytics (YOLO11) + ByteTrack + OpenCV + PyTorch |
| 时序预测 | Chronos (HuggingFace transformers) |
| 业务后端 | FastAPI + Pydantic v2 + asyncio |
| 实时状态/历史 | Redis (redis.asyncio) |
| 通信 | HTTPX + WebSockets |

## 目录结构

```
智慧交通项目/
├── app/
│   ├── common/        # 配置·日志·Redis (基础层)
│   ├── schemas/       # 事件/统计 Pydantic 契约
│   ├── ai/            # YOLO11检测·BoT-SORT跟踪·越线计数(ROI+双向)·异常识别·拉流·管道·服务入口
│   ├── backend/       # FastAPI: 事件·统计·告警·设备·警力·WebSocket·WVP同步·离线检测
│   └── prediction/    # Chronos 时序预测·路由·定时调度
├── configs/
│   ├── rules.yaml     # 告警规则 (车辆>300红警 / 游客>10000饱和)
│   ├── bytetrack.yaml # BoT-SORT 跟踪配置
│   ├── wvp/           # WVP 信令平台配置 (application.yml)
│   └── zlm/           # ZLMediaKit 流媒体配置 (config.ini)
├── deploy/wvp/        # WVP arm64 自建镜像部署 (gitee源码maven编译)
│   ├── wvp/Dockerfile # WVP 2.7.4 多阶段构建镜像
│   ├── wvp/application.yml
│   ├── zlmediakit/config.ini
│   └── mysql/init.sql # WVP 数据库初始化 SQL
├── scripts/           # 离线处理·冒烟测试·异常视频生成
├── tool/              # 离线视频处理器 (独立组件, 不依赖 Redis)
├── tests/             # WVP客户端·异常识别 单元测试
├── Dockerfile         # 统一镜像 (AI + 后端)
├── docker-compose.yml # 主编排: ai + backend + redis (+ wvp/zlm/mysql 可选)
├── docker-compose.wvp.yml # WVP 信令平台独立编排 (arm64 自建镜像, host网络)
├── requirements.txt
└── .env.example
```

## 环境要求

- Docker 20.10+ （含 BuildKit）
- Docker Compose v2
- 基础镜像 `python:3.11-slim`（构建时自动拉取）

## 手动构建镜像

镜像**自动适配 CPU 架构**，无需额外参数：

| 架构 | torch 来源 | 说明 |
|------|-----------|------|
| x86_64 (amd64) | `download.pytorch.org/whl/cpu` | 轻量 CPU 版 (~200MB) |
| aarch64 (arm64) | PyPI (清华镜像) | 官方 CPU 源无 aarch64 wheel，用 PyPI 版（含 CUDA，可在 GPU 服务器推理）|

```bash
# 1. 默认构建（自动按当前机器架构选 torch 源）
docker build -t smart-city-platform:latest .

# 2. (可选) 指定镜像名/标签
docker build -t smart-city-platform:0.1.0 .

# 3. (可选) 不使用缓存重建
docker build --no-cache -t smart-city-platform:latest .
```

> 构建已内置国内加速：apt 用清华 debian 源、pip 用清华 PyPI 源。
> 若在 amd64 机器上需要 GPU 版 torch，编辑 Dockerfile 第 2 步把 amd64 分支的
> `--index-url` 改为 `https://download.pytorch.org/whl/cu121`。

## 启动服务（Docker Compose）

```bash
# 复制环境变量示例并按需修改
cp .env.example .env

# 构建并启动全部服务 (ai + backend + redis)
docker compose -p smartcity up -d --build

# 查看状态
docker compose -p smartcity ps

# 查看日志
docker compose -p smartcity logs -f backend

# 停止
docker compose -p smartcity down
```

服务端口（默认）：
- 后端 API：`8000`
- AI 分析服务：`8001`
- Redis：`16379`（主机映射，避免与宿主机 6379 冲突）
- WVP 管理后台：`18080` / SIP：`5060`
- ZLMediaKit HTTP-FLV：`80` / RTP 收包：`30000-30500/udp`

> 注：docker-compose.yml 未固定 container_name，使用 `-p smartcity` 项目名隔离，
> 适合在共享服务器上运行，避免与其他项目冲突。

### 启用 GB28181/WVP 接入（可选）

默认 `WVP_ENABLED=false`，设备靠手填 `stream_url` 注册（兼容旧流程）。启用 WVP 自动同步：

1. `.env` 设 `WVP_ENABLED=true`（并可调 `WVP_USERNAME`/`WVP_PASSWORD`/`WVP_SYNC_INTERVAL` 等）。
2. 启动 WVP 全家桶：`docker compose -p smartcity up -d mysql zlm wvp`，再（重）启 `backend`。
3. 在 IPC 侧配置 GB28181 指向 WVP（SIP 域 `3402000000`、SIP ID `34020000002000000001`、端口 `5060`、密码 `12345678`，见 `configs/wvp/application.yml`），建议拉子码流降低推理压力。
4. IPC 在 WVP 后台显示在线后，`POST /api/devices/sync` 同步入表 → `POST /api/devices/{id}/enable` 配计数线启流。
5. 之后设备上下线/断流由后台 `wvp_sync`（每 30s）与 AI 断流刷新自动维护。

> 跨网部署：`configs/wvp/application.yml` 的 `media.stream-ip`/`sdp-ip` 需改为 IPC 可达的宿主机/公网 IP（默认 `zlm` 仅容器内可达）。

## 部署到新机器（跨机器构建指南）

镜像不随仓库分发，需在目标机器上本地构建（Dockerfile 已跨架构自动适配）。

### 前置条件
- 已安装 Docker 20.10+（含 BuildKit）与 Docker Compose v2
- Windows 需 10 22H2 (build 19045)+ 或 Windows 11（否则无法安装 Docker Desktop）；Linux / macOS 直接安装 Docker Engine / Docker Desktop

### 标准步骤
```bash
# 1. 克隆仓库
git clone git@github.com:assassin5966/Intelligent-Transportation-System.git
cd Intelligent-Transportation-System

# 2. 准备环境变量
cp .env.example .env   # Windows PowerShell: copy .env.example .env

# 3. 构建镜像（自动按当前机器 CPU 架构选 torch 源，无需任何参数）
docker build -t smart-city-platform:latest .

# 4. 启动全栈 (ai + backend + redis)
docker compose -p smartcity up -d --build
```

### 国内网络加速
- **pip / apt 已内置加速**：Dockerfile 配置了清华 PyPI + 清华 debian 源，构建时装包很快。
- **Docker Hub 基础镜像拉取慢**时，用 `docker.1ms.run` 镜像前缀预拉并重打标，构建/启动时直接走本地缓存：
  ```bash
  docker pull docker.1ms.run/library/python:3.11-slim && docker tag docker.1ms.run/library/python:3.11-slim python:3.11-slim
  docker pull docker.1ms.run/library/redis:7-alpine && docker tag docker.1ms.run/library/redis:7-alpine redis:7-alpine
  ```
  预拉后再 `docker compose up`，基础镜像命中本地缓存，不再连 Docker Hub。
- **GitHub 克隆慢**：可用 HTTPS 方式 `https://github.com/assassin5966/Intelligent-Transportation-System.git`，或配置 git 代理。

### CPU 架构自动适配

| 架构 | torch 来源 | 说明 |
|------|-----------|------|
| x86_64 (amd64，常见 PC/服务器) | `download.pytorch.org/whl/cpu` | 轻量 CPU 版 (~200MB) |
| aarch64 (arm64，如 Grace/树莓派) | PyPI | 官方 CPU 源无 aarch64 wheel，用 PyPI 版（含 CUDA，可在 GPU 服务器推理）|

- amd64 机器如需 **GPU 版 torch**：编辑 Dockerfile 中 amd64 分支，把 `--index-url` 改为 `https://download.pytorch.org/whl/cu121`，并在 docker-compose.yml 中启用 ai 服务的 `deploy.resources.reservations.devices` GPU 块。
- 镜像只在本机构建、本机运行，不会自动同步；换机器重新 `docker build` 即可。

## API 接口

### 后端（端口 8000）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| POST | `/api/events` | 接收 AI 推送的事件 `{device_id, event_type, occurred_at}` |
| GET | `/api/stats/realtime` | 实时统计（当前车辆/人员、今日累计、活跃设备）|
| GET | `/api/alerts?limit=100` | 告警列表（Redis 保留最近 1000 条）|
| POST | `/api/alerts/anomaly` | 视频异常上报（AI→后端，黑屏/花屏 onset/recovery）|
| GET/POST/DELETE | `/api/devices` | 设备管理（含计数线/anchor/ROI 配置）|
| POST | `/api/devices/sync` | 手动触发 WVP 设备同步 |
| POST | `/api/devices/{id}/enable` | 配置计数线/ROI 并启动 AI 管道 |
| POST | `/api/devices/{id}/heartbeat` | AI 心跳上报（供离线检测，90s 超时标离线）|
| GET | `/api/devices/{id}/stream` | 获取/刷新 FLV 流地址（WVP play/start，AI 断流刷新用）|
| POST | `/api/devices/wvp-webhook` | WVP 设备上下线 webhook 回调 |
| GET/POST/DELETE | `/api/police/regions` | 警力区域管理 |
| GET | `/api/police/allocation` | 当前警力分配结果 |
| POST | `/api/police/optimize` | 触发警力优化分配 |
| GET | `/api/police/plan` | 警力调度计划 |
| GET | `/api/prediction/health` | 预测服务健康（含降级状态）|
| POST | `/api/prediction/predict` | 时序预测（总人数）|
| GET | `/api/prediction/latest` | 最近一次定时预测缓存 |
| WS | `/ws` | WebSocket 实时推送（tracks / crossing_event / video_anomaly）|

### AI 分析服务（端口 8001）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| GET/POST/DELETE | `/devices` | 注册/列出/停止摄像头管道 |

事件类型：`VehicleEnter`、`VehicleExit`、`PersonEnter`、`PersonExit`

## 配置说明

通过环境变量配置（见 `.env.example`），关键项：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `REDIS_URL` | `redis://redis:6379/0` | Redis 连接 |
| `YOLO_MODEL` | `models/yolo11n.pt` | YOLO 权重 |
| `CHRONOS_MODEL` | `models` | Chronos-2 本地模型目录 |
| `PREDICTION_HORIZON` | `60` | 预测步长（小时）|
| `PREDICTION_HISTORY_HOURS` | `168` | 预测历史窗口（小时）|
| `RULES_FILE` | `configs/rules.yaml` | 告警规则文件 |
| `WVP_ENABLED` | `false` | 启用 WVP 自动同步（false 时设备靠手填 stream_url）|
| `WVP_API_URL` | `http://wvp:18080` | WVP REST API 地址 |
| `WVP_USERNAME` | `admin` | WVP 管理账号 |
| `WVP_PASSWORD` | `admin` | WVP 管理密码 |
| `WVP_SYNC_INTERVAL` | `30` | WVP 设备同步间隔（秒）|
| `WVP_PLAY_PROTOCOL` | `flv` | 点播流协议（flv/rtsp）|

## 开发指南

```bash
# 后端开发（热加载，挂载本地 app/ 目录）
docker compose -p smartcity up -d redis
docker compose -p smartcity up backend  # 挂载 ./app 实时生效

# 冒烟测试（仅需 Redis，验证后端 API）
bash scripts/smoke_test.sh
```

## 开发流程（来自开发方案）

| 阶段 | 内容 | 状态 |
|------|------|------|
| 视频接入 | GB28181/RTSP 接入、多路管理 | ✅ 框架就绪 |
| AI 分析 | 检测、跟踪、越线计数、HTTP 推送 | ✅ |
| 后端 | 统计、Redis、REST API、告警 | ✅ |
| 预测 | Chronos 预测接口 | ✅ |
| 警力分配 | 区域需求估算 + 比例分配 + 贪心调度 | ✅ |
| 视频异常识别 | 黑屏 + 花屏检测（在线/离线双路径，复用告警体系） | ✅ |
| 展示大屏 | Vue3 + ECharts | ⏳ 待开发（前端同事） |
| 联调 | 设备、AI、后端、大屏 | ⏳ |
| 测试部署 | 性能测试、Docker 部署 | ✅ 镜像已验证 |
