# 智慧古城车辆人流监管平台 — 项目整理

> 工作目录: `D:\散活\smart-transport`
> 版本基线: `main@024f2ff` (ZLM 断流修复之后)
> 备注: 原 `README-DEPLOY.txt` 是 2026-09-18 的「计数兼容修复包」现场执行说明,本文件是项目级整体梳理

---

## 1. 项目定位

**目标场景**: 古城 / 景区 / 园区级视频监控, 基于 RTSP/HTTP-FLV 视频流做:

- 车辆 / 行人实时检测、跟踪、越线计数
- 区域拥堵判定与饱和预警
- 警力需求评估与区域分配
- 时序预测(短时车流 / 人流)与提前告警
- 视频异常检测(黑屏、花屏、遮挡等)
- 大屏可视化(地图 + 实时统计 + 预测曲线 + 警力分布)

**核心数据链路**:

```
摄像头 (WVP/GB28181/ZLMediaKit) ──HTTP-FLV──> AI 推理服务 (YOLO11 + ByteTrack)
                                              │
                                              ├─ 越线 / 进入 / 离开 / 异常 事件
                                              ▼
                            业务后端 (FastAPI) ──Redis (实时状态)──> 前端 WebSocket
                                              │
                                              └─ MySQL (小时级长期归档) + Chronos 预测
```

---

## 2. 仓库结构

| 顶层目录 | 角色 |
|---|---|
| `app/`                | Python 应用代码 (后端 + AI + 预测 + 公共) |
| `configs/`            | 业务规则 / 跟踪器 / ZLM / 警力 / 异常阈值 |
| `data/`               | 设备种子 (经纬度 / 分类 / 设备信息表) |
| `models/`             | YOLO11n 权重 + Chronos 模型 |
| `scripts/`            | 启动、测试、压测、Mock 工具脚本 |
| `static/`             | 运维 HTML (业务规则 / 设备配置 / 设备信息) |
| `map-marking-system-vue/` | 大屏前端 (Vue3 + Vite + Leaflet + ECharts) |
| `simulator/`          | 下级模拟器(外部, 不在本项目编排) |
| `setting-server/`     | WVP-GB28181 平台配置(外部) |
| `setting-server-bak/` | 历史版本备份 |
| `setting-server-gpu/` | GPU 版配置 |
| `tool/`               | 辅助工具 |
| `docs/`               | 运维 troubleshooting 文档 (含 .docx) |
| `logs/`               | 运行日志 |
| `output/`             | 输出(目前为空) |
| `Dockerfile` / `Dockerfile.gpu` | 镜像构建 |
| `docker-compose.yml` / `*.mock.yml` / `*.gpu.yml` | 编排 |
| `wvp_zlm.yml`         | WVP-ZLM 集成配置 |

---

## 3. 三大子系统详解

### 3.1 AI 分析服务 `app/ai/` (FastAPI: 8001)

**入口**: `python -m app.ai.service` → `lifespan` 注册服务、初始化模型池

| 子模块 | 职责 |
|---|---|
| `service.py`           | FastAPI 入口,设备注册 / 启停 / WebSocket |
| `pipeline.py`          | 单路设备处理管线(读流 → 解码 → 推理 → 跟踪 → 越线 → 事件) |
| `stream.py`            | RTSP/HTTP-FLV 拉流 + 解码节流 (`DECODE_MAX_FPS`) |
| `tracker.py`           | BoT-SORT / ByteTrack 包装(支持 GPU / CPU) |
| `gpu_engine.py`        | GPU 批量推理引擎(每卡一个 YOLO context + 单线程亲和) |
| `model_pool.py`        | 模型实例池(auto/gpu_batch/cpu_batch/legacy 四种模式自适应) |
| `inference_scheduler.py` | 多路请求调度器 + 队列上限 + EngineBusy 丢帧策略 |
| `geometry_engine.py`   | 几何工具(线段相交、点线距离、归一化坐标) |
| `crossing_detector.py` | 越线检测核心(配合 `counter.py` 落点确认) |
| `counter.py`           | 跨线计数状态机(已含 09-18 修复: 轨迹消失落定 / 确认超时落定) |
| `track_state_manager.py` | 跟踪状态聚合(去抖/滞回/出场判定) |
| `debounce_validator.py`| 事件去抖 + ROI 内有效性校验 |
| `id_switch_detector.py`| ID switch 检测(同一目标丢失 ID 后重新分配) |
| `traj_state.py`        | 轨迹状态机 |
| `roi_detector.py`      | ROI 多边形内目标数统计 |
| `event_generator.py`   | 事件(enter/exit/anomaly)生成 + 推送 |
| `anomaly.py`           | 视频异常: 黑屏 / 花屏 / 遮挡 / 静止 等 |
| `mock_simulator.py`    | Mock 数据生成器(`MOCK_ENABLED=true` 时启用) |

