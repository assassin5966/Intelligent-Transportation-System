# 智慧古城车辆人流监管平台 - 运维文档

> 本文档基于项目实际代码与配置编写，涵盖部署、配置、监控、维护、故障处理、备份恢复、扩展与版本更新全流程。
>
> 项目仓库：`Intelligent-Transportation-System`
>
> 架构概览：GB28181 视频平台（WVP + ZLMediaKit）-> AI 分析服务（YOLO11 + BoT-SORT + 越线计数 + 视频异常识别）-> 业务后端（FastAPI + Redis + Chronos-2 时序预测 + 警力分配）-> 展示大屏（WebSocket 实时推送）

---

## 一、部署流程

### 1.1 环境要求

#### 硬件要求

| 资源 | 最低配置 | 推荐配置（生产） | 说明 |
|------|---------|----------------|------|
| CPU | 4 核 | 8 核+ | AI 推理为 CPU 密集型，核数直接影响多路并发能力 |
| GPU | 无（CPU 推理） | NVIDIA GPU 4GB+ 显存 | 启用 GPU 可显著提升 YOLO11 推理速度，需主机支持 nvidia-docker |
| 内存 | 4 GB | 8 GB+ | 每路视频流管道约占 200-400 MB，Chronos-2 模型约需 1 GB |
| 磁盘 | 20 GB | 50 GB+ SSD | 镜像约 3 GB，模型文件约 500 MB，日志按 100 MB 轮转保留 30 天 |
| 网络 | 100 Mbps | 1 Gbps+ | 视频拉流带宽消耗大，子码流约 1-2 Mbps/路 |

#### 软件要求

| 软件 | 版本要求 | 说明 |
|------|---------|------|
| Docker Engine | 20.10+（含 BuildKit） | 容器运行时 |
| Docker Compose | v2 | 服务编排 |
| 操作系统 | Linux（Ubuntu 20.04+/CentOS 8+）/ macOS / Windows 10 22H2+ | Linux 为生产推荐 |
| NVIDIA Driver | 525+（仅 GPU 场景） | 配合 nvidia-container-toolkit |
| Git | 2.20+ | 代码拉取 |

#### 端口规划表

| 端口 | 协议 | 服务 | 用途 | 映射来源 |
|------|------|------|------|---------|
| 8000 | TCP | 业务后端 (FastAPI) | REST API + WebSocket | docker-compose.yml `backend` |
| 8001 | TCP | AI 分析服务 | 视频流注册/管理/WS 推送 | docker-compose.yml `ai` |
| 16379 | TCP | Redis | 实时状态存储（映射容器 6379） | docker-compose.yml `redis` |
| 3306 | TCP | MySQL | 小时级统计长期归档 | docker-compose.yml `mysql` |
| 8554 | TCP | MediaMTX RTSP | 测试用 RTSP 服务器 | docker-compose.yml `rtsp-server` |
| 8888 | TCP | MediaMTX HTTP | 测试用 HTTP 接口 | docker-compose.yml `rtsp-server` |

> **注意**：Redis 映射端口为 16379（非默认 6379），避开宿主机已占用端口。WVP(18080/5060) 与 ZLMediaKit(80/554/1935/30000-30500) 为外部独立部署组件，端口由 WVP 侧管理，不在本项目编排内。

### 1.2 镜像构建

#### Dockerfile 说明

项目使用单一 Dockerfile 构建统一镜像（`smart-city-platform:latest`），AI 服务与业务后端共用镜像、通过不同启动命令区分：

- **基础镜像**：`python:3.11-slim`（Debian 轻量版）
- **系统依赖**：`libgl1`/`libglib2.0-0`（OpenCV 运行库）、`libgomp1`（OpenMP/PyTorch 多线程）、`ffmpeg`（RTSP 视频流解码）、`gcc`/`g++`（部分 Python 包编译）、`tzdata`（时区）
- **国内镜像加速**：apt 源切换为 `mirrors.tuna.tsinghua.edu.cn`，pip 使用 `pypi.tuna.tsinghua.edu.cn`
- **CPU/GPU 自适应**：构建时自动检测架构（`dpkg --print-architecture`），amd64 走 PyTorch 官方 CPU 源（约 200 MB），aarch64 走 PyPI 源（含 CUDA，可用于 GPU 推理）
- **YOLO 权重校验**：构建时联网下载 `models/yolo11n.pt` 并验证可加载，下载失败则构建中断（避免静默产出无权重镜像）
- **时区**：`Asia/Shanghai`
- **暴露端口**：8000（后端）、8001（AI 服务）

#### 构建命令

```bash
# 默认构建（CPU 模式，自动适配当前机器架构）
docker build -t smart-city-platform:latest .

# 指定版本标签
docker build -t smart-city-platform:0.1.0 .

# 不使用缓存重建
docker build --no-cache -t smart-city-platform:latest .

# GPU 版本构建（amd64 + CUDA 12.1，需主机支持 nvidia-docker）
# 需先编辑 Dockerfile 中 amd64 分支，将 --index-url 改为:
#   https://download.pytorch.org/whl/cu121
# 或通过 build-arg 传入:
docker build --build-arg TORCH_INDEX=https://download.pytorch.org/whl/cu121 \
             -t smart-city-platform:latest .
```

#### 跨架构支持

| 架构 | torch 来源 | 说明 |
|------|-----------|------|
| x86_64 (amd64) | `download.pytorch.org/whl/cpu` | 轻量 CPU 版（约 200 MB，无 CUDA） |
| aarch64 (arm64) | PyPI（清华镜像） | 官方 CPU 源无 aarch64 wheel，使用 PyPI 版（含 CUDA，可在 GPU 服务器推理） |

> amd64 机器如需 GPU 推理：编辑 Dockerfile 第 2 步中 amd64 分支的 `--index-url` 为 `https://download.pytorch.org/whl/cu121`，并在 `docker-compose.yml` 中取消 `ai` 服务 `deploy.resources.reservations.devices` GPU 块的注释。

#### 国内网络加速方案

Dockerfile 已内置 pip 和 apt 清华镜像加速。若 Docker Hub 基础镜像拉取慢，可预拉并重打标：

```bash
# 预拉基础镜像（通过国内镜像站加速）
docker pull docker.1ms.run/library/python:3.11-slim && \
docker tag docker.1ms.run/library/python:3.11-slim python:3.11-slim

docker pull docker.1ms.run/library/redis:7-alpine && \
docker tag docker.1ms.run/library/redis:7-alpine redis:7-alpine
```

预拉后执行 `docker compose up`，基础镜像命中本地缓存，不再连接 Docker Hub。

### 1.3 服务编排

#### docker-compose.yml 服务说明

| 服务名 | 镜像 | 启动命令 | 端口 | 用途 |
|--------|------|---------|------|------|
| `ai` | smart-city-platform:latest | `python -m app.ai.service` | 8001 | AI 分析服务（YOLO11/BoT-SORT/越线计数/异常识别） |
| `backend` | smart-city-platform:latest | `uvicorn app.backend.main:app --host 0.0.0.0 --port 8000` | 8000 | 业务后端（REST API/WebSocket/告警/预测/警力分配） |
| `redis` | redis:7-alpine | `redis-server --appendonly yes` | 16379->6379 | 实时状态存储（AOF 持久化） |
| `rtsp-server` | bluenviron/mediamtx:latest | 默认 | 8554/1935/8888 | MediaMTX RTSP 测试服务器 |
| `rtsp-streamer-vehicle` | smart-city-platform:latest | ffmpeg 推流 | - | 测试用车辆识别视频推流 |
| `rtsp-streamer-person` | smart-city-platform:latest | ffmpeg 推流 | - | 测试用人流识别视频推流 |
| `ai-processor` | smart-city-platform:latest | `python /app/tool/video_processor.py` | - | 离线视频处理器（测试用） |

> 生产环境仅需启动 `ai`、`backend`、`redis` 三个核心服务；`rtsp-*` 和 `ai-processor` 仅供测试。

#### 环境变量配置

从 `.env.example` 复制为 `.env` 并按需修改：

```bash
cp .env.example .env
```

`docker-compose.yml` 中 `backend` 服务读取以下 WVP 相关环境变量（支持 `${VAR:-default}` 语法）：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `WVP_ENABLED` | `false` | WVP 对接总开关 |
| `WVP_API_URL` | `http://wvp:18080` | WVP REST API 地址 |
| `WVP_USERNAME` | `admin` | WVP 管理账号 |
| `WVP_PASSWORD` | `admin` | WVP 管理密码 |
| `WVP_SYNC_INTERVAL` | `30` | 设备同步间隔（秒） |
| `WVP_PLAY_PROTOCOL` | `flv` | AI 拉流协议（flv/rtsp） |
| `WVP_STREAM_SUB` | `true` | 拉子码流（true=子码流，false=主码流） |

#### 卷挂载说明

| 挂载路径（宿主->容器） | 挂载的服务 | 用途 |
|----------------------|-----------|------|
| `./app:/app/app:cached` | ai, backend | 应用代码（开发热加载） |
| `./models:/app/models:cached` | ai, backend | AI 模型权重（YOLO/Chronos-2） |
| `./configs:/app/configs:cached` | ai, backend | 配置文件（rules.yaml/business_rules.yaml/bytetrack.yaml，规则与业务参数热重载） |
| `./static:/app/static:cached` | backend | 静态文件（运维工具页面） |
| `./data:/app/data:cached` | backend | 数据目录（测试视频等） |
| `./logs:/app/logs` | ai, backend | 日志输出目录 |
| `model-cache:/root/.cache` | ai | 模型缓存（HuggingFace/ultralytics） |
| `redis-data:/data` | redis | Redis 持久化数据 |

#### 启动/停止/重启命令

```bash
# 构建并启动全部核心服务
docker compose -p smartcity up -d --build

# 仅启动核心三服务（不含测试推流）
docker compose -p smartcity up -d ai backend redis

# 查看服务状态
docker compose -p smartcity ps

# 查看实时日志
docker compose -p smartcity logs -f backend
docker compose -p smartcity logs -f ai
docker compose -p smartcity logs -f redis

# 重启单个服务
docker compose -p smartcity restart backend
docker compose -p smartcity restart ai

# 停止全部服务
docker compose -p smartcity down

# 停止并清除数据卷（会删除 Redis 数据）
docker compose -p smartcity down -v
```

#### 项目名隔离说明

使用 `-p smartcity` 指定 Compose 项目名，所有容器/网络/卷均带 `smartcity` 前缀，适合在共享服务器上运行，避免与其他项目冲突。`docker-compose.yml` 未固定 `container_name`（`rtsp-server` 等测试服务除外），同一镜像可部署多套实例。

### 1.4 WVP/ZLM 外部对接

#### 前提条件

- WVP-GB28181-Pro 信令平台与 ZLMediaKit 流媒体引擎已在外部部署（上下级级联）
- WVP 管理后台可访问（默认端口 18080）
- ZLMediaKit HTTP-FLV 端口（默认 80）对 AI 服务网络可达
- 摄像头已通过 GB28181 SIP 协议注册到 WVP

WVP 信令平台与 ZLMediaKit 流媒体引擎由外部独立部署（本项目不提供编排），部署方式参考 WVP 官方文档。本项目仅作为消费方对接（见下方 `.env` 配置）。

#### .env 配置

```bash
# 启用 WVP 对接
WVP_ENABLED=true

# 必改: 指向已部署的 WVP 管理地址（不可用容器名 wvp）
WVP_API_URL=http://192.168.1.200:18080

# WVP 后台账号密码（按实际部署填写）
WVP_USERNAME=admin
WVP_PASSWORD=admin

# 设备同步间隔（秒）
WVP_SYNC_INTERVAL=30

# AI 拉流协议: flv（默认）/ rtsp
WVP_PLAY_PROTOCOL=flv

# 拉子码流降低推理压力
WVP_STREAM_SUB=true
```

