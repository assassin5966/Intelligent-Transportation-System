# 业务后端服务 (app/backend)

平台的**业务中枢**：聚合 AI 分析服务上报的越线/区域事件，提供实时统计、告警、拥挤判断、警力分配、时序预测，并通过 WVP-GB28181 同步设备与流地址；对外暴露 REST API 与 WebSocket 供大屏前端消费。

> 入口：`uvicorn app.backend.main:app --host 0.0.0.0 --port 8000`（FastAPI，端口 **8000**）
> 与 `app/ai`、`app/common`、`app/prediction`、`app/schemas` 同属一个 Python 包 `app`，共用根 `requirements.txt`。

---

## 1. 项目结构

```
app/backend/
├─ main.py                 # FastAPI 入口: 注册路由, lifespan 启动各调度器 (预测/心跳/警力/WVP同步/归档/设备信息)
├─ api/                    # HTTP/WS 路由层
│  ├─ devices.py           # 设备列表/详情/流地址
│  ├─ device_info.py       # 设备名称/经纬度/分类 (MySQL + 内存缓存)
│  ├─ events.py            # 越线/区域事件查询
│  ├─ stats.py             # 实时统计 / 存量清零 (/api/stats/reset)
│  ├─ alerts.py            # 告警列表/确认
│  ├─ config_rules.py      # 业务规则热重载接口
│  ├─ police.py            # 警力分配查询/调整
│  └─ ws.py                # WebSocket 实时推送 (计数/事件/状态)
├─ core/                   # 业务逻辑层
│  ├─ realtime.py          # 实时统计聚合 (Redis 读写、小时级累计)
│  ├─ wvp_client.py        # WVP REST 客户端 (点播/设备查询)
│  ├─ wvp_sync.py          # 设备/通道定时同步 + 流地址刷新
│  ├─ allocator.py         # 警力分配算法 (按当前/预测人数需求)
│  ├─ police_scheduler.py  # 警力分配定时调度
│  ├─ congestion.py        # 车流/人流拥挤判断
│  ├─ alerts.py            # 告警判定与去重
│  ├─ heartbeat.py         # 设备离线心跳检测
│  ├─ stream_urls.py       # 流地址翻译 (ZLM 内外网重写)
│  └─ archive_scheduler.py # 小时级数据 MySQL 归档
└─ __init__.py
```

> 共享模块：`app/common`（config/logger/redis_client/device_info）、`app/prediction`（Chronos 时序预测调度）、`app/schemas`（pydantic 模型）。

---

## 2. 项目依赖

共用根 `requirements.txt`，后端相关分层：

| 层 | 依赖 |
|---|---|
| 业务后端 | `fastapi>=0.111.0`、`uvicorn[standard]>=0.29.0`、`pydantic>=2.7.0`、`pydantic-settings>=2.2.0`、`python-multipart>=0.0.9` |
| 数据存储 | `redis>=5.0.0`、`aiomysql>=0.2.0`、`cryptography>=41.0.0` |
| 通信/工具 | `httpx>=0.27.0`、`websockets>=12.0`、`python-dotenv>=1.0.0`、`loguru>=0.7.2`、`tenacity>=8.3.0`、`pyyaml>=6.0` |
| 时序预测 | `chronos-forecasting>=1.4.0`、`transformers>=4.40.0`、`accelerate>=0.30.0`（`app/prediction` 使用） |

---

## 3. 项目使用版本

| 组件 | 版本 / 说明 |
|---|---|
| Python | 3.11（`python:3.11-slim-bookworm`） |
| FastAPI | 随 `requirements.txt`（>=0.111.0） |
| Redis | 7（`redis:7-alpine`，宿主机映射 16379→6379） |
| MySQL | 8.0（`mysql:8.0`，宿主机映射 3307→3306，库名 `dt_stats`） |
| 时序预测 | Chronos-2（本地模型目录 `models/`，由 `chronos_model` 指定） |

---

## 4. 部署与配置

### 4.1 启动方式
- **容器（推荐）**：由 `docker-compose.gpu.yml` / `docker-compose.yml` 编排为 `dt-backend`；一键脚本 `bash scripts/start_all_gpu.sh`（或 `start_all.sh`）拉起。
- **本地**：`uvicorn app.backend.main:app --host 0.0.0.0 --port 8000`。