**关键设计**:
- **GPU 批量化**: 32 路各自 new YOLO 会在 0 号卡堆 32 个 CUDA context; 改为每卡一个模型实例 + 批内多路 track,启动时 `model_pool.initialize()` 预热 + `_selftest` 自检。
- **推理模式**: `auto` 优先 gpu_batch,自检失败自动降到 cpu_batch;显式指定时引擎失败只回退 legacy。
- **解码节流**: 拉流全速 `read()` 后 96% 被覆盖,改 `DECODE_MAX_FPS=4~8` 节流;CPU 部署拉子码流。
- **计数可观测**: `LOG_LEVEL=DEBUG` + `COUNT_EVENT_LOG=true` 可看到逐事件明细,排查漏计。

### 3.2 业务后端 `app/backend/` (FastAPI: 8000)

**入口**: `uvicorn app.backend.main:app` → 启动 6 个调度器

| 子模块 | 职责 |
|---|---|
| `main.py`                     | FastAPI 总入口 / `lifespan` 装载所有调度器 |
| `api/stats.py`                | 实时统计 (`/api/stats`、`/api/stats/reset` ← 09-18 新增) |
| `api/events.py`               | 历史事件查询 |
| `api/alerts.py`               | 告警查询 |
| `api/devices.py`              | 设备列表 / 启停 / 取流地址翻译 |
| `api/device_info.py`          | 设备信息表(经纬度/分类/状态)查询 |
| `api/police.py`               | 警力分配查询 |
| `api/config_rules.py`         | 业务规则热重载接口 |
| `api/ws.py`                   | WebSocket 推送(事件/统计/告警/警力) |
| `core/realtime.py`            | 实时统计聚合(当前在场 / 小时累计 / 今日累计) |
| `core/congestion.py`          | 拥挤度计算(车流速度 + 区域权重) |
| `core/alerts.py`              | 告警触发与去重 |
| `core/police_scheduler.py`    | 警力调度引擎 |
| `core/allocator.py`           | 警力需求计算(当前人数权重 α + 预测 β) |
| `core/heartbeat.py`           | 设备心跳 / 离线检测 |
| `core/wvp_client.py`          | WVP-GB28181 HTTP 客户端 |
| `core/wvp_sync.py`            | WVP 设备定期同步(30s) |
| `core/stream_urls.py`         | 流地址翻译(WVP 返回 → 浏览器可播 / 容器内可拉) |
| `core/archive_scheduler.py`   | MySQL 小时级归档(已完成小时落地) |

**Lifespan 启动顺序**:
预测调度 → 心跳 → 警力种子 → 警力调度 → WVP 同步 → MySQL 归档 → 设备信息表

### 3.3 时序预测 `app/prediction/`

| 子模块 | 职责 |
|---|---|
| `chronos_model.py`     | Chronos-2 包装(本地 `models/`) |
| `repository.py`        | 历史序列读写(Redis 小时桶) |
| `router.py`            | `/api/prediction/*` REST API |
| `scheduler.py`         | 定时调度(默认 15 分钟一次) |

**预测链路**: Redis 30 个 15 分钟桶 → Chronos-2 → 未来若干分钟值 → `predict_total_warning` / `predict_total_critical` 预警。

### 3.4 公共层 `app/common/`

| 子模块 | 职责 |
|---|---|
| `config.py`        | `pydantic-settings` 统一配置(几乎所有 `.env` 字段) |
| `logger.py`        | loguru 包装(控制台 + 文件双输出) |
| `redis_client.py`  | async redis 封装 |
| `mysql_client.py`  | aiomysql 连接池 |
| `device_info.py`   | 设备信息表(MySQL 优先, JSON 回退) |
| `business_rules.py`| 业务规则热重载(`configs/business_rules.yaml` mtime 监听) |