#### 网络可达性验证命令

```bash
# 验证后端到 WVP REST API 的连通性
curl -s "http://192.168.1.200:18080/api/device/query/devices?page=1&count=1" | head

# 验证 AI 服务到 ZLMediaKit HTTP-FLV 的连通性
curl -s -o /dev/null -w "%{http_code}" http://192.168.1.200:80/

# 验证 SIP 端口（5060）可达
nc -zuv 192.168.1.200 5060

# 验证 RTP 端口范围（30000-30500）可达
nc -zv 192.168.1.200 30000
```

#### 运维工具页面访问

启用 WVP 后，通过浏览器访问运维工具页面：

```
http://<backend-host>:8000/static/device-config.html
```

该页面提供：
- 设备列表查看（含 synced/online/offline 状态标识）
- WVP 同步设备截帧预览
- 计数线/锚点/ROI 可视化绘制
- 设备启用配置（调用 `POST /api/devices/{id}/enable`）
- 拥挤判断阈值设置（「最大车辆数」「最大人数」输入框，对应 `max_vehicles` / `max_persons` 字段）

**业务规则配置页**（无需启用 WVP 也可访问）：

```
http://<backend-host>:8000/static/business-rules.html
```

提供计数/告警/拥挤/警力/预测/视频异常/跟踪参数的图形化编辑，保存即热重载（对应接口 `GET|PUT /api/config/business-rules`，详见下方 2.2 与 API 文档 §10）。

---

## 二、环境配置指南

### 2.1 .env 配置项详解

配置文件通过 `pydantic-settings` 从环境变量 / `.env` 文件读取（见 `app/common/config.py`）。

#### 基础配置

| 变量 | 默认值 | 说明 | 必填 | 生产建议值 |
|------|--------|------|------|-----------|
| `REDIS_URL` | `redis://redis:6379/0` | Redis 连接地址 | 是 | `redis://redis:6379/0` |
| `BACKEND_URL` | `http://backend:8000` | 业务后端地址（AI 推送事件用） | 是 | `http://backend:8000` |
| `AI_SERVICE_URL` | `http://ai:8001` | AI 服务地址（后端转发用） | 是 | `http://ai:8001` |
| `BACKEND_PORT` | `8000` | 后端服务端口 | 否 | `8000` |
| `AI_PORT` | `8001` | AI 服务端口 | 否 | `8001` |
| `REDIS_PREFIX` | `sc` | Redis key 前缀 | 否 | `sc` |

#### AI 推理配置

| 变量 | 默认值 | 说明 | 必填 | 生产建议值 |
|------|--------|------|------|-----------|
| `YOLO_MODEL` | `models/yolo11n.pt` | YOLO11 权重文件路径 | 是 | `models/yolo11n.pt`（或替换为更大模型） |
| `YOLO_CONF` | `0.4` | YOLO 检测置信度阈值 | 否 | `0.4`（低光照场景可降至 0.3） |
| `YOLO_IOU` | `0.5` | YOLO NMS IOU 阈值 | 否 | `0.5` |
| `TRACK_BUFFER` | `30` | 跟踪轨迹保留帧数 | 否 | `30`（遮挡频繁场景可增大） |

#### 时序预测配置

| 变量 | 默认值 | 说明 | 必填 | 生产建议值 |
|------|--------|------|------|-----------|
| `CHRONOS_MODEL` | `models` | Chronos-2 本地模型目录 | 是 | `models` |
| `PREDICTION_INTERVAL_MINUTES` | `15` | 预测间隔（分钟） | 否 | `15`（高峰期可缩短至 10） |
| `PREDICTION_SERIES_LENGTH` | `30` | 历史序列长度（30 个区间） | 否 | `30` |
| `VEHICLE_PERSON_MIN` | `2` | 车流转人流每车最少人数（随机采样下界） | 否 | `2` |
| `VEHICLE_PERSON_MAX` | `5` | 车流转人流每车最多人数（随机采样上界） | 否 | `5` |

> 预测逻辑：读取历史 N 个区间的人流/车流序列 -> 车流转人流（每车 `random.randint(min,max)` 人）-> 总人数 = 人流 + 转化车流 -> 喂入 Chronos-2 预测下一个 N 分钟。
>
> ⚠️ **随机性为专门设计**：真实场景中每车承载人数存在波动，确定性期望值（如 `(min+max)/2`）会低估序列方差，不利于 Chronos-2 对人流峰谷的时序预测。请勿将此处改为确定性转换。预测结果总数已向上取整为整数。

#### 车流速度与拥挤判断配置

| 变量 | 默认值 | 说明 | 必填 | 生产建议值 |
|------|--------|------|------|-----------|
| `CONGESTION_MIN_FLOW` | `5.0` | 车辆拥挤判定车流速度阈值（辆/分钟）：每分钟跨线车辆数低于此值视为车流速度过低 | 否 | `5.0`（车流量大的路口可调低，如 `3.0`） |
| `PERSON_CONGESTION_MIN_FLOW` | `10.0` | 人流拥挤判定人流速度阈值（人/分钟） | 否 | `10.0` |
| `CONGESTION_VEHICLE_WEIGHT` | `0.5` | 人车混合区域：车辆拥挤度权重 | 否 | `0.5` |
| `CONGESTION_PERSON_WEIGHT` | `0.5` | 人车混合区域：人流拥挤度权重 | 否 | `0.5` |
| `CONGESTION_THRESHOLD` | `0.5` | 人车混合区域：加权拥挤度阈值（0-1），达到即判拥挤 | 否 | `0.5` |
| `ROI_REPORT_INTERVAL` | `2.0` | AI 上报 ROI 内车辆/人员数的间隔（秒） | 否 | `2.0` |

> **拥挤判定口径（双维度加权）**：
>
> - **车辆拥挤**：`ROI 内车辆数 >= 设备.max_vehicles` 且 `车流速度 < CONGESTION_MIN_FLOW`。
> - **人流拥挤**：`ROI 内人数 >= 设备.max_persons` 且 `人流速度 < PERSON_CONGESTION_MIN_FLOW`。
> - **人车混合**（两个阈值都配置）：各维度拥挤度（0-1）= `0.5×数量饱和度 + 0.5×速度因子`，综合拥挤度 = 按 `CONGESTION_VEHICLE_WEIGHT / CONGESTION_PERSON_WEIGHT` 加权，`综合拥挤度 >= CONGESTION_THRESHOLD` 判定拥挤。
>
> **车流速度口径**：AI 在 60 秒滑动窗口内统计跨线事件次数，折算为每分钟量（辆/分钟、人/分钟），非真实车速（km/h）——不需要像素↔米标定，阈值全局通用。
>
> **设备维度**：拥挤判断按设备开启（运维页面「最大车辆数」「最大人数」输入框，或启流 API `max_vehicles` / `max_persons` 字段），均未配置阈值的设备不做拥挤判定，但仍上报数据供查询。

#### 告警配置

| 变量 | 默认值 | 说明 | 必填 | 生产建议值 |
|------|--------|------|------|-----------|
| `RULES_FILE` | `configs/rules.yaml` | 告警规则文件路径 | 是 | `configs/rules.yaml` |

#### 警力分配配置

| 变量 | 默认值 | 说明 | 必填 | 生产建议值 |
|------|--------|------|------|-----------|
| `POLICE_DEMAND_WEIGHT_CURRENT` | `0.3` | 需求计算：当前人数权重 α | 否 | `0.3` |
| `POLICE_DEMAND_WEIGHT_PREDICT` | `0.7` | 需求计算：预测人数权重 β | 否 | `0.7` |
| `POLICE_MOVEMENT_RATIO` | `0.5` | 每轮最大移动比例（占总警力） | 否 | `0.5` |
| `POLICE_MIN_PER_REGION` | `1` | 每区域最少警力数 | 否 | `1` |

> 需求计算公式：`demand = α × 当前人数 + β × 预测人数`，α + β 建议保持为 1.0。

#### 视频异常配置

| 变量 | 默认值 | 说明 | 必填 | 生产建议值 |
|------|--------|------|------|-----------|
| `ANOMALY_CHECK_INTERVAL` | `30` | 每 N 帧检测一次（约 1s@30fps） | 否 | `30` |
| `ANOMALY_CONFIRM_FRAMES` | `2` | 连续确认帧数（去抖） | 否 | `2` |
| `ANOMALY_COOLDOWN_SECONDS` | `60` | 同设备同异常告警冷却（秒） | 否 | `60` |
| `ANOMALY_ANALYSIS_WIDTH` | `480` | 分析帧宽度（等比缩放） | 否 | `480` |
| `BLACK_SCREEN_BRIGHTNESS` | `20` | 灰度均值低于此值判定黑屏（条件 1） | 否 | `20` |
| `BLACK_SCREEN_RATIO` | `0.95` | 近黑像素占比高于此值判定黑屏（条件 2） | 否 | `0.95` |
| `BLACK_PIXEL_VALUE` | `20` | 近黑像素亮度上限 | 否 | `20` |
| `FLOWER_BLOCK_GRID` | `8` | 花屏分析块网格（NxN） | 否 | `8` |
| `FLOWER_NOISE_STD` | `35.0` | 块均标准差高于此值判定花屏（高噪） | 否 | `35.0` |
| `FLOWER_UNIFORMITY` | `0.6` | 噪声均匀度 1-CV 高于此值判定花屏 | 否 | `0.6` |
| `FLOWER_CHANNEL_CORR` | `0.5` | 通道相关性低于此值判定花屏（去相关） | 否 | `0.5` |
| `FLOWER_TEMPORAL_DIFF` | `25.0` | 时域差分高于此值判定花屏（有前帧时） | 否 | `25.0` |

> 黑屏判定为双条件 AND（灰度均值 < brightness AND 近黑像素占比 > ratio），避免夜间低光误报。
> 花屏判定为多信号复合 AND（空间噪声高 AND 噪声均匀 AND（通道去相关 OR 时域高噪）），单信号易误报。

#### WVP 对接配置

| 变量 | 默认值 | 说明 | 必填 | 生产建议值 |
|------|--------|------|------|-----------|
| `WVP_ENABLED` | `false` | WVP 对接总开关 | 否 | `true`（启用 GB28181 时） |
| `WVP_API_URL` | `http://wvp:18080` | WVP REST API 地址 | 是 | `http://<实际IP>:18080` |
| `WVP_USERNAME` | `admin` | WVP 管理账号 | 是 | 按实际填写 |
| `WVP_PASSWORD` | `admin` | WVP 管理密码 | 是 | 按实际填写 |
| `WVP_SYNC_INTERVAL` | `30` | 设备同步间隔（秒） | 否 | `30` |
| `WVP_PLAY_PROTOCOL` | `flv` | AI 拉流协议（flv/rtsp） | 否 | `flv` |
| `WVP_STREAM_SUB` | `true` | 拉子码流（true=子码流） | 否 | `true` |

#### 安全配置

| 变量 | 默认值 | 说明 | 必填 | 生产建议值 |
|------|--------|------|------|-----------|
| `CORS_ORIGINS` | `*` | 允许的跨域来源（逗号分隔） | 否 | `https://screen.example.com`（限定具体域名） |

### 2.2 告警规则配置

#### rules.yaml 实时规则 + 预测规则

告警规则文件 `configs/rules.yaml` 包含两类规则：

**实时规则**（`rules`）：评估当前实时指标（当前车辆数/当前人员数），超阈值触发告警。