### 4.2 lifespan 启动的组件
`main.py` 的 `lifespan` 按异常隔离依次启动：
- 时序预测调度器（`app.prediction.start_scheduler`）
- 设备离线心跳检测（`core.heartbeat`）
- 警力初始配置加载（`core.allocator.seed_from_config`）
- 警力分配调度（`core.police_scheduler`）
- WVP 同步（`core.wvp_sync`，`wvp_enabled` 时）
- MySQL 归档调度（`core.archive_scheduler`，`mysql_enabled` 时）
- 设备信息表（MySQL 建表 + 种子导入 + 内存缓存，回落 JSON）

> **互斥保护**：`mock_enabled=True` 时强制关闭 `wvp_enabled`（避免真实设备流与模拟数据双重计数）。

### 4.3 关键配置（环境变量 / `.env.gpu` 或 `.env`，经 pydantic-settings 读取）

| 配置项 | 默认 | 说明 |
|---|---|---|
| `backend_port` | `8000` | 本服务端口 |
| `redis_url` | `redis://redis:6379/0` | 实时状态/计数缓存 |
| `mysql_enabled` | `False` | 归档总开关 |
| `mysql_host`/`mysql_port` | `mysql`/`3306` | MySQL 连接（服务名） |
| `mysql_user`/`mysql_password` | `root`/`root123456` | MySQL 凭据 |
| `mysql_database` | `dt_stats` | 归档库名 |
| `archive_interval_seconds` | `300` | 归档周期 |
| `wvp_enabled` | `False` | WVP 同步总开关 |
| `wvp_api_url` | `http://wvp:18080` | WVP 管理后台地址 |
| `wvp_username`/`wvp_password` | `admin`/`admin` | WVP 凭据 |
| `wvp_sync_interval` | `30` | 设备同步间隔(秒) |
| `zlm_public_base` | `""` | ZLM 对外地址前缀（重写不可达的容器名地址） |
| `zlm_internal_base` | `""` | ZLM 内部取流地址（容器内直连服务名） |
| `mediamtx_public_base` | `""` | MediaMTX 对外地址（RTSP→HLS 翻译） |
| `ai_service_url` | `http://ai:8001` | AI 服务地址（注册设备管道） |
| `business_rules_file` | `configs/business_rules.yaml` | 计数/告警/拥挤/警力/预测/异常/跟踪参数（热重载） |
| `rules_file` | `configs/rules.yaml` | 告警规则 |
| `police_config_file` | `configs/police.yaml` | 警力初始配置（区域+总警力） |
| `mock_enabled` | `False` | Mock 数据模拟（不接真实流） |

> 完整字段见 `app/common/config.py`。业务规则改后基于文件 mtime 热重载，无需重启。

### 4.4 数据存储
- **Redis**：实时计数/状态（`redis_prefix=sc`），小时级数据按 `hourly_retention_days`（默认 30）自动过期。
- **MySQL**：小时级车流/人流量长期归档（`mysql_enabled=True` 时），按 `mysql_retention_days`（默认 365）定期清理；未启用时回落 JSON，不阻塞启动。

### 4.5 对外接口（前端/第三方）
- REST：`/api/devices`、`/api/events`、`/api/stats`、`/api/alerts`、`/api/police`、`/api/config-rules`、`/api/device-info`、`/api/health`
- WebSocket：`/ws`（实时计数/事件/状态推送）
- 运维页：`static/business-rules.html`（规则热重载调试）

### 4.6 与 AI / WVP 的协作
- 经 `ai_service_url` 调 AI 服务**注册设备管道**，接收其上报的越线/区域事件。
- `wvp_enabled=True` 时经 `wvp_sync` 定时从 WVP 同步设备/通道并刷新流地址，供前端播放与 AI 拉流（`stream_urls` 做 ZLM 内外网地址翻译）。

### 4.7 挂载与网络
- bind mount：`configs/`、`static/`、`logs/` 进容器（与 AI 服务共享 `configs`）。
- 经 `wvp-shared` 网访问 WVP/ZLM；`dt_default` 与 `dt-ai` 互联。

### 4.8 健康检查
- `GET /api/health`：服务存活。
- 日志级别 `log_level`（默认 INFO，文件日志固定 DEBUG），排查时设 DEBUG。

---

相关：根 `README.md`（整体结构/部署）、`docs/OFFLINE_BUILD.md`（离线构建）、`README-ai.md`（AI 分析服务对接）。
