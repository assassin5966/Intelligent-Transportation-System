# 应用核心层 (app/)

平台的**应用代码根包**，包含两大常驻服务与共享基础设施，统一以 `app` 为 Python 包名导入（如 `from app.common.config import settings`）。

- **AI 视觉分析服务**（`app/ai`，端口 8001）：摄像头拉流 → YOLO11 检测 + ByteTrack 跟踪 → 越线计数 / ROI / 异常检测 → WebSocket 实时推送 + 事件上报后端。详见 [`README-ai.md`](README-ai.md)。
- **业务后端服务**（`app/backend`，端口 8000）：聚合 AI 事件，提供实时统计 / 告警 / 拥挤判断 / 警力分配 / 时序预测，并通过 WVP-GB28181 同步设备与流地址，对外暴露 REST + WebSocket。详见 [`README-backend.md`](README-backend.md)。
- 两者通过 `app.common.config.settings`（pydantic-settings）统一读取配置，经 Redis / MySQL 与 WVP 协同。

---

## 1. 项目结构

```
app/
├─ __init__.py
├─ ai/                    # AI 视觉分析服务 (FastAPI, :8001) — 见 README-ai.md
│  ├─ service.py          # 入口: 设备管道注册/启停, 模型池加载
│  ├─ pipeline.py         # 单设备管道: 拉流→解码→推理→计数→事件
│  ├─ model_pool.py / gpu_engine.py / tracker.py   # 批量推理资源池 / GPU 引擎 / ByteTrack
│  ├─ counter.py / crossing_detector.py / roi_detector.py / anomaly.py  # 计数/越线/ROI/异常
│  ├─ track_state_manager.py / traj_state.py / id_switch_detector.py    # 轨迹状态/ID 跳变保护
│  ├─ inference_scheduler.py / debounce_validator.py / event_generator.py
│  ├─ stream.py / geometry_engine.py / mock_simulator.py
├─ backend/               # 业务后端 (FastAPI, :8000) — 见 README-backend.md
│  ├─ main.py             # 入口: 路由注册 + lifespan 启动各调度器
│  ├─ api/                # 路由层: devices/device_info/events/stats/alerts/config_rules/police/ws
│  └─ core/               # 逻辑层: realtime/wvp_client/wvp_sync/allocator/police_scheduler/
│                         #         congestion/alerts/heartbeat/stream_urls/archive_scheduler
├─ common/                # 共享基础设施
│  ├─ config.py           # 全局配置 (pydantic-settings, Settings, 所有服务的配置源)
│  ├─ logger.py           # 日志 (loguru, 控制台+文件双写)
│  ├─ redis_client.py     # Redis 异步客户端 (实时状态/计数缓存)
│  ├─ mysql_client.py     # MySQL 异步客户端 (aiomysql, 归档/设备信息)
│  ├─ device_info.py      # 设备信息表: MySQL 建表+种子导入+内存缓存
│  └─ business_rules.py   # 业务规则加载与 mtime 热重载 (business_rules.yaml)
├─ prediction/            # 时序预测 (Chronos-2)
│  ├─ chronos_model.py    # Chronos 模型封装 (本地目录加载/推理)
│  ├─ repository.py       # 历史序列存取 (Redis/MySQL)
│  ├─ scheduler.py        # 预测调度器 (start_scheduler, 由 backend lifespan 启动)
│  └─ router.py           # 预测 HTTP 路由 (可选)
└─ schemas/               # pydantic 数据模型
   └─ events.py           # 越线/区域事件 schema (前后端/服务间契约)
```

---

## 2. 项目依赖

整个 `app/` 共用根 `requirements.txt`，按职责分层（详见子模块 README）：