```yaml
rules:
  - id: vehicle_saturate_warning
    category: vehicle_saturate
    metric: current_vehicles       # 评估的指标字段
    threshold: 200                  # 触发阈值
    level: warning                  # 告警级别
    message: "车辆数量接近饱和 ({value}/{threshold})"

  - id: vehicle_saturate_critical
    category: vehicle_saturate
    metric: current_vehicles
    threshold: 300
    level: critical
    message: "车辆饱和红色告警 ({value}/{threshold})"

  - id: person_saturate_warning
    category: person_saturate
    metric: current_persons
    threshold: 8000
    level: warning
    message: "游客数量接近饱和 ({value}/{threshold})"

  - id: person_saturate_critical
    category: person_saturate
    metric: current_persons
    threshold: 10000
    level: critical
    message: "人流饱和 ({value}/{threshold})"
```

**预测规则**（`predict_rules`）：评估 Chronos-2 预测的总人数，超阈值时提前预警。

```yaml
predict_rules:
  - id: predict_total_warning
    category: predict_saturate
    metric: predicted_total
    threshold: 500
    level: warning
    message: "预计 {predict_minutes} 分钟后总人数接近饱和 ({value}/{threshold})"

  - id: predict_total_critical
    category: predict_saturate
    metric: predicted_total
    threshold: 1000
    level: critical
    message: "预计 {predict_minutes} 分钟后总人数将饱和 ({value}/{threshold})"
```

#### 热重载机制

告警规则通过文件 mtime 检测实现热重载（见 `app/backend/core/alerts.py`）：

- `load_rules()` 和 `load_predict_rules()` 在每次调用时检查文件 `st_mtime`
- 文件修改时间变化时自动重新加载，无需重启服务
- 加载后缓存到内存，未变化时直接复用缓存

> 修改 `rules.yaml` 后保存即可，下一次告警评估周期自动生效。

#### 业务规则集中配置（business_rules.yaml，热重载）

计数、拥挤、警力、预测、视频异常、跟踪参数可集中在 `configs/business_rules.yaml`（见 `app/common/business_rules.py`）：

```yaml
counting:                  # 越线计数参数 (counter.py)
  min_distance_ratio: 0.02
  hold_frames: 3
  hysteresis_ratio: 0.04
  ...
congestion:                # 拥挤判断参数
  congestion_min_flow: 5.0
  person_congestion_min_flow: 10.0
  congestion_vehicle_weight: 0.5
  congestion_person_weight: 0.5
  congestion_threshold: 0.5
  roi_report_interval: 2.0
police:                    # 警力分配算法参数
  demand_weight_current: 0.3
  demand_weight_predict: 0.7
  movement_ratio: 0.5
prediction:                # 时序预测参数
  interval_minutes: 15
  series_length: 30
  vehicle_person_min: 2
  vehicle_person_max: 5
anomaly:                   # 视频异常检测参数
  black_screen_brightness: 20
  check_interval: 30
  ...
tracking:                  # 目标跟踪参数
  yolo_conf: 0.4
  yolo_iou: 0.5
```

- 与 rules.yaml 同一热重载机制（mtime 检测），修改保存后自动生效，**无需重启** backend/AI
- 优先级：`business_rules.yaml` 覆盖 config.py 默认值；未配置项回落环境变量/config.py
- 配置文件中未列出的分组项保持 config.py 默认值，可按需增删
- 调度周期类参数（`prediction.interval_minutes`）修改后从下一轮循环起按新周期执行

##### 图形化配置页面（推荐）

无需直接编辑 YAML，浏览器访问运维配置页：

```
http://<backend-host>:8000/static/business-rules.html
```

- 按分组展示全部参数（label/说明/默认值），支持单个恢复默认与一键全部恢复
- 点击「保存并热重载」调用 `PUT /api/config/business-rules` 写回 yaml（保留注释），**立即生效无需重启**
- 后端地址可在页面顶部修改（跨域由 CORS 控制）；已集成设备计数配置页入口

对应接口：
- `GET /api/config/business-rules`：返回参数值 + 元数据（label/desc/type/default），供页面渲染
- `PUT /api/config/business-rules`：接收 `{分组: {参数: 值}}`，类型/范围校验（非法值返回 400），按行写回 yaml 保留注释与顺序

#### 自定义规则示例

新增一条车辆预警规则（阈值 150，warning 级别）：

```yaml
rules:
  # ... 已有规则 ...

  - id: vehicle_early_warning
    category: vehicle_saturate
    metric: current_vehicles
    threshold: 150
    level: warning
    message: "车辆数量预警 ({value}/{threshold})，请关注"
```

规则字段说明：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 规则唯一标识（用于去重 key） |
| `category` | string | 告警分类 |
| `metric` | string | 评估指标（`current_vehicles`/`current_persons`/`predicted_total`） |
| `threshold` | number | 触发阈值（指标值 >= 阈值时触发） |
| `level` | string | 告警级别（`warning`/`critical`/`info`） |
| `message` | string | 告警消息模板（支持 `{value}`/`{threshold}`/`{predict_minutes}` 占位符） |

### 2.3 跟踪器配置

#### bytetrack.yaml 参数说明

跟踪器配置文件 `configs/bytetrack.yaml` 使用 BoT-SORT 算法（见 `app/ai/tracker.py`）：

```yaml
tracker_type: botsort          # 跟踪器类型（botsort）
track_high_thresh: 0.35        # 高置信度检测阈值（只信任高置信度检测建立轨迹）
track_low_thresh: 0.1          # 低置信度检测阈值（低于此值丢弃）
new_track_thresh: 0.5          # 新建轨迹阈值（更高=更难创建新轨迹，减少 ID 分裂）
track_buffer: 60               # 轨迹保留帧数（遮挡时保留更久，2 秒@30fps）
match_thresh: 0.4              # IOU 匹配阈值（更低=更严格匹配，减少漏匹配）
fuse_score: True               # 是否融合检测分数到跟踪

# GMC 全局运动补偿
gmc_method: none               # 固定摄像头无需运动补偿，设为 none 节省计算

# ReID 外观匹配
with_reid: False               # 关闭（需额外模型文件）
model: none
proximity_thresh: 0.5          # 近邻阈值
appearance_thresh: 0.8         # 外观匹配阈值
```

#### 调参建议

| 场景 | 建议调整 | 说明 |
|------|---------|------|
| ID 频繁切换 | `new_track_thresh` 提高（0.5->0.6），`match_thresh` 降低（0.4->0.3） | 更难创建新轨迹 + 更严格匹配 |
| 遮挡严重 | `track_buffer` 增大（60->90） | 遮挡时保留轨迹更久 |
| 误检多 | `track_high_thresh` 提高（0.35->0.45） | 只信任更高置信度检测 |
| 漏检多 | `track_low_thresh` 降低（0.1->0.05），`YOLO_CONF` 降低 | 降低检测门槛 |
| 移动摄像头 | `gmc_method` 改为 `sparseOptFlow` | 启用全局运动补偿 |
| 需要外观匹配 | `with_reid: True`，`model: 指定 ReID 模型路径` | 需额外下载模型文件 |

> 修改 `bytetrack.yaml` 后需重启 AI 服务才能生效（跟踪算法参数，非热重载）。
> 检测置信度/IOU 阈值（`yolo_conf`/`yolo_iou`）与历史长度（`track_buffer`）可通过 `business_rules.yaml` 的 `tracking` 分组热重载，无需重启。

---

## 业务全流程：WVP 设备从拉流到计数

```
WVP (GB28181) ──→ 后端定时同步 ──→ Redis 设备表 ──→ 用户配置计数线 ──→ AI 管道 ──→ 事件 ──→ Redis 统计
                      │                                               │
                      └── play/start 拿流地址 ──────────────────────────┘
```

### 阶段一：WVP 定时同步

**入口**：`app/backend/core/wvp_sync.py` → `sync_once()`，每 `wvp_sync_interval` 秒执行一次：

| 步骤 | 操作 | 对应代码 |
|------|------|---------|
| 1 | WVP 登录，取 token（遇 401 自动重登） | `wvp_client.py:login()` |
| 2 | 拉取全量在线设备 `GET /api/device/query/devices` | `wvp_client.py:list_devices()` |
| 3 | 每台设备拉取通道列表 `GET /api/device/query/devices/{id}/channels` | `wvp_client.py:list_channels()` |
| 4 | 与本地 Redis 设备表比对（按 `gb_device_id + gb_channel_id` 作为唯一键） | `wvp_sync.py:sync_once()` |

**比对结果处理**：

| 情况 | 操作 |
|------|------|
| WVP 新通道（已在 `device_info` 注册）| 入表 `status=synced`，**不启流**（等待用户画线配置） |
| WVP 新通道（未在 `device_info` 注册）| 跳过，不登记、不拉流、不计数 |
| 已入表但 `device_info` 不再注册 | 停 AI 管道并从设备表移除 |
| WVP 已离线 | 停 AI 管道，标 `status=offline` |
| WVP 恢复在线 | `play/start` 拿流地址 → 启 AI 管道 |
| 在线但 AI 侧没跑（WVP/ZLM 重启后） | 重新 `play/start` → 重新启流 |

> `device_info` 表有数据即启用"仅注册设备"过滤：WVP 通道名去空格后必须与 `device_info.name` 一致才参与拉流/计数（见 `device_info.is_registered()`）。

### 阶段二：用户配置启流

**入口**：`device-config.html` → `POST /api/devices/{id}/enable`

1. 前端截帧 `GET /api/devices/{id}/snapshot`（非 WVP 设备直接拉 RTSP 截帧）
2. 在画面上点击红线两端 + 锚点，形成计数线（可选 ROI 多边形）
3. 选择类型（车辆/人流）和方向（单向/双向），可填写「最大车辆数」开启拥挤判断
4. 提交 → `POST /api/devices/{id}/enable` 携带 `{line_coords, anchor_coords, camera_type, count_only, roi_coords, max_vehicles}`
5. 后端保存配置到 Redis → 获取流地址 → 转发到 AI 服务

**流地址获取选择逻辑**（`devices.py:enable_device()`）：

```python
is_wvp = bool(settings.wvp_enabled and gb_dev and gb_ch)
```

| 条件 | 流地址来源 | 适用场景 |
|------|-----------|---------|
| `WVP_ENABLED=true` + 有 `gb_device_id` + 有 `gb_channel_id` | WVP `play/start` 接口 | 真实 WVP 同步的摄像头 |
| 以上任一不满足 | 直接使用设备注册时填的 `stream_url` | Mock 设备、手动注册设备 |

### 阶段三：AI 管道处理循环

**入口**：`pipeline.py:DevicePipeline._run()`

```
stream_frames() ──→ ByteTracker.track() ──→ LineCrossingCounter.process_tracks() ──→ 事件推送
      │                                      │
      │                                      └── 每帧：检测跟踪 → 越线判定 → 去重 → 事件
      │
      └── 断流自动重连（5 次退避），WVP 设备回调后端刷新流地址
```

### 阶段四：事件落库

**入口**：`realtime.py:apply_event()`，Redis 管道一次完成：

```
越线事件 → Redis：
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

## Mock 模式启动

```bash
# 构建并启动（首次或镜像有变更）
bash scripts/mock_start.sh --build

# 快速启动（已有镜像，代码挂载 volume 无需 rebuild）
bash scripts/mock_start.sh

