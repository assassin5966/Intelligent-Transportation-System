# Intelligent Transportation System / 智慧古城车辆人流监管平台

基于 GB28181 视频平台 → AI 分析 → 业务后端 → Redis/MySQL → 展示大屏 的轻量化架构。
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
                          ┌──────────────────────┼──────────────────┐
                          ▼                      ▼                  ▼
                    ┌──────────┐          ┌──────────┐        ┌──────────┐
                    │  Redis   │          │  MySQL   │        │ Chronos  │
                    │ 实时状态  │          │ 历史数据  │        │ 时序预测 │
                    └──────────┘          └──────────┘        └──────────┘
```

- **AI 分析服务**：YOLO11 检测 + ByteTrack 跟踪 + 越线计数，仅输出业务事件（不传视频），大幅降低通信压力。
- **业务后端**：实时统计、规则告警、REST API、小时聚合、时序预测。
- **时序预测**：接入 Chronos 大模型，预测未来 15/30/45/60 分钟人流/车辆趋势。

## 技术栈

| 层 | 技术 |
|----|------|
| AI 视觉 | ultralytics (YOLO11) + ByteTrack + OpenCV + PyTorch |
| 时序预测 | Chronos (HuggingFace transformers) |
| 业务后端 | FastAPI + Pydantic v2 + asyncio |
| 实时状态 | Redis (redis.asyncio) |
| 历史数据 | MySQL 8 + SQLAlchemy 2.0 (async) + aiomysql |
| 通信 | HTTPX + WebSockets |

## 目录结构

```
智慧交通项目/
├── app/
│   ├── common/        # 配置·日志·Redis·MySQL·ORM 模型 (基础层)
│   ├── schemas/       # 事件/统计 Pydantic 契约
│   ├── ai/            # YOLO11 检测·ByteTrack 跟踪·越线计数·RTSP 拉流·管道·服务入口
│   ├── backend/       # FastAPI: 事件接收·实时统计·告警·趋势·设备·小时聚合
│   └── prediction/    # Chronos 时序预测·路由·定时调度
├── configs/rules.yaml # 告警规则 (车辆>300红警 / 游客>10000饱和)
├── scripts/
│   ├── init_db.py     # 建表脚本
│   └── smoke_test.sh  # 冒烟测试
├── Dockerfile         # 统一镜像 (AI + 后端)
├── docker-compose.yml # 编排: ai + backend + redis + mysql
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

# 构建并启动全部服务 (ai + backend + redis + mysql)
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
- MySQL：`13306`（主机映射，避免与宿主机 3306 冲突）

> 注：docker-compose.yml 未固定 container_name，使用 `-p smartcity` 项目名隔离，
> 适合在共享服务器上运行，避免与其他项目冲突。

## API 接口

### 后端（端口 8000）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| POST | `/api/events` | 接收 AI 推送的事件 `{device_id, event_type, occurred_at}` |
| GET | `/api/stats/realtime` | 实时统计（当前车辆/人员、今日累计、活跃设备）|
| GET | `/api/stats/trend?hours=24` | 历史趋势曲线 |
| GET | `/api/alerts?limit=100` | 告警列表 |
| GET/POST/DELETE | `/api/devices` | 设备管理（含越线计数线配置）|
| GET | `/api/prediction/health` | 预测服务健康 |
| POST | `/api/prediction/predict` | 时序预测 `{metric, horizon}` |
| GET | `/api/prediction/latest?metric=vehicle` | 最近一次定时预测缓存 |

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
| `MYSQL_URL` | `mysql+aiomysql://smartcity:smartcity123@mysql:3306/smart_city` | MySQL 连接 |
| `YOLO_MODEL` | `yolo11n.pt` | YOLO 权重 |
| `CHRONOS_MODEL` | `amazon/chronos-t5-tiny` | Chronos 模型 |
| `RULES_FILE` | `configs/rules.yaml` | 告警规则文件 |

## 开发指南

```bash
# 后端开发（热加载，挂载本地 app/ 目录）
docker compose -p smartcity up -d redis mysql
docker compose -p smartcity up backend  # 挂载 ./app 实时生效

# 初始化数据库表
docker compose -p smartcity exec backend python -m scripts.init_db

# 冒烟测试（仅需 Redis，验证后端 API）
bash scripts/smoke_test.sh
```

## 开发流程（来自开发方案）

| 阶段 | 内容 | 状态 |
|------|------|------|
| 视频接入 | GB28181/RTSP 接入、多路管理 | ✅ 框架就绪 |
| AI 分析 | 检测、跟踪、越线计数、HTTP 推送 | ✅ |
| 后端 | 统计、Redis、MySQL、REST API、告警 | ✅ |
| 预测 | Chronos 预测接口 | ✅ |
| 展示大屏 | Vue3 + ECharts | ⏳ 待开发 |
| 联调 | 设备、AI、后端、大屏 | ⏳ |
| 测试部署 | 性能测试、Docker 部署 | ✅ 镜像已验证 |