### 3.5 数据契约 `app/schemas/`

- `events.py`: Pydantic 数据契约

---

## 4. 前端大屏 `map-marking-system-vue/`

**技术栈**: Vue 3.4 + Vite 5 + ECharts 6 + Leaflet 1.9 (无额外 UI 框架)

### 4.1 顶层组件 (`src/App.vue` + `src/components/`)

| 组件 | 功能 |
|---|---|
| `MapPanel.vue`                | 地图主体(Leaflet 瓦片 + 设备点位 + 流量动效) |
| `TopBar.vue`                  | 顶栏(标题/时间/系统状态) |
| `StatBoard.vue`               | 核心指标卡(总车流/人流量/告警数) |
| `TrafficMetricsCard.vue`      | 车流指标 |
| `FlowLegendPanel.vue`         | 流量图例面板 |
| `DailyTrendChart.vue`         | 当日趋势曲线 |
| `PredictionChart.vue`         | 预测曲线 |
| `PredictionPanel.vue`         | 预测区 |
| `PolicePanel.vue`             | 警力分布 + 调度 |
| `VideoDetailPanel.vue`        | 视频详情 |
| `CardZoomModal.vue`           | 卡片放大弹窗 |
| `LoadingOverlay.vue`          | 加载层 |
| `ToastHost.vue`               | 全局通知 |

### 4.2 工具/数据/接口

- `src/api/`: REST + WebSocket 客户端
- `src/utils/`: 通用工具
- `src/composables/`: Vue3 组合式封装
- `src/data/`: 静态数据
- `src/styles/`: 样式

### 4.3 文档

- `API_v0.11_integration.md` (前端对接)
- `OFFLINE_MAP_FIX.md` / `path-highlight-disable.md` 等
- `frontend_api.md`

### 4.4 构建产物

- `dist/`: nginx 部署目录(挂载到 `dt-frontend` 容器的 `/usr/share/nginx/html`)
- `nginx.conf`: 反代后端 API + 离线瓦片
- `public/tiles/`: 离线地图瓦片(挂载 `/var/www/tiles`)

---

## 5. 容器编排

### 5.1 默认编排 `docker-compose.yml` (CPU)

5 个服务 (`dt` 项目名,共享外部网络 `wvp-shared`):

| 服务 | 端口 | 镜像 | 说明 |
|---|---|---|---|
| `dt-ai`         | 8001 | smart-city-platform (CPU 启动命令) | AI 推理 |
| `dt-backend`    | 8000 | 同上 | 业务后端 |
| `dt-mysql`      | 3307→3306 | mysql:8.0 | 长期归档(`dt_stats`) |
| `dt-redis`      | 16379→6379 | redis:7-alpine | 实时状态(开启 AOF) |
| `dt-frontend`   | 5173→80 | nginx:1.27-alpine | 大屏前端(挂 dist + 瓦片) |

WVP / ZLM / MediaMTX 全部**外部已部署**,本项目只消费:
- backend 经 WVP REST API 同步设备
- AI 拉外部 ZLM 的 HTTP-FLV
- 通过 `ZLM_PUBLIC_BASE` / `ZLM_INTERNAL_BASE` 地址翻译解决「容器内不可达 WVP 流地址 host」问题

### 5.2 GPU 版 `docker-compose.gpu.yml`

基于 `Dockerfile.gpu` (CUDA 12.2,适配 8×4090 等),除上 5 个服务外额外挂载 `nvidia` runtime。

### 5.3 Mock 版 `docker-compose.mock.yml`

`MOCK_ENABLED=true` 时不拉流不推理,模拟器按真实车流/人流规律生成事件,验证前端/联调。

---

## 6. 配置文件矩阵 `configs/`