# 停止所有服务
bash scripts/mock_start.sh --down
```

> Mock 模式用 `data/test_50f.mp4` 作为测试视频，经 `rtsp-streamer-vehicle` / `rtsp-streamer-person` 两路 ffmpeg 分别推流到 MediaMTX 的 `/vehicle`、`/person` 路径（推流命令见 `docker-compose.mock.yml`）。测试视频文件需放在 `data/` 根目录且文件名保持一致（常见坑：`data/video/text.mp4` 不存在会导致推流容器启动即退出、RTSP 路径 404）。如需更换测试视频，直接替换该文件即可。

### 服务地址

| 服务 | 地址 |
|------|------|
| 后端 API | `http://localhost:8000` |
| 健康检查 | `http://localhost:8000/health` |
| 设备列表 | `http://localhost:8000/api/devices` |
| 播放地址（前端实时流） | `http://localhost:8000/api/devices/{id}/play` |
| 实时统计 | `http://localhost:8000/api/stats/realtime` |
| 设备统计 | `http://localhost:8000/api/stats/devices` |
| 事件列表 / 告警 | `http://localhost:8000/api/events` / `api/alerts` |
| 运维工具（截图/画线/预览） | `http://localhost:8000/static/device-config.html` |
| WebSocket | `ws://localhost:8000/ws` |
| AI 服务 | `http://localhost:8001` |
| RTSP 推流 | `rtsp://localhost:8554/vehicle` / `rtsp://localhost:8554/person` |
| MediaMTX HLS 播放 | `http://localhost:8888/{path}/index.m3u8` |

### 常用运维

```bash
# 全接口测试 (可带 IP 参数)
bash scripts/interface_test.sh
bash scripts/interface_test.sh 192.168.1.41

# 日志
docker compose logs -f backend
docker compose logs -f ai
docker compose logs -f rtsp-streamer-vehicle

# 代码变更后重启
docker compose restart backend ai
```

### 端口清单与冲突说明

| 端口 | 归属 | 说明 |
|------|------|------|
| 8000 / 8001 | 后端 / AI | 业务端口 |
| 16379 | Redis | 避让宿主 6379 |
| 8554 / 8888 | MediaMTX(`rtsp-server`) | RTSP 收流 + HLS 播放 |
| 18080 / 5060 / 80 / 554 | WVP / ZLMediaKit | 信令/管理/FLV/RTSP |
| 3307 / 6380 | WVP 专用 MySQL / Redis | 避让宿主 3306/6379 |
| 30000-30500 | ZLMediaKit RTP | GB28181 收流 |

> **1935 端口已从 rtsp-server 移除映射**：此前与 WVP 项目的 zlmediakit(1935) 冲突，mock 推流走 8554 RTSP 不需要 RTMP。若需 MediaMTX RTMP，请改用非 1935 端口。

---

## 前端实时播放：GET /api/devices/{id}/play

前端大屏/运维工具播放实时画面统一走此接口（详情见 `docs/api.md` 3.6 前端播放地址）：

- **WVP 设备** → `play/start` 取 FLV（配置 `ZLM_PUBLIC_BASE` 可重写为浏览器可达前缀）
- **RTSP 设备（MediaMTX）** → 翻译为 `http://{宿主}:8888/{path}/index.m3u8`（HLS，`MEDIAMTX_PUBLIC_BASE` 可覆盖，空则按请求 Host 自动推导）
- **http(s) 直配流** → 原样返回

```json
// 响应
{ "device_id": "mock-vehicle-01", "play_url": "http://172.16.168.9:8888/vehicle/index.m3u8",
  "protocol": "hls", "source": "mediamtx", "source_stream_url": "rtsp://rtsp-server:8554/vehicle" }
```

前端按 `protocol` 选播放器：`flv`→flv.js，`hls`→hls.js（Safari 原生）。**验证命令**：

```bash
curl -s http://localhost:8000/api/devices/{id}/play          # 取地址
curl -s -L http://localhost:8888/vehicle/index.m3u8 | head   # 带 cookie 跟随 LL-HLS 302, 应见 #EXTM3U
```

**相关配置**（`.env`）：`ZLM_PUBLIC_BASE=`（空=原样）、`MEDIAMTX_PUBLIC_BASE=`（空=自动推导）。MediaMTX 的 HLS 默认关闭，需挂载 `configs/mediamtx.yml`（`hls: yes`）并映射 8888。

---

## ROI 感兴趣区域

### 作用

ROI 是一个多边形区域，限制只有**中心点落在该多边形内**的轨迹才参与越线计数。ROI 外的物体即使穿过计数线也不会触发事件。适用于：排除画面边缘干扰、聚焦特定车道/出入口、降低无效检测的算力消耗。

### 格式

归一化坐标字符串 `"x1,y1,x2,y2,x3,y3,..."`，坐标值 0~1，至少 3 个顶点（6 个值）。示例——矩形 ROI：

```
"0.1,0.1,0.9,0.1,0.9,0.9,0.1,0.9"
```

### ROI 与计数线的交互

ROI 启用时，计数器将**计数线自动裁剪到 ROI 多边形内**，只保留 ROI 内线段作为有效计数段；跨线点投影落在 ROI 外线段不触发计数。

```
         ┌───────────────┐
         │   ROI 区域     │
         │   ──●────●──  │  ← 有效计数线段（裁剪后）
         │       ↑锚点    │
         └───────────────┘
         ←── 无效段（不计数）
```

### 设置方式

- **Web 工具**：`device-config.html` 已支持 ROI 绘制（「ROI 模式」按钮 → 点画面加顶点 → 右键/双击完成）
- **API**：注册 `POST /api/devices` 或启流 `POST /api/devices/{id}/enable` 时传 `roi_coords`；不传则全画面计数

### 拥挤判断（ROI 内车辆数）

ROI 同时是**拥挤判断的区域数量统计范围**（见「拥挤告警」小节）：AI 每 2 秒统计一次中心点落在 ROI 内的车辆（car/truck/bus）数与人员（person）数并上报，后端结合每分钟车/人流量按双维度加权判定拥挤。未配置 ROI 时统计全画面（向后兼容）。设置方式：运维页面填写「最大车辆数」（`max_vehicles`）开启车辆拥挤、「最大人数」（`max_persons`）开启人流拥挤。

---

## 三、监控告警机制

### 3.1 系统健康检查

#### /health 端点

| 服务 | 端点 | 返回示例 | 说明 |
|------|------|---------|------|
| 业务后端 | `GET http://<host>:8000/health` | `{"status": "ok", "service": "backend"}` | 后端存活检查 |
| AI 服务 | `GET http://<host>:8001/health` | `{"status": "ok", "service": "ai", "active_devices": 3}` | AI 存活 + 活跃设备数 |
| 预测服务 | `GET http://<host>:8000/api/prediction/health` | `{"status": "ok", "service": "prediction", "degraded": false, ...}` | 预测服务 + 降级状态 |

健康检查命令：

```bash
# 后端健康检查
curl -s http://localhost:8000/health | python -m json.tool

# AI 服务健康检查（含活跃设备数）
curl -s http://localhost:8001/health | python -m json.tool

# 预测服务健康检查（含降级状态）
curl -s http://localhost:8000/api/prediction/health | python -m json.tool
```

#### AI 服务 active_devices 监控

AI 服务的 `/health` 端点返回 `active_devices` 字段，表示当前正在运行的视频处理管道数量。监控建议：

- `active_devices` 突然降为 0：所有管道停止（可能 AI 服务重启或全部断流）
- `active_devices` 持续低于注册设备数：部分设备断流未恢复
- 结合 `GET /devices`（AI 服务）可查看每路管道的运行状态

```bash
# 查看 AI 服务所有设备管道状态
curl -s http://localhost:8001/devices | python -m json.tool
```

#### Redis 连接监控

Redis 连接池配置（`app/common/redis_client.py`）：`max_connections=32`，`decode_responses=True`。

```bash
# 检查 Redis 连通性
docker compose -p smartcity exec redis redis-cli ping
# 预期返回: PONG

# 查看 Redis 连接信息
docker compose -p smartcity exec redis redis-cli info clients
# 关注 connected_clients、blocked_clients

# 查看 Redis 内存使用
docker compose -p smartcity exec redis redis-cli info memory
# 关注 used_memory_human、used_memory_peak_human
```

### 3.2 业务告警

#### 车辆/人流饱和告警

基于实时指标评估（见 `app/backend/core/alerts.py` 的 `evaluate()`）：

| 规则 ID | 指标 | 阈值 | 级别 | 消息 |
|---------|------|------|------|------|
| `vehicle_saturate_warning` | `current_vehicles` | 200 | warning | 车辆数量接近饱和 |
| `vehicle_saturate_critical` | `current_vehicles` | 300 | critical | 车辆饱和红色告警 |
| `person_saturate_warning` | `current_persons` | 8000 | warning | 游客数量接近饱和 |
| `person_saturate_critical` | `current_persons` | 10000 | critical | 人流饱和 |

触发条件：指标值 >= 阈值。每次 AI 推送事件后触发评估（`BackgroundTasks` 异步执行）。

#### 预测告警（提前预警）

基于 Chronos-2 预测结果评估（见 `evaluate_prediction()`）：

| 规则 ID | 指标 | 阈值 | 级别 | 消息 |
|---------|------|------|------|------|
| `predict_total_warning` | `predicted_total` | 500 | warning | 预计 N 分钟后总人数接近饱和 |
| `predict_total_critical` | `predicted_total` | 1000 | critical | 预计 N 分钟后总人数将饱和 |

每 15 分钟（`PREDICTION_INTERVAL_MINUTES`）预测调度器执行一次，预测完成后自动评估预测告警。

#### 视频异常告警（黑屏/花屏）

AI 管道内置 `AnomalyMonitor`（见 `app/ai/anomaly.py`），周期采样 + 连续确认去抖 + 状态机：

| 异常类型 | 判定条件 | 上报级别 |
|---------|---------|---------|
| `black_screen`（黑屏） | 灰度均值 < 20 AND 近黑像素占比 > 0.95（双条件 AND） | onset -> critical，recovery -> info |
| `flower_screen`（花屏） | 空间噪声 > 35 AND 噪声均匀度 > 0.6 AND（通道相关性 < 0.5 OR 时域差分 > 25） | onset -> critical，recovery -> info |

仅在状态转移时产出事件（normal -> 异常 = onset，异常 -> normal = recovery），避免持续告警刷屏。

#### 设备离线告警（心跳超时 90s）

离线检测机制（见 `app/backend/core/heartbeat.py`）：

- AI 管道每 30 秒向后端发送心跳（`POST /api/devices/{id}/heartbeat`）
- 后端每 30 秒检查所有设备心跳
- 超过 90 秒（3 个心跳周期）无心跳 -> 标记 `offline` + 触发 `critical` 告警
- 恢复心跳 -> 自动标记 `online`

> 未收到过心跳的新注册设备不判定离线（避免误报）。

#### 拥挤告警（双维度加权，事件驱动）

AI 周期上报 + 后端双维度加权判定（见 `app/backend/core/congestion.py`）：

| 阶段 | 触发条件 | 级别 | 消息示例 |
|------|---------|------|---------|
| `onset`（进入拥挤） | 车辆拥挤 / 人流拥挤 / 人车混合加权综合判定成立（见上方「拥挤判定口径」） | critical | `设备 xxx 拥堵: 区域车辆 12/10 且车流速度 3.0 辆/分钟 (低于 5.0)` / `设备 xxx 人流拥堵: 区域人数 220/200 且人流速度 3.0 人/分钟 (低于 10.0)` / `设备 xxx 人车混合拥堵: 综合拥挤度 0.62 (车辆 12/10 人流 220/200)` |
| `recovery`（解除拥挤） | 不再满足判定条件 | info | `设备 xxx 拥堵解除: 综合拥挤度 0.30` |

