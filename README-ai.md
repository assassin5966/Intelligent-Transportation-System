# AI 视觉分析服务 (app/ai)

摄像头视频流的**接入、检测、跟踪、越线计数、ROI/异常检测**与**实时事件推送**服务。后端 `dt-backend` 通过 `ai_service_url` 调用本服务注册设备管道；本服务经 WebSocket 实时推送计数结果，并按事件上报后端。

> 入口：`python -m app.ai.service`（FastAPI，`settings.ai_port`，默认 **8001**）
> 服务版本：`0.1.0`（见 `service.py` 的 `FastAPI(title="AI 分析服务", version="0.1.0")`）
> 与 `app/backend`、`app/common`、`app/prediction`、`app/schemas` 同属一个 Python 包 `app`，共用根 `requirements.txt`。

---

## 1. 项目结构

```
app/ai/
├─ service.py             # FastAPI 入口: 设备管道注册/启停(/health、/ws 注册)、lifespan 加载模型池
├─ pipeline.py            # 单设备管道 DevicePipeline: 拉流→解码→推理→计数→事件; WS 队列与广播
├─ stream.py              # 拉流与解码采样 (decode_max_fps / decode_max_short_side 节流)
├─ model_pool.py          # 模型池: 每卡/每 CPU 引擎一个批量推理实例, 设备按最少负载均摊
├─ gpu_engine.py          # GPU 批量推理引擎 (torch + YOLO, CUDA context 亲和)
├─ tracker.py             # ByteTrack 跟踪器封装 (TRACKER_CFG)
├─ inference_scheduler.py # 批量推理调度 (组批/丢帧)
├─ counter.py             # 越线计数核心 (Point、双向/单向、enter/exit 落定)
├─ crossing_detector.py   # 越线方向判定
├─ roi_detector.py        # ROI 多边形内目标统计 (供拥挤判断)
├─ debounce_validator.py  # 去抖校验 (防抖动误计)
├─ track_state_manager.py # 轨迹状态管理 (跨线落定/超时落定)
├─ traj_state.py          # 单轨迹状态机
├─ id_switch_detector.py  # ID 切换检测 (跟踪跳变保护)
├─ event_generator.py     # 越线事件生成 (上报后端)
├─ anomaly.py             # 视频异常检测 (黑屏/花屏/噪声)
├─ geometry_engine.py     # 几何运算 (点线/点面关系)
├─ mock_simulator.py      # Mock 模式: 不拉流, 按真实规律生成事件 (联调用)
└─ __init__.py
```

---

## 2. 项目依赖

共用根 `requirements.txt`，AI 相关分层：

| 层 | 依赖 |
|---|---|
| AI 视觉分析 | `ultralytics>=8.3.0,<9.0.0`（大版本变更会改跟踪器输出列序，启动自检拦截）、`opencv-python-headless>=4.9.0`、`numpy>=1.26.0`、`lap>=0.5.12` |
| 推理引擎 | `torch` / `torchvision` **不在** requirements（Dockerfile 按架构单独装：CPU 走 PyTorch 官方 CPU 源，GPU 走 cu121 源，版本 2.7.0） |
| 通信/工具 | `fastapi`、`uvicorn[standard]`、`websockets>=12.0`、`pydantic>=2.7.0`、`httpx>=0.27.0`、`loguru>=0.7.2` |
| 共享模块 | `app/common`（config/logger/redis_client）、`app/schemas`（pydantic 模型）、`app/prediction`（时序预测调度） |

---

## 3. 项目使用版本

| 组件 | 版本 / 说明 |
|---|---|
| Python | 3.11（`python:3.11-slim-bookworm`） |
| PyTorch | 2.7.0 跨平台 wheel（CPU 源 / GPU cu121 轮子） |
| CUDA | GPU 服务器 12.2（cu121 轮子向下兼容，驱动 R530+） |
| YOLO | YOLO11n（默认 `models/yolo11n.pt`，推理尺寸 `infer_imgsz=960`，CPU 建议 640） |
| 跟踪器 | ByteTrack（`track_buffer=30`） |
| FastAPI | 0.1.0（本服务） |

---

## 4. 部署与配置