| 子包 | 主要依赖 |
|---|---|
| `app/ai` | `ultralytics>=8.3.0,<9.0.0`、`opencv-python-headless>=4.9.0`、`numpy>=1.26.0`、`lap`；`torch`/`torchvision` 由 Dockerfile 按架构单独装（2.7.0，CPU 源 / GPU cu121） |
| `app/backend` | `fastapi`、`uvicorn[standard]`、`pydantic`、`pydantic-settings`、`redis`、`aiomysql`、`httpx`、`websockets`、`loguru`、`tenacity`、`pyyaml`、`cryptography` |
| `app/prediction` | `chronos-forecasting>=1.4.0`、`transformers>=4.40.0`、`accelerate>=0.30.0` |
| `app/common`/`app/schemas` | 上述各层共享（配置/日志/客户端/模型） |

> `torch` 不在 `requirements.txt`，原因是跨平台 wheel 选择（CPU vs GPU）需在镜像构建阶段按架构确定，见根 `Dockerfile`/`Dockerfile.gpu` 注释。

---

## 3. 项目使用版本

| 组件 | 版本 / 说明 |
|---|---|
| Python | 3.11（`python:3.11-slim-bookworm`） |
| PyTorch | 2.7.0 跨平台 wheel（CPU 源 / GPU cu121 轮子） |
| CUDA | GPU 服务器 12.2（cu121 轮子向下兼容，驱动 R530+） |
| YOLO | YOLO11n（默认 `models/yolo11n.pt`） |
| 跟踪器 | ByteTrack |
| 时序预测 | Chronos-2（本地模型目录 `models/`） |
| FastAPI | 随 `requirements.txt`（>=0.11x） |
| Redis / MySQL | 7 / 8.0 |

---

## 4. 部署配置

- **构建**：`app/` 由 `Dockerfile` / `Dockerfile.gpu` 构建进同一镜像 `smart-city-platform`（CPU `:latest` / GPU `:gpu`），`COPY app /app/app`。**AI 与后端共用同一镜像**，仅启动命令不同。
- **启动命令**：
  - AI：`python -m app.ai.service`（监听 `settings.ai_port`，默认 8001）
  - 后端：`uvicorn app.backend.main:app --host 0.0.0.0 --port 8000`
  - 由 `docker-compose.yml` / `docker-compose.gpu.yml` 编排为 `dt-ai` / `dt-backend`（compose 中分别指定命令）。
- **配置来源**（统一经 `app.common.config.settings`，pydantic-settings 从环境变量读取）：
  - 环境变量：`.env`（CPU）/` .env.gpu`（GPU），compose 经 `--env-file` 显式传入。
  - 业务规则：`configs/business_rules.yaml`、`configs/rules.yaml`、`configs/police.yaml`，支持基于 mtime **热重载**，无需重启。
  - 权重：`models/`（YOLO11n + Chronos-2，离线预置，缺失时构建期报错）。
  - 静态资源：`static/`（运维页 html）。
- **挂载策略**：`app/` 代码构建进镜像（非 bind mount）；`configs/`、`models/`、`static/`、`logs/` 以 bind mount 进容器，便于不重建镜像即可改配置/权重/瓦片。
- **服务间通信**：容器内用服务名互访——`ai:8001` / `backend:8000` / `redis` / `mysql` / `wvp`；经 `dt_default`（业务内部网）与 `wvp-shared`（对接 WVP/ZLM）两张网络。
- **Mock 模式**：`mock_enabled=true` 时 AI 不拉流（由 mock_simulator 生成事件），后端强制关闭 WVP 同步，用于无真实流联调。
- **存活检查**：AI `GET /health`（含推理 mode/reason）、后端 `GET /api/health`。

> 完整部署流程（离线一键、systemd 自启、Mock、校验等）见根 [`README.md`](README.md) §4；离线镜像/权重闭环见 [`docs/OFFLINE_BUILD.md`](docs/OFFLINE_BUILD.md)。

---

## 5. 相关文档

- [`README-ai.md`](README-ai.md) — AI 视觉分析服务详解
- [`README-backend.md`](README-backend.md) — 业务后端详解
- [`README.md`](README.md) — 项目总览与部署
- [`docs/OFFLINE_BUILD.md`](docs/OFFLINE_BUILD.md) — 离线构建闭环