- **车流/人流速度** = AI 最近 60 秒跨线事件次数折算为每分钟量（`app/ai/pipeline.py` 滑动窗口）
- **区域车辆数/人数** = AI 每 2 秒（`ROI_REPORT_INTERVAL`）统计的 ROI 内瞬时车辆数/人数（`app/ai/counter.py:count_roi_vehicles` / `count_roi_persons`）
- **状态机去抖**：Redis 记录拥挤状态（`sc:congestion:state:{device_id}`），仅状态转移时产生告警，避免刷屏
- 告警经 `GET /api/alerts` 查询、后端 `/ws`（`type: alert`）推送，前端据此在设备卡片展示「拥挤/已解除」及维度（车辆/人流/混合）
- 需设备配置 `max_vehicles > 0`（车辆）和/或 `max_persons > 0`（人流）（运维页面「最大车辆数」「最大人数」）才启用

#### 告警去重机制

| 告警类型 | 去重 key | 去重时长 | 机制 |
|---------|---------|---------|------|
| 实时规则告警 | `sc:alert:{rule_id}` | 300 秒（5 分钟） | Redis `SET NX EX 300` |
| 预测规则告警 | `sc:alert:{rule_id}` | 预测间隔 × 60 秒 | 一个预测周期内同一规则只触发一次 |
| 设备离线告警 | `sc:alert:device_offline:{device_id}` | 300 秒（5 分钟） | Redis `SET NX EX 300` |
| 视频异常告警 | `sc:alert:anomaly:{device_id}:{type}:{phase}` | 60 秒（`ANOMALY_COOLDOWN_SECONDS`） | Redis `SET NX EX 60` |
| 拥挤告警 | `sc:congestion:state:{device_id}` | 状态机（仅状态转移触发） | 双阈值判定 + onset/recovery 转移 |

所有告警持久化到 Redis List `sc:alerts`，保留最近 1000 条（`LTRIM 0 999`），通过 `GET /api/alerts?limit=100` 查询。

#### 越线事件历史（审计回溯）

AI 推送的每一条越线事件（`VehicleEnter/Exit`、`PersonEnter/Exit`）在更新实时统计的同时，落库到 Redis List `sc:events`（见 `app/backend/core/realtime.py` 的 `apply_event`）：

- 写入方式：`LPUSH` 头插 + `LTRIM 0 1999`，保留最近 2000 条，超出自动淘汰最早记录
- 与实时统计独立：实时统计（`sc:realtime:current`）和今日累计（`sc:realtime:daily:*`）存于独立 Hash，事件历史淘汰不影响计数
- 查询接口：`GET /api/events?limit=N`（N 范围 1~2000，默认 100，按时间倒序）

```bash
# 查看最近 20 条越线事件（REST 接口）
curl -s "http://localhost:8000/api/events?limit=20" | python -m json.tool

# 直接读取 Redis List（调试/运维）
docker compose -p smartcity exec redis redis-cli lrange sc:events 0 19

# 统计事件历史总量（上限 2000）
docker compose -p smartcity exec redis redis-cli llen sc:events
```

> 事件历史供前端大屏「最近事件」滚动列表与运营审计回溯使用。若需更长保留周期，可调整 `app/backend/core/realtime.py` 中 `_EVENT_MAX` 常量并重启 backend。

### 3.3 日志监控

#### loguru 日志格式和级别

日志系统基于 loguru（见 `app/common/logger.py`）：

- **控制台输出**（stderr）：`INFO` 级别，格式含时间、级别、模块、函数、行号
- **文件输出**：`logs/{YYYY-MM-DD}.log`，`DEBUG` 级别，100 MB 轮转，保留 30 天，UTF-8 编码
- **异步写入**：`enqueue=True` 保证线程安全

日志格式示例：
```
2026-08-08 14:30:00.123 | WARNING  | app.backend.core.alerts:evaluate:103 - [告警] [critical] 车辆饱和红色告警 (305/300)
```

#### Docker 日志查看命令

```bash
# 查看后端实时日志
docker compose -p smartcity logs -f backend

# 查看 AI 服务实时日志
docker compose -p smartcity logs -f ai

# 查看 Redis 日志
docker compose -p smartcity logs -f redis

# 查看最近 100 行日志
docker compose -p smartcity logs --tail 100 backend

# 查看指定时间段日志
docker compose -p smartcity logs --since "2026-08-08T10:00:00" --until "2026-08-08T12:00:00" backend

# 过滤告警日志
docker compose -p smartcity logs backend 2>&1 | grep "\[告警\]"
docker compose -p smartcity logs backend 2>&1 | grep "\[离线\]"
docker compose -p smartcity logs backend 2>&1 | grep "\[WVP同步\]"
```

#### 日志卷挂载

日志通过 `./logs:/app/logs` 卷挂载到宿主机，可直接查看：

```bash
# 查看当日日志文件
ls -la logs/

# 搜索告警关键字
grep "\[告警\]" logs/$(date +%Y-%m-%d).log

# 搜索错误日志
grep "ERROR" logs/$(date +%Y-%m-%d).log

# 统计各级别日志数量
awk -F'|' '{print $2}' logs/$(date +%Y-%m-%d).log | sort | uniq -c | sort -rn
```

### 3.4 监控指标建议

#### Prometheus 可抓取指标

本平台未内置 Prometheus exporter，建议通过以下方式采集：

| 指标类别 | 采集方式 | 指标示例 |
|---------|---------|---------|
| API 延迟 | 黑盒探测 `/health` 端点 | `http_request_duration_seconds{service="backend"}` |
| Redis 连接数 | Redis exporter | `redis_connected_clients`、`redis_used_memory_bytes` |
| AI 推理耗时 | 解析日志或自定义埋点 | `ai_inference_duration_seconds{device_id="..."}` |
| 设备在线率 | 轮询 `GET /api/devices` 统计 status | `device_online_total` / `device_registered_total` |
| 活跃管道数 | 轮询 `GET http://ai:8001/health` | `ai_active_devices` |
| 告警计数 | 轮询 `GET /api/alerts` | `alerts_total{level="critical"}` |
| 预测降级状态 | 轮询 `GET /api/prediction/health` | `prediction_degraded` (0/1) |

#### Grafana 大屏建议指标

| 面板 | 数据源 | 展示内容 |
|------|--------|---------|
| 实时车辆/人员数 | `GET /api/stats/realtime` | 当前车辆数、当前人员数（时序图） |
| 今日累计进出 | `GET /api/stats/realtime` | 车辆/人员进出累计（柱状图） |
| 设备在线率 | `GET /api/devices` | 在线/离线/同步中设备占比（饼图） |
| 告警趋势 | `GET /api/alerts` | 按级别/分类的告警数量趋势 |
| AI 推理延迟 | 日志解析 | 各设备推理耗时（折线图） |
| Redis 内存 | Redis INFO | 内存使用量、连接数（仪表盘） |
| 预测准确度 | `GET /api/prediction/latest` | 预测值 vs 实际值对比 |

---

## 四、日常维护操作

### 4.1 设备管理

#### WVP 同步（手动/自动）

**自动同步**：`WVP_ENABLED=true` 时，后端每 30 秒（`WVP_SYNC_INTERVAL`）自动执行一次同步（见 `app/backend/core/wvp_sync.py`）。

同步逻辑：
1. 拉取 WVP 全量在线设备/通道
2. 与本地 Redis 设备表按 `gb_device_id + gb_channel_id` 比对
3. 新通道：仅 `device_info` 表注册设备入表（status=synced），未注册跳过；不启流（缺计数线）
4. 已入表但 `device_info` 不再注册：停 AI 管道并移除
5. 已配置 + WVP 在线 + AI 管道未运行：重新启流
6. WVP 离线：停 AI 管道，标记 offline
7. WVP 恢复：重新启流

**手动同步**：

```bash
# 手动触发一次 WVP 设备同步
curl -X POST http://localhost:8000/api/devices/sync | python -m json.tool

# 返回示例: {"added": 2, "started": 0, "stopped": 1, "recovered": 0}
```

#### 设备启用配置计数线（运维工具页面）

WVP 同步入表的设备（status=synced）需配置计数线后才能启流。通过运维工具页面或 API 操作：

```bash
# 方式 1: 运维工具页面（推荐）
# 浏览器访问 http://<host>:8000/static/device-config.html
# 选择设备 -> 截帧预览 -> 绘制计数线/锚点/ROI -> 点击启用

# 方式 2: API 调用
curl -X POST http://localhost:8000/api/devices/GB-3402000000-34020000001320000001/enable \
  -H "Content-Type: application/json" \
  -d '{
    "line_coords": "0.5,0.1,0.5,0.9",
    "anchor_coords": "0.6,0.5",
    "count_only": null,
    "camera_type": "vehicle",
    "roi_coords": "0.1,0.1,0.9,0.1,0.9,0.9,0.1,0.9",
    "max_vehicles": 10,
    "max_persons": 200
  }'
```

参数说明：
- `line_coords`：计数线两端点 `x1,y1,x2,y2`（归一化 0-1，必填）
- `anchor_coords`：内侧锚点 `x,y`（归一化 0-1，可选，决定 Enter/Exit 方向）
- `count_only`：`null`=双向计数，`enter`=只计进入，`exit`=只计离开（Pydantic `Literal` 校验，仅接受小写枚举值，`Enter`/`in`/`both` 等会返回 422）
- `camera_type`：`null`=全部检测，`vehicle`=只检测机动车，`person`=只检测人流（同样 `Literal` 校验）
- `roi_coords`：ROI 多边形顶点 `x1,y1,x2,y2,...`（归一化 0-1，至少 3 顶点，可选）
- `max_vehicles`：车辆拥挤判断阈值（ROI 内最大车辆数，`>0` 时开启车辆拥挤判断，可选；运维页面「最大车辆数」输入框对应此字段）
- `max_persons`：人流拥挤判断阈值（ROI 内最大人数，`>0` 时开启人流拥挤判断；与 `max_vehicles` 同时配置则人车加权综合，可选；运维页面「最大人数」输入框对应此字段）

> 📌 **单向车道 `count_only` 配置要点**：车流单向车道应设 `count_only=enter`（只产生 `VehicleEnter`），此时 `current_vehicles` 为累计进入数（无 `Exit` 对冲，为单向场景预期语义），勿误判为「只增不减异常」。人流摄像头恒为双向计数（`count_only=null`），`Enter`/`Exit` 自然对冲 `current_persons`。该参数仅过滤事件生成，不影响事件对实时统计的累加逻辑。

#### 设备状态查看

```bash
# 查看所有注册设备
curl -s http://localhost:8000/api/devices | python -m json.tool

# 查看 AI 服务运行中的管道
curl -s http://localhost:8001/devices | python -m json.tool

# 获取设备流地址（WVP 设备）
curl -s http://localhost:8000/api/devices/GB-xxx-xxx/stream | python -m json.tool

# 截取设备当前画面（WVP 设备）
curl -s http://localhost:8000/api/devices/GB-xxx-xxx/snapshot -o snapshot.jpg

# 查看最近越线事件历史（审计/排障）
curl -s "http://localhost:8000/api/events?limit=50" | python -m json.tool
```

设备状态值说明：

| 状态 | 说明 |
|------|------|
| `registered` | 手动注册（未对接 WVP） |
| `synced` | WVP 同步入表，待配置计数线 |
| `online` | 在线运行中 |
| `offline` | 离线（心跳超时或 WVP 侧离线） |

#### 设备删除

```bash
# 删除设备（同时停止 AI 管道）
curl -X DELETE http://localhost:8000/api/devices/GB-xxx-xxx
```

