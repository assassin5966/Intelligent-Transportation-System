# Intelligent Transportation System / 智慧古城车辆人流监管平台

基于 GB28181 视频平台 → AI 分析 → 业务后端 → Redis → 展示大屏 的轻量化架构。
AI 与后端均采用 Python，**共用一个 Docker 镜像**，以不同启动命令运行两个服务。

## 系统架构

```
GB28181/RTSP 视频流
      │
      ▼
┌─────────────────┐   HTTP POST 事件    ┌─────────────────┐
│  AI 分析服务     │ ───────────────────▶│  业务后端        │
│  (YOLO11+ByteTrack│  VehicleEnter/Exit │  (FastAPI)       │
│   +越线计数)      │  PersonEnter/Exit   │                  │
│  端口 8001       │                     │  端口 8000       │
└─────────────────┘                     └────────┬────────┘
                                                 │
                          ┌──────────────────────┴──────────────────┐
                          ▼                                         ▼
                    ┌──────────────────┐                    ┌──────────┐
                    │      Redis       │                    │  Chronos  │
                    │ 实时状态 / 告警  │                    │ 时序预测 │
                    │ 小时聚合 / 趋势  │                    └──────────┘
                    └──────────────────┘
```

- **AI 分析服务**：YOLO11 检测 + BoT-SORT 跟踪 + 越线计数 + 视频异常识别（黑屏/花屏），仅输出业务事件（不传视频），大幅降低通信压力。
- **业务后端**：实时统计、规则告警、REST API、时序预测、警力分配。
- **时序预测**：接入 Chronos-2 本地模型，基于 N 分钟区间历史总人数序列预测未来 N 分钟总人数。

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
│   ├── ai/            # YOLO11 检测·ByteTrack 跟踪·越线计数·RTSP 拉流·管道·服务入口
│   ├── backend/       # FastAPI: 事件接收·实时统计·告警·趋势·设备·小时聚合
│   └── prediction/    # Chronos 时序预测·路由·定时调度
├── configs/rules.yaml # 告警规则 (车辆>300红警 / 游客>10000饱和)
├── scripts/
│   ├── run_video_processor.sh  # 离线视频处理 (Docker 一键运行)
│   └── smoke_test.sh           # 冒烟测试
├── tool/              # 离线视频处理器 (独立组件)
├── Dockerfile         # 统一镜像 (AI + 后端)
├── docker-compose.yml # 编排: ai + backend + redis + ai-processor
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

> 注：docker-compose.yml 未固定 container_name，使用 `-p smartcity` 项目名隔离，
> 适合在共享服务器上运行，避免与其他项目冲突。

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
| GET/POST/DELETE | `/api/devices` | 设备管理（含越线计数线配置）|
| GET | `/api/prediction/health` | 预测服务健康 |
| POST | `/api/prediction/predict` | 时序预测（总人数，无需参数） |
| GET | `/api/prediction/latest` | 最近一次定时预测缓存 |

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