| 文件 | 用途 |
|---|---|
| `business_rules.yaml` | 业务规则集中(越线/告警/拥挤/警力/预测/异常/跟踪),支持热重载 |
| `rules.yaml`          | 告警规则定义(`vehicle_saturate_*` / `person_saturate_*` / `predict_*`) |
| `bytetrack.yaml`      | ByteTrack 参数(track_buffer、match_thresh 等) |
| `police.yaml`         | 警力初始分布(区域 + 总警力) |
| `mediamtx.yml`        | MediaMTX(RTSP→HLS)测试流服务 |

---

## 7. 关键运维能力

### 7.1 实时性 vs 历史性

| 数据 | 存储 | 用途 |
|---|---|---|
| 实时计数(当前在场) | Redis | 秒级 |
| 小时级桶              | Redis (30 天) | 日报/小时报表 |
| 长期归档(已完成小时) | MySQL `dt_stats` (1 年) | 长期趋势、预测训练数据 |
| 时序预测输入          | Redis 30×N 分钟桶 | Chronos-2 推理 |
| 设备信息              | MySQL `device_info` (空表时种子导入 JSON) | 地图标注 |

### 7.2 告警体系

1. **饱和告警**(`rules.yaml`):车辆 ≥ 300 / 游客 ≥ 10000 → critical
2. **预测告警**(`predict_*`):预测未来 N 分钟超阈值提前预警
3. **异常告警**(`app/ai/anomaly.py`):黑屏/花屏/遮挡(`anomaly_cooldown_seconds=60` 后端去重)
4. **警力调度**:`/api/police/*` 按需求自动分配区域警力

### 7.3 可观测性

- 文件日志固定 DEBUG(全部落 `logs/*.log`)
- 控制台日志受 `LOG_LEVEL` 控制
- `COUNT_STATS_INTERVAL` 周期输出计数运行统计(有效帧率/丢帧/推理耗时)
- `COUNT_EVENT_LOG=true` 输出每个越线事件明细
- `/health` 端点包含推理模式、引擎状态、显存占用

---

## 8. 主要技术栈与版本

| 类别 | 依赖 |
|---|---|
| AI | `ultralytics>=8.3,<9`、`opencv-python-headless`、`numpy>=1.26`、`lap>=0.5` |
| 跟踪 | ByteTrack / BoT-SORT (`configs/bytetrack.yaml`) |
| 预测 | `chronos-forecasting>=1.4`、`transformers>=4.40`、`accelerate>=0.30` |
| 后端 | `fastapi>=0.111`、`uvicorn`、`pydantic>=2.7`、`loguru`、`httpx`、`websockets` |
| 存储 | `redis>=5`、`aiomysql`、`cryptography` |
| 前端 | Vue 3.4 + Vite 5 + ECharts 6 + Leaflet 1.9 |
| 部署  | Docker(同镜像两服务)、nginx 1.27、MySQL 8.0、Redis 7 |

---

## 9. 09-18 计数兼容修复包要点(`README-DEPLOY.txt`)

触发的痛点:**现场计数偏少**。修复内容:

1. **轨迹消失落定**: 目标跨线后快速出画 / 被遮挡,直接计入(旧版永久丢弃)
2. **确认超时落定**: 跨线后 3 秒未完成确认直接计入(防低帧率漏计)
3. **新增存量清零接口**: `POST /api/stats/reset`(校准"当前在场"虚高)
4. **运维页热重载开关**: "轨迹消失落定" / "确认超时落定(秒)" 无需重启即时生效

涉及文件: `counter.py` / `track_state_manager.py` / `stats.py` / `realtime.py` / `config_rules.py` / `wvp_client.py` / `business_rules.yaml`(全部 bind mount, `docker compose restart ai backend` 即生效,无需重建镜像)。

---

## 10. 二次改造建议(待用户确认后再展开)

- **现有可优化点**: 警力调度 `core/allocator.py`、异常检测 `anomaly.py`、预测链路 `prediction/chronos_model.py` 等是相对独立的热更新候选
- **业务规则热重载**已支持 70+ 项参数,可在 `static/business-rules.html` 在线调节
- **离线/在线切换**: 通过 `.env.mock` / `.env` / `.env.local` 三层互斥完成
- **文档缺口**: 没有项目级 README,只有零散的 troubleshooting `.txt` / `.docx`,建议后续将本文件落地

如需我针对某个子系统(例如 AI、警力、预测、前端组件)再深入出方案或重构,请告诉我目标。