> 删除设备会同时转发到 AI 服务停止对应管道，并从 Redis 删除设备配置。

### 4.2 告警规则维护

#### 修改 rules.yaml（热重载）

```bash
# 编辑告警规则文件
vi configs/rules.yaml

# 修改后保存即可，下一次评估周期自动生效（基于 mtime 检测）
# 验证规则已加载（查看后端日志）
docker compose -p smartcity logs --tail 20 backend 2>&1 | grep "已加载"
# 预期日志: 已加载 4 条告警规则
```

#### 查看历史告警（GET /api/alerts）

```bash
# 查看最近 100 条告警
curl -s "http://localhost:8000/api/alerts?limit=100" | python -m json.tool

# 查看最近 10 条告警
curl -s "http://localhost:8000/api/alerts?limit=10" | python -m json.tool

# 过滤 critical 级别告警
curl -s "http://localhost:8000/api/alerts?limit=100" | \
  python -c "import sys,json; [print(a) for a in json.load(sys.stdin) if a.get('level')=='critical']"
```

> 告警存储在 Redis List `sc:alerts`，保留最近 1000 条，按时间倒序。

### 4.3 警力分配维护

#### 初始配置（configs/police.yaml）

区域与总警力可通过 **配置文件** `configs/police.yaml` 预置，后端启动时自动写入 Redis：

```yaml
total: 50                                    # 总警力数
regions:
  - id: region_01                            # 区域 ID（唯一）
    name: "古城南门"                          # 区域名称
    longitude: 113.302310                    # 实际中心经度（前端地图标注用）
    latitude: 40.084888                      # 实际中心纬度
    center_x: 0.5                            # 中心坐标（归一化 0-1，调度计算用）
    center_y: 0.5
    device_id: "GB-xxx-xxx"                  # 关联摄像头（读取在场人数）
```

> - 项目内置默认 4 区域（东-和阳门/南-永泰门/西-清远门/北-武定门），中心坐标与经纬度按设备信息表经纬度（即 `data/device_geo.json` 数据）城墙方位聚类生成。
> - **摄像头就近归区**：分配计算时，每个摄像头（名称匹配 `device_info` 表有经纬度者）自动归属**距离最近的区域中心**，区域在場人数 = 归属该区域的摄像头人数之和。无需为每个区域手动绑定 `device_id`；某区域无归属摄像头且显式绑定的设备有数据时，以绑定设备兜底。
> - 加载逻辑：仅当 Redis 中**尚无对应数据**时写入（HSETNX/SETNX），**不覆盖** API 动态配置。改文件后重启后端生效，但已存在的区域/总警力以 Redis 为准。
> - 若需全部重新加载，先删掉 Redis 中对应键（`sc:police:regions` / `sc:police:total`）再重启后端。
> - `GET /api/police/regions` 同时返回 `longitude`/`latitude`（实际经纬度）与 `center_x`/`center_y`（归一化坐标）。

#### 区域注册/删除（API 动态配置）

```bash
# 注册警力区域
curl -X POST http://localhost:8000/api/police/regions \
  -H "Content-Type: application/json" \
  -d '{
    "id": "region_01",
    "name": "古城南门",
    "center_x": 100.5,
    "center_y": 200.3,
    "device_id": "GB-xxx-xxx"
  }'

# 查看所有区域
curl -s http://localhost:8000/api/police/regions | python -m json.tool

# 删除区域
curl -X DELETE http://localhost:8000/api/police/regions/region_01
```

#### 总警力设置

```bash
# 设置总警力数
curl -X POST http://localhost:8000/api/police/total \
  -H "Content-Type: application/json" \
  -d '{"total": 50}'
```

#### 手动触发优化

```bash
# 手动触发一次警力分配优化
curl -X POST http://localhost:8000/api/police/optimize | python -m json.tool
```

分配算法三阶段（见 `app/backend/core/allocator.py`）：
1. **需求计算**：`demand = alpha * 当前人数 + beta * 预测人数`（alpha=0.3, beta=0.7）
2. **目标分配**：比例分配 + 最小保障（每区域至少 1 人）+ 取整修正
3. **调度规划**：贪心最近优先，移动上限 = `总警力 * 0.5`

#### 查看分配方案

```bash
# 查看最近一次自动分配方案
curl -s http://localhost:8000/api/police/plan | python -m json.tool

# 查看当前分配状态
curl -s http://localhost:8000/api/police/allocation | python -m json.tool
```

> 警力分配调度器每 15 分钟（与预测间隔同步）自动执行一次，首次执行在启动后等待一个间隔。方案缓存到 Redis `sc:police:plan:latest`。

### 4.4 预测服务维护

#### 健康检查（degraded 状态）

```bash
# 预测服务健康检查
curl -s http://localhost:8000/api/prediction/health | python -m json.tool
```

返回示例：
```json
{
  "status": "ok",
  "service": "prediction",
  "degraded": false,
  "interval_minutes": 15,
  "series_length": 30,
  "vehicle_person_range": [2, 5]
}
```

`degraded` 字段说明：
- `false`：Chronos-2 模型正常加载，使用大模型预测
- `true`：模型加载失败或推理异常，降级为线性趋势外推（`_predict_naive`）

降级原因排查：
- `models/` 目录下缺少 Chronos-2 模型文件（`config.json` 等）
- `chronos-forecasting` / `transformers` / `accelerate` 依赖缺失
- GPU 显存不足（模型加载到 CUDA 失败）

#### 手动触发预测

```bash
# 手动触发一次总人数预测
curl -X POST http://localhost:8000/api/prediction/predict | python -m json.tool

# 查看缓存的最近一次预测结果
curl -s http://localhost:8000/api/prediction/latest | python -m json.tool
```

> 预测超时阈值为 60 秒（`_PREDICT_TIMEOUT`），超时返回 HTTP 504。

#### 模型目录检查

```bash
# 检查模型目录结构
ls -la models/

# 预期内容:
# - yolo11n.pt          (YOLO11 权重)
# - config.json         (Chronos-2 配置)
# - 其他 Chronos-2 模型文件

# 检查 YOLO 权重可加载
docker compose -p smartcity exec ai python -c "from ultralytics import YOLO; m=YOLO('models/yolo11n.pt'); print('YOLO OK')"

# 检查 Chronos 模型可加载
docker compose -p smartcity exec backend python -c "
from app.prediction.chronos_model import ChronosPredictor
p = ChronosPredictor.instance()
print(p.predict([10,20,30,40,50], 1))
print('degraded:', p.is_degraded)
"
```

### 4.5 Redis 维护

#### 连接检查

```bash
# 进入 Redis CLI
docker compose -p smartcity exec redis redis-cli

# 测试连通性
127.0.0.1:6379> ping
PONG

# 查看服务器信息
127.0.0.1:6379> info server

# 查看客户端连接
127.0.0.1:6379> info clients
```

#### 数据查看常用命令

```bash
# 查看所有设备
redis-cli hgetall sc:device:GB-xxx-xxx

# 扫描所有设备 key
redis-cli --scan --pattern "sc:device:*"

# 查看当前实时统计
redis-cli hgetall sc:realtime:current

# 查看今日累计
redis-cli hgetall sc:realtime:daily:$(date +%Y%m%d)

# 查看最近告警（前 10 条）
redis-cli lrange sc:alerts 0 9

# 查看警力区域
redis-cli hgetall sc:police:regions

# 查看总警力
redis-cli get sc:police:total

# 查看最新预测结果
redis-cli get sc:prediction:latest:total

# 查看最新警力方案
redis-cli get sc:police:plan:latest

# 查看设备拥挤上报数据（最近一次 ROI 车辆数/人数 + 每分钟车/人流量）
redis-cli hgetall sc:congestion:device:GB-xxx-xxx

# 查看设备拥挤状态（1=拥挤, 0=正常）
redis-cli get sc:congestion:state:GB-xxx-xxx

# 查询拥挤数据 REST 接口（拥挤字段已随设备统计返回: roi_vehicles / roi_persons / vehicle_flow_per_min / person_flow_per_min / vehicle_congested / person_congested / congestion_score / congested）
curl -s "http://localhost:8000/api/stats/devices" | python -m json.tool

# 查看告警去重 key（TTL）
redis-cli ttl sc:alert:vehicle_saturate_critical
```

#### 内存使用监控

```bash
# 查看内存使用
redis-cli info memory

# 关注指标:
# used_memory_human      - 当前内存使用
# used_memory_peak_human - 历史峰值
# maxmemory_human        - 最大内存限制（无限制则为 0B）
# mem_fragmentation_ratio - 内存碎片率（建议 < 1.5）

# 查看各类型 key 数量
redis-cli dbsize

# 分析大 key
redis-cli --bigkeys
```

#### 持久化（AOF）

Redis 启动时已配置 `--appendonly yes`（见 `docker-compose.yml`），AOF 持久化自动开启：

```bash
# 查看 AOF 状态
redis-cli info persistence
# 关注 aof_enabled: 1

# AOF 文件位置: redis-data 卷的 /data/appendonly.aof

# 手动触发 AOF 重写（压缩 AOF 文件）
redis-cli bgrewriteaof

# 查看 AOF 文件大小
docker compose -p smartcity exec redis ls -lh /data/appendonly.aof
```

---

## 五、故障处理预案