### 4.1 启动方式
- **容器（推荐）**：由 `docker-compose.gpu.yml` / `docker-compose.yml` 编排为 `dt-ai`；一键脚本 `bash scripts/start_all_gpu.sh`（或 `start_all.sh`）拉起。
- **本地**：`python -m app.ai.service`（监听 `settings.ai_port`，默认 8001）。

### 4.2 关键配置（环境变量 / `.env.gpu` 或 `.env`，经 pydantic-settings 读取）

| 配置项 | 默认 | 说明 |
|---|---|---|
| `yolo_model` | `models/yolo11n.pt` | 权重路径（需预置） |
| `yolo_conf` / `yolo_iou` | `0.4` / `0.5` | 检测置信度 / NMS IoU |
| `track_buffer` | `30` | 跟踪丢失缓冲帧数 |
| `infer_mode` | `auto` | `auto`\|`gpu_batch`\|`cpu_batch`\|`legacy`（auto 失败自动降级） |
| `infer_gpu_devices` | `""` | 参与推理的卡号 `"0,1,2,3"`；空=全部可见卡 |
| `infer_slots_per_gpu` | `8` | 每卡槽位(设备数)上限；`0`=按 `infer_capacity` 自适应 |
| `infer_capacity` | `32` | 预期接入路数（自适应槽位基准） |
| `infer_imgsz` | `960` | 推理短边；原图更小则用原图（CPU 建议 640） |
| `infer_half` | `True` | FP16 半精度（仅 CUDA 生效） |
| `decode_max_fps` | `8.0` | 拉流解码节流（0=不限）；CPU 部署配合子码流降到 2~4 |
| `decode_max_short_side` | `0` | 解码端缩放短边（0=不缩放） |
| `mock_enabled` | `False` | `True` 时不拉流不推理，由 `mock_simulator` 生成事件 |
| `wvp_enabled` | `False` | 总开关；`True` 时按 WVP 同步设备自动刷新流地址 |
| `wvp_api_url` | `http://wvp:18080` | WVP 管理后台 REST 地址（容器服务名） |
| `wvp_play_protocol` | `flv` | AI 拉流协议：`flv`\|`rtsp` |
| `wvp_stream_sub` | `True` | 拉子码流降低推理压力 |
| `backend_url` | `http://backend:8000` | 事件上报/设备信息后端地址（服务名） |
| `ai_service_url` | `http://ai:8001` | 暴露给后端调用的本服务地址 |
| `redis_url` | `redis://redis:6379/0` | 共享 Redis（状态/计数缓存） |

> 完整字段见 `app/common/config.py`。业务规则（`configs/business_rules.yaml`）支持基于 mtime **热重载**，无需重启。

### 4.3 模型权重
`models/yolo11n.pt` 必须随离线 tar **预置**到 `./models/`，不联网下载；缺失时 Dockerfile 构建期显式报错（见根 `Dockerfile`/`Dockerfile.gpu` 的 `models` 校验）。

### 4.4 GPU 批量推理
- `infer_mode=auto`：CUDA 可用且自检通过 → `gpu_batch`（每卡一个引擎、8 卡真并行，32 路均摊每卡 4 路）；CUDA 不可用 → 自动转 `cpu_batch` → 失败转 `legacy`。
- 模型实例在 lifespan 阶段一次性加载/预热/自检，避免首帧建 CUDA context 卡死。
- `GET /health` 返回当前推理 `mode` 与降级 `reason`，可用于巡检。

### 4.5 与后端/前端通信
- 后端经 `ai_service_url` 调本服务**注册设备管道**（设备 ID、流地址、越线 `line`、ROI、相机类型等）。
- 本服务经 WebSocket 实时推送每路计数，并调用 `backend_url` 上报越线事件。

### 4.6 挂载与网络
- bind mount：`models/`、`configs/`、`logs/` 进容器（与 backend 共享）。
- 经 `wvp-shared` 网络按服务名访问 WVP/ZLM；`dt_default` 与 backend 互联。

### 4.7 健康检查与日志
- `GET /health`：服务与推理模式状态。
- 计数可观测：`count_stats_interval`（默认 30s）输出有效帧率/丢帧/推理耗时；`count_event_log=True` 输出每次越线明细（排查漏计时开启）；`log_level=DEBUG` 文件日志固定 DEBUG。

---

相关：根 `README.md`（整体结构/部署）、`docs/OFFLINE_BUILD.md`（离线构建）、`README-backend.md`（业务后端对接）。