| 故障现象 | 可能原因 | 排查步骤 | 修复方案 |
|---------|---------|---------|---------|
| AI 服务不可达 | AI 容器未启动/崩溃；网络不通 | 1. `docker compose -p smartcity ps` 查看容器状态<br>2. `docker compose -p smartcity logs ai` 查看错误日志<br>3. `curl http://localhost:8001/health` 测试连通性 | 1. 重启 AI 服务：`docker compose -p smartcity restart ai`<br>2. 若 OOM 则增加内存限制<br>3. 检查 `models/yolo11n.pt` 是否存在 |
| backend 无法连接 Redis | Redis 容器未启动；`REDIS_URL` 配置错误；网络隔离 | 1. `docker compose -p smartcity ps redis` 确认运行<br>2. `docker compose -p smartcity exec redis redis-cli ping`<br>3. 检查 `REDIS_URL` 是否为 `redis://redis:6379/0`<br>4. 查看后端日志中 Redis 连接错误 | 1. 重启 Redis：`docker compose -p smartcity restart redis`<br>2. 修正 `.env` 中的 `REDIS_URL`<br>3. 确认 ai/backend/redis 在同一 Compose 网络 |
| WVP 同步失败 | WVP 服务不可达；账号密码错误；WVP API 返回异常 | 1. `curl http://<WVP_API_URL>/api/device/query/devices?page=1&count=1` 测试连通<br>2. 检查 `.env` 中 `WVP_ENABLED`/`WVP_API_URL`/`WVP_USERNAME`/`WVP_PASSWORD`<br>3. `docker compose -p smartcity logs backend 2>&1 \| grep "WVP"` 查看同步日志<br>4. 检查 WVP token 是否过期（日志中有 "token 失效" 记录） | 1. 修正 WVP 连接参数<br>2. 确认 WVP 服务正常运行<br>3. 重启 backend 使配置生效<br>4. 手动触发同步验证：`POST /api/devices/sync` |
| 截帧失败/超时 | 设备离线；ZLM 流未就绪；网络延迟 | 1. 检查设备状态是否 online<br>2. `GET /api/devices/{id}/stream` 验证流地址可获取<br>3. 检查 ZLMediaKit 是否正常运行<br>4. 查看后端日志中截帧相关错误 | 1. 确认设备在线后重试<br>2. 检查 ZLM 端口 80 是否可达<br>3. 调整截帧超时（代码中为 15 秒）<br>4. WVP 重新点播：`GET /api/devices/{id}/stream` |
| 设备状态 offline | AI 心跳停止；AI 管道崩溃；网络中断 | 1. `curl http://localhost:8001/devices` 查看 AI 管道状态<br>2. `docker compose -p smartcity logs ai 2>&1 \| grep "管道"`<br>3. 检查 `sc:device:{id}` 的 `last_heartbeat` 时间<br>4. 确认 AI -> backend 网络（端口 8000）可达 | 1. 重启 AI 服务<br>2. 若 WVP 设备，同步会自动恢复启流<br>3. 手动注册设备重新启流<br>4. 检查 `BACKEND_URL` 配置 |
| 预测服务 degraded | Chronos-2 模型文件缺失；依赖未安装；GPU 显存不足 | 1. `GET /api/prediction/health` 确认 `degraded: true`<br>2. 检查 `models/` 目录是否有 `config.json`<br>3. `docker compose -p smartcity logs backend 2>&1 \| grep "Chronos"`<br>4. 检查 `chronos-forecasting` 是否安装 | 1. 补全 `models/` 目录下 Chronos-2 模型文件<br>2. 重建镜像确保依赖完整<br>3. degraded 状态下仍可用（降级为线性外推），但精度降低<br>4. 重启 backend 重新加载模型 |
| WebSocket 断连 | 后端重启；网络中断；前端连接超时 | 1. `curl http://localhost:8000/health` 确认后端存活<br>2. 检查浏览器控制台 WS 错误<br>3. 确认端口 8000 可达<br>4. 查看后端日志 WS 连接记录 | 1. 后端正常运行则前端自动重连<br>2. 检查反向代理/防火墙的 WS 超时配置<br>3. 确认 `CORS_ORIGINS` 配置正确 |
| Docker 容器 OOM | 内存不足；多路视频并发过多；模型加载内存大 | 1. `docker stats` 查看容器内存使用<br>2. `docker inspect <container> \| grep OOMKilled` 确认 OOM<br>3. `docker compose -p smartcity logs ai 2>&1 \| grep -i "killed"`<br>4. 检查并发管道数量 | 1. 在 `docker-compose.yml` 中添加 `mem_limit`<br>2. 减少并发视频路数<br>3. 使用更小的 YOLO 模型（yolo11n）<br>4. 增加服务器内存<br>5. 启用 GPU 减轻 CPU 内存压力 |
| GPU 显存不足 | 多路并发推理；模型过大；显存碎片 | 1. `nvidia-smi` 查看 GPU 显存使用<br>2. 检查并发管道数量<br>3. `docker compose -p smartcity logs ai 2>&1 \| grep "CUDA out of memory"` | 1. 减少并发视频路数<br>2. 使用子码流（`WVP_STREAM_SUB=true`）<br>3. 降低视频分辨率（`ANOMALY_ANALYSIS_WIDTH`）<br>4. 增大 GPU 显存或使用多 GPU 分片 |

---

## 六、数据备份与恢复策略

### 6.1 Redis 数据备份

#### AOF 持久化机制

Redis 启动时已配置 `--appendonly yes`，AOF（Append Only File）持久化自动开启：

- 每次写操作追加到 `/data/appendonly.aof`
- AOF 文件存储在 `redis-data` Docker 卷中
- 容器重启后自动从 AOF 恢复数据

```bash
# 查看 AOF 文件
docker compose -p smartcity exec redis ls -lh /data/appendonly.aof

# 查看 AOF 持久化状态
docker compose -p smartcity exec redis redis-cli info persistence | grep aof
```

#### RDB 手动快照

```bash
# 手动触发 RDB 快照（同步，会阻塞）
docker compose -p smartcity exec redis redis-cli save

# 后台触发 RDB 快照（异步，不阻塞）
docker compose -p smartcity exec redis redis-cli bgsave

# 查看最后一次 RDB 保存时间
docker compose -p smartcity exec redis redis-cli lastsave

# RDB 文件位置
docker compose -p smartcity exec redis ls -lh /data/dump.rdb
```

#### 备份恢复命令

```bash
# === 备份 ===

# 备份 AOF 文件到宿主机
docker compose -p smartcity cp redis:/data/appendonly.aof ./backup/appendonly-$(date +%Y%m%d).aof

# 备份 RDB 文件到宿主机
docker compose -p smartcity cp redis:/data/dump.rdb ./backup/dump-$(date +%Y%m%d).rdb

# 备份全部 Redis 数据目录
docker run --rm -v smartcity_redis-data:/data -v $(pwd)/backup:/backup alpine \
  cp -r /data /backup/redis-data-$(date +%Y%m%d)

# === 恢复 ===

# 1. 停止 Redis
docker compose -p smartcity stop redis

# 2. 恢复 AOF 文件
docker compose -p smartcity cp ./backup/appendonly-20260808.aof redis:/data/appendonly.aof

# 3. 启动 Redis
docker compose -p smartcity start redis

# 4. 验证数据
docker compose -p smartcity exec redis redis-cli dbsize
```

#### 关键数据（设备配置/告警/统计）

| 数据类型 | Redis Key | 说明 | TTL |
|---------|-----------|------|-----|
| 设备配置 | `sc:device:{device_id}` | Hash：设备信息/计数线/ROI/状态 | 无 |
| 当前统计 | `sc:realtime:current` | Hash：当前车辆/人员数 | 无 |
| 今日累计 | `sc:realtime:daily:{YYYYMMDD}` | Hash：当日进出累计 | 90 天 |
| N 分钟区间 | `sc:realtime:interval:{YYYYMMDDHHMM}` | Hash：区间计数（供预测） | (序列长度+10)*间隔*60 秒 |
| 逐设备人数 | `sc:realtime:device:{device_id}` | Hash：设备在场人数 | 24 小时 |
| 拥挤上报数据 | `sc:congestion:device:{device_id}` | Hash：最近一次 ROI 车辆数/人数 + 每分钟车/人流量 + 上报时间 | 7200 秒 |
| 拥挤状态 | `sc:congestion:state:{device_id}` | String：拥挤状态（`1`=拥挤，`0`=正常） | 86400 秒 |
| 告警列表 | `sc:alerts` | List：最近 1000 条告警 | 无（LTRIM 保留 1000 条） |
| 越线事件历史 | `sc:events` | List：最近 2000 条越线事件（`VehicleEnter/Exit`、`PersonEnter/Exit`） | 无（LTRIM 保留 2000 条） |
| 告警序号 | `sc:alert:seq` | String：告警自增 ID | 无 |
| 警力区域 | `sc:police:regions` | Hash：区域配置 | 无 |
| 总警力 | `sc:police:total` | String：总警力数 | 无 |
| 警力方案 | `sc:police:plan:latest` | String：最新分配方案 JSON | 间隔*60*2 秒 |
| 预测结果 | `sc:prediction:latest:total` | String：最新预测 JSON | 间隔*60*4 秒 |
| 活跃设备集 | `sc:devices:active` | Set：活跃设备 ID | 无 |

### 6.2 模型文件备份

| 模型 | 路径 | 说明 | 备份命令 |
|------|------|------|---------|
| YOLO11 权重 | `models/yolo11n.pt` | YOLO11 nano 模型（约 5 MB） | `cp models/yolo11n.pt backup/` |
| Chronos-2 模型 | `models/` 目录 | Chronos-2-Small（28M 参数），含 `config.json` 等 | `tar czf backup/chronos-model-$(date +%Y%m%d).tar.gz models/` |

```bash
# 备份全部模型文件
mkdir -p backup
tar czf backup/models-$(date +%Y%m%d).tar.gz models/

# 恢复模型文件
tar xzf backup/models-20260808.tar.gz
```

> 模型文件被 `.gitignore` 排除，不入版本控制，务必定期备份。Dockerfile 构建时会联网下载 `yolo11n.pt`，但 Chronos-2 模型需手动放置。

### 6.3 配置文件备份

| 配置文件 | 路径 | 说明 |
|---------|------|------|
| 环境变量 | `.env` | 环境配置（含 WVP 账号密码，注意安全） |
| 告警规则 | `configs/rules.yaml` | 告警阈值配置（热重载） |
| 业务规则 | `configs/business_rules.yaml` | 计数/拥挤/警力/预测/异常/跟踪参数（热重载） |
| 跟踪器配置 | `configs/bytetrack.yaml` | BoT-SORT 算法参数（改后需重启 AI） |
| 警力配置 | `configs/police.yaml` | 警力区域 + 总警力初始配置 |
| 主编排 | `docker-compose.yml` | 服务编排 |
| Dockerfile | `Dockerfile` | 镜像构建 |

> WVP 信令平台与 ZLMediaKit 为外部独立部署组件，其配置由 WVP 侧自行备份，不在本项目备份清单内。

```bash
# 备份全部配置文件
mkdir -p backup/configs
cp .env backup/configs/.env.bak
cp configs/rules.yaml backup/configs/
cp configs/business_rules.yaml backup/configs/
cp configs/bytetrack.yaml backup/configs/
cp configs/police.yaml backup/configs/
cp docker-compose.yml backup/configs/
cp Dockerfile backup/configs/

# 一键备份（配置 + 模型 + Redis 数据）
tar czf backup/full-backup-$(date +%Y%m%d).tar.gz \
  .env configs/ docker-compose.yml Dockerfile \
  models/ backup/redis-data-$(date +%Y%m%d)/
```

---

## 七、系统扩展方案

### 7.1 横向扩展

#### 多 backend 实例（需共享 Redis）

业务后端为无状态服务（状态存 Redis），可部署多实例负载均衡：

```yaml
# docker-compose.yml 扩展示例
services:
  backend-2:
    image: smart-city-platform:latest
    command: ["uvicorn", "app.backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
    ports:
      - "8002:8000"
    environment:
      - REDIS_URL=redis://redis:6379/0
      - AI_SERVICE_URL=http://ai:8001
      # ... 同 backend ...
    depends_on:
      - redis
```

注意事项：
- 所有实例必须共享同一个 Redis（`REDIS_URL` 一致）
- 后台调度器（预测/离线检测/警力分配/WVP 同步）会在每个实例中各自运行，需避免重复执行
- 建议通过 Nginx/HAProxy 做负载均衡，WebSocket 需启用 sticky session

#### 多 AI 实例（设备分片）

AI 服务为有状态（内存中维护管道），需按设备分片：

```yaml
# 按设备分片部署多个 AI 实例
services:
  ai-1:
    image: smart-city-platform:latest
    command: ["python", "-m", "app.ai.service"]
    ports:
      - "8001:8001"
    environment:
      - REDIS_URL=redis://redis:6379/0
      - BACKEND_URL=http://backend:8000

  ai-2:
    image: smart-city-platform:latest
    command: ["python", "-m", "app.ai.service"]
    ports:
      - "8011:8001"
    environment:
      - REDIS_URL=redis://redis:6379/0
      - BACKEND_URL=http://backend:8000
```

分片策略：
- 不同区域的设备分配到不同 AI 实例
- 后端 `AI_SERVICE_URL` 需改为可路由到对应 AI 实例的网关
- 每路视频管道独立运行，互不影响

#### Redis 集群

当单节点 Redis 无法满足吞吐或内存需求时，可升级为 Redis 集群：

- 修改 `REDIS_URL` 为集群连接串
- 注意 `redis.asyncio` 对集群模式的支持
- 告警去重使用 `SET NX` 需确保 key 不跨槽
- `SCAN` 命令在集群模式下行为不同（需逐节点扫描）

### 7.2 纵向扩展

#### GPU 推理加速（Dockerfile GPU 配置）

启用 GPU 推理（见 Dockerfile 和 docker-compose.yml 注释）：

1. 编辑 Dockerfile，将 amd64 分支的 torch 源改为 CUDA 版：
   ```
   --index-url https://download.pytorch.org/whl/cu121
   ```
2. 在 `docker-compose.yml` 中取消 `ai` 服务 GPU 块的注释：
   ```yaml
   deploy:
     resources:
       reservations:
         devices:
           - driver: nvidia
             count: all
             capabilities: [gpu]
   ```
3. 重建镜像并重启 AI 服务

#### 多路并发优化

| 优化项 | 方法 | 说明 |
|--------|------|------|
| 降低分辨率 | 使用子码流（`WVP_STREAM_SUB=true`） | 子码流约 1-2 Mbps，主码流 4-8 Mbps |
| 调整帧跳过 | 代码中 `frame_skip` 参数 | 跳帧处理降低推理频率 |
| 使用轻量模型 | `models/yolo11n.pt`（nano） | 比 yolo11s/m/l 快 2-5 倍 |
| 限制 ROI | 配置 `roi_coords` 过滤画面外目标 | 减少无效检测和跟踪计算 |
| 调整异常检测间隔 | `ANOMALY_CHECK_INTERVAL` 增大 | 降低异常检测频率 |

#### 视频分辨率调整

- 通过 WVP 拉子码流（`WVP_STREAM_SUB=true`）自动使用低分辨率流
- 异常检测分析宽度通过 `ANOMALY_ANALYSIS_WIDTH`（默认 480）控制
- 越线计数使用归一化坐标（0-1），分辨率变化时自动重算像素坐标

### 7.3 功能扩展

#### 新增告警规则

在 `configs/rules.yaml` 中添加规则（热重载，无需重启）：

```yaml
rules:
  - id: custom_vehicle_warning
    category: custom
    metric: current_vehicles
    threshold: 100
    level: warning
    message: "自定义车辆预警 ({value}/{threshold})"
```

支持的指标字段（来自 `get_stats()` 返回）：
- `current_vehicles` - 当前车辆数
- `current_persons` - 当前人员数
- `today_vehicle_in` - 今日车辆进入
- `today_vehicle_out` - 今日车辆离开
- `today_person_in` - 今日人员进入
- `today_person_out` - 今日人员离开
- `active_devices` - 活跃设备数

#### 新增摄像头类型

通过 `camera_type` 参数控制检测类型：
- `null` - 全部检测（机动车 + 行人 + 非机动车）
- `vehicle` - 只检测机动车
- `person` - 只检测人流（含非机动车）

注册设备时指定：
```bash
curl -X POST http://localhost:8000/api/devices \
  -H "Content-Type: application/json" \
  -d '{
    "id": "CAM-CUSTOM-01",
    "name": "自定义摄像头",
    "stream_url": "rtsp://...",
    "line_coords": "0.5,0.1,0.5,0.9",
    "camera_type": "vehicle"
  }'
```

#### 接入更多 AI 模型

1. 将新模型权重放入 `models/` 目录
2. 在 `app/ai/` 下实现检测/分析逻辑
3. 在 `DevicePipeline` 中集成新的处理步骤
4. 如需通过环境变量配置，在 `app/common/config.py` 的 `Settings` 类中添加配置项
5. 重建镜像：`docker build -t smart-city-platform:latest .`

---

## 八、版本更新流程

### 8.1 代码更新

#### 标准更新流程

```bash
# 1. 拉取最新代码
git pull origin main

# 2. 重建镜像
docker build -t smart-city-platform:latest .

# 3. 重新构建并启动服务
docker compose -p smartcity up -d --build
```

#### 滚动更新（先 backend 后 ai）

```bash
# 1. 先更新后端（不影响 AI 管道运行）
docker compose -p smartcity up -d --build backend
# 确认后端健康
curl -s http://localhost:8000/health

# 2. 再更新 AI 服务（会短暂中断视频分析）
docker compose -p smartcity up -d --build ai
# 确认 AI 服务健康
curl -s http://localhost:8001/health
```

> AI 服务重启后，WVP 同步会在下一个周期（30 秒内）自动恢复所有已配置设备的视频管道。手动注册的设备需重新调用启流（`POST /api/devices/{id}/enable`）恢复管道。

#### 回滚方案

```bash
# 1. 回退代码到上一个版本
git log --oneline -5          # 查看历史
git checkout <previous_commit>

# 2. 用旧代码重建镜像
docker build -t smart-city-platform:latest .

# 3. 重启服务
docker compose -p smartcity up -d --build

# 如果保留了旧镜像标签，可直接回退:
# docker compose -p smartcity up -d  # 使用已有镜像
```

> 建议：每次更新前给当前镜像打版本标签，便于快速回滚：
> `docker tag smart-city-platform:latest smart-city-platform:$(date +%Y%m%d)`

### 8.2 配置更新

#### .env 修改 -> 重启对应服务

```bash
# 修改 .env
vi .env

# 重启 backend（WVP 相关配置在 backend 中读取）
docker compose -p smartcity restart backend

# 如果修改了 AI 相关配置（如 REDIS_URL/BACKEND_URL）
docker compose -p smartcity restart ai
```

#### rules.yaml 修改（热重载无需重启）

```bash
# 直接编辑保存即可，下一次评估周期自动生效
vi configs/rules.yaml

# 验证热重载（查看日志）
docker compose -p smartcity logs --tail 5 backend 2>&1 | grep "已加载"
```

#### bytetrack.yaml 修改（需重启 AI 服务）

```bash
# 编辑跟踪器配置
vi configs/bytetrack.yaml

# 重启 AI 服务使配置生效
docker compose -p smartcity restart ai
```

### 8.3 模型更新

```bash
# 1. 替换 models/ 目录下的模型文件
# YOLO 权重
cp /path/to/new/yolo11n.pt models/yolo11n.pt

# Chronos-2 模型
cp -r /path/to/new/chronos-model/* models/

# 2. 重启 AI 服务（YOLO 模型在 AI 服务中加载）
docker compose -p smartcity restart ai

# 3. 重启 backend（Chronos-2 模型在后端中加载）
docker compose -p smartcity restart backend

# 4. 验证模型加载
curl -s http://localhost:8001/health
curl -s http://localhost:8000/api/prediction/health
```

> 模型更新需重启对应服务才能生效（非热重载）。Chronos-2 模型加载失败会自动降级为线性外推（`degraded=true`）。

### 8.4 数据库迁移

#### Redis key 前缀变更

若需修改 Redis key 前缀（默认 `sc`），需迁移现有数据：

```bash
# 1. 导出当前数据
docker compose -p smartcity exec redis redis-cli --scan | head -20  # 查看现有 key

# 2. 修改 .env 中的 REDIS_PREFIX（如有）或 config.py 中的 redis_prefix

# 3. 迁移 key 前缀（示例: sc -> sc2）
docker compose -p smartcity exec redis redis-cli --raw --scan --pattern "sc:*" | \
  while read key; do
    newkey=$(echo "$key" | sed 's/^sc:/sc2:/')
    docker compose -p smartcity exec redis redis-cli RENAME "$key" "$newkey"
  done

# 4. 重启服务
docker compose -p smartcity restart ai backend
```

#### 数据迁移脚本

如需批量迁移或清理数据，可编写 Python 脚本通过 `redis.asyncio` 操作：

```python
import asyncio
import redis.asyncio as aioredis

async def migrate():
    r = aioredis.from_url("redis://redis:6379/0", decode_responses=True)
    # 示例: 扫描所有设备 key 并更新字段
    async for key in r.scan_iter("sc:device:*"):
        data = await r.hgetall(key)
        # 迁移逻辑...
        print(f"迁移: {key}")
    await r.aclose()

asyncio.run(migrate())
```

> 数据迁移前务必备份 Redis 数据（见第六章）。迁移操作建议在低峰期进行，避免影响实时业务。

### 8.5 测试套件

项目内置 73 个自动化测试（单元 + 集成 + 功能），覆盖越线计数、实时状态、事件 API、Schema 校验、离线流量统计、视频异常、WVP 客户端等模块。测试均为**自包含**（stub 掉 `ultralytics`、mock Redis、不依赖真实视频/模型），可在无 Docker / 无 GPU 环境直接运行。

#### 测试文件一览

| 测试文件 | 类型 | 覆盖内容 |
|---------|------|---------|
| `tests/test_counter.py` | 单元 | `LineCrossingCounter`：Enter/Exit 判定、抖动抑制、hold_frames 确认、去重、冷却、count_only 过滤、ROI 裁剪、ID 切换、多目标（16 用例） |
| `tests/test_counter_accuracy.py` | 集成 | 已知真值的多目标场景精度基准：precision/recall/accuracy = 1.00（4 场景） |
| `tests/test_realtime.py` | 单元 | `realtime.py`：`apply_event` 累加、负数钳位、按天隔离、N 分钟区间、单向车道 `current_vehicles` 累计、双向人流对冲（FakeRedis mock） |
| `tests/test_api_events.py` | 功能 | `/api/events` 全链路：POST 事件入库 + GET 历史读取 + count_only 设备 current 更新（FastAPI TestClient） |
| `tests/test_schemas_validation.py` | 单元 | `count_only`/`camera_type` 的 `Literal` 枚举校验（拒绝 `Enter`/`in`/`both` 等非法值） |
| `tests/test_video_processor_stats.py` | 单元 | 离线处理器 60s 滑动窗口流量统计（`vehicle/person_flow_in/out`） |
| `tests/test_anomaly.py` | 单元 | 黑屏/花屏异常识别（合成帧，双条件 AND 判定） |
| `tests/test_wvp_client.py` | 单元 | WVP-GB28181 客户端登录/设备查询/点播 |

#### 运行命令

```bash
# 运行全部测试（需在项目根目录，Python 3.11+）
python -m pytest tests/ -q

# 运行单个测试文件
python -m pytest tests/test_counter.py -v

# 运行指定用例
python -m pytest tests/test_realtime.py::test_count_only_vehicle_updates_current -v

# 查看详细输出（含 print）
python -m pytest tests/ -v -s

# 生成测试报告
python -m pytest tests/ --tb=short --junitxml=test-results.xml
```

> 测试无需启动 Docker 服务，直接在宿主机 Python 环境运行（仅需 `pytest`、`pydantic`、`fastapi`、`httpx` 依赖）。镜像构建时已安装全部测试依赖。

#### 在容器内运行

```bash
# 在 backend 容器内运行测试
docker compose -p smartcity exec backend python -m pytest tests/ -q

# 临时启动容器运行测试后退出
docker run --rm -v $(pwd):/app -w /app smart-city-platform:latest \
  python -m pytest tests/ -q
```

#### 接口冒烟测试

`scripts/interface_test.sh` 提供 38 个 REST 端点的端到端冒烟测试（需启动 backend + redis）：

```bash
# 启动服务后运行
bash scripts/interface_test.sh

# 期望输出: 38/38 通过, 0 失败
```

> 该脚本覆盖设备注册/查询/删除、实时统计、告警（含视频异常 onset/recovery）、预测、警力分配、事件历史等全部接口，适合版本发布前的回归验证。

#### 离线视频精度验证

`tool/video_processor.py` 提供离线处理能力，输出含 `vehicle/person_flow_in/out`（60s 滑动窗口）的统计 JSON，用于在无实时流环境下验证计数精度：

```bash
# 离线处理测试视频（输出统计 + 事件 + 告警）
python tool/video_processor.py \
  --video data/test_50f.mp4 \
  --output output/ \
  --camera_id CAM001 \
  --frame_skip 5

# 检查输出统计
python -c "import json; d=json.load(open('output/result.json')); print(d['final_statistics'])"
```
