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

## 部署指南

### 部署形态总览

系统由**两个互相独立的 Docker Compose 项目**组成，按需选择部署形态：

| 形态 | 启动的服务 | 适用场景 |
|------|-----------|---------|
| 最小部署 | `smartcity`: ai + backend + redis | 设备手填 RTSP 地址即可跑通 AI 计数 |
| 完整部署 | `smartcity` + `wvp`: wvp + zlmediakit + wvp-mysql + wvp-redis | 接 GB28181 国标摄像头，设备自动同步 |

```
┌─ smartcity 网络 ─────────────────┐      ┌─ wvp-net 网络 ──────────────────┐
│  ai(8001) ⇄ backend(8000) ⇄ redis│      │  wvp(18080/5060, host网络)      │
└──────────────────────────────────┘      │  zlmediakit(80) mysql redis     │
        │                                  └────────────────────────────────┘
        │  跨网络互不连通! backend 访问 WVP 必须用宿主机 IP (见第四步)
        └──────────────────────────────────────────┘
```

### 第一步：准备

```bash
# 1. 克隆仓库
git clone https://github.com/assassin5966/Intelligent-Transportation-System.git
cd Intelligent-Transportation-System

# 2. 准备环境变量
cp .env.example .env

# 3. 准备 YOLO 权重 (必须!)
#    models/ 被 gitignore 不入库, 但构建时 COPY 进镜像; 本地缺失则构建直接失败
mkdir -p models
# 国内网络从 hf-mirror 下载 (~5.4MB):
wget -O models/yolo11n.pt https://hf-mirror.com/Ultralytics/YOLO11/resolve/main/yolo11n.pt
# 有外网时也可以从 GitHub Release 下载, 或让本机已有的 ultralytics 自动下载后拷入

# 4. (可选) 放入 Chronos-2 时序预测模型到 models/ (config.json 等)
#    缺失时后端启动不报错, 预测自动降级为线性趋势外推
```

**环境要求**：Docker 20.10+（含 BuildKit）、Docker Compose v2；Windows 需 10 22H2+ / Win11。

### 第二步：构建业务镜像

```bash
docker build -t smart-city-platform:latest .
```

镜像**自动适配 CPU 架构**（构建时按 `dpkg --print-architecture` 分支），无需任何参数：

| 架构 | torch 来源 | 体积 | 首次构建耗时 |
|------|-----------|------|------------|
| x86_64 (amd64) | `download.pytorch.org/whl/cpu`（轻量 CPU 版） | ~3GB | 5-15 分钟 |
| aarch64 (arm64) | PyPI 清华源（官方 CPU 源无 arm64 wheel，含 CUDA 依赖约 2GB+） | ~6.5GB | 30-60 分钟 |

> - pip / apt 已内置国内加速（清华 PyPI + 清华 debian 源），无需额外配置。
> - amd64 需 GPU 推理时：编辑 Dockerfile 把 amd64 分支 `--index-url` 改为
>   `https://download.pytorch.org/whl/cu121`，并在 compose 启用 ai 服务 GPU 块。
> - **构建失败排查**：`COPY models 失败` = 第一步的权重没放；下载超时 = 见下方加速器。

### 国内受限网络：基础镜像预拉

Docker Hub 直连（`registry-1.docker.io`）在国内通常超时。若 `docker info` 未配置可用的 Registry Mirror，先手动预拉基础镜像再构建/启动：

```bash
# 实测可用加速器 (2026-08): docker.m.daocloud.io / docker.1panel.live / ccr.ccs.tencentyun.com
# 不可用: docker.1ms.run / docker.tbedu.top / hub-mirror.c.163.com / dockerpull.org
M=docker.m.daocloud.io

for img in library/python:3.11-slim library/redis:7-alpine library/mysql:8.0 zlmediakit/zlmediakit:master; do
  docker pull $M/$img && docker tag $M/$img ${img#library/}
done
```

> 注意：DaoCloud 对**个人镜像有白名单限制**（如 `648540858/wvp_pro` 不在白名单拉不到）；
> 官方镜像（`library/*`）与 `zlmediakit/zlmediakit` 可正常拉取。

### 离线构建（目标机器无外网）

在有网机器上构建并导出，拷贝到目标机器导入（详见[完全离线部署](#完全离线部署无外网机器)）：

```bash
# 有网机器
docker build -t smart-city-platform:latest .
docker save smart-city-platform:latest | gzip > smart-city.tar.gz
# 目标机器
docker load < smart-city.tar.gz
```

### 第三步：启动业务服务（smartcity 项目）

```bash
docker compose -p smartcity up -d

# 逐步验证 (每步应看到对应输出)
docker compose -p smartcity ps                       # ai/backend/redis 为 Up
curl http://localhost:8000/health                    # {"status":"ok","service":"backend"}
curl http://localhost:8001/health                    # {"status":"ok","service":"ai",...}
curl http://localhost:8000/api/stats/realtime        # 返回统计 JSON

# 日常运维
docker compose -p smartcity logs -f backend          # 跟日志
docker compose -p smartcity down                     # 停止
```

服务端口：后端 `8000` / AI `8001` / Redis `16379`（避让宿主 6379）。

> - `-p smartcity` 项目名隔离（compose 未固定 container_name），适合共享服务器。
> - compose 内置 `rtsp-server` + `rtsp-streamer-{vehicle,person}` 测试推流服务（循环推测试视频），
>   联调时可 `docker compose -p smartcity stop rtsp-server rtsp-streamer-vehicle rtsp-streamer-person`。
> - **最小部署到此完成**：`POST /api/devices` 手填设备（`stream_url` 填
>   `rtsp://<宿主IP>:8554/vehicle` 可用内置测试流）即可跑通。

### 第四步（可选，GB28181 接入）：部署 WVP 信令平台

按 CPU 架构选择方案--**arm64 机器没有现成镜像，必须源码自建**：

| 场景 | 方案 |
|------|------|
| x86_64 (amd64) | 现成镜像 `648540858/wvp_pro:latest`（或仓库 `wvp-lower/`、`wvp-upper/` 级联编排）|
| aarch64 (arm64) | **必须自建**：本仓库 `docker-compose.wvp.yml`（gitee 源码 maven 编译）|

#### 4a. arm64 自建 WVP（本仓库方案）

```bash
# 1. 克隆 WVP 源码 (国内 gitee 镜像, ~1分钟)
git clone --depth 1 https://gitee.com/pan648540858/wvp-GB28181-pro.git /tmp/wvp-src

# 2. 预拉编译用基础镜像 (DaoCloud, 见第二步加速器)
M=docker.m.daocloud.io
docker pull $M/library/maven:3.9-eclipse-temurin-21 && docker tag $M/library/maven:3.9-eclipse-temurin-21 maven:3.9-eclipse-temurin-21
docker pull $M/library/eclipse-temurin:21-jre && docker tag $M/library/eclipse-temurin:21-jre eclipse-temurin:21-jre

# 3. 构建 WVP 镜像 (maven 多阶段编译, 约 10-20 分钟)
docker build -t wvp:2.7.4 -f deploy/wvp/wvp/Dockerfile /tmp/wvp-src

# 4. 启动 WVP 四容器 (端口映射 30000-30500 较多, 首次 up 需 1-3 分钟属正常)
docker compose -p wvp -f docker-compose.wvp.yml up -d

# 5. 验证
docker compose -p wvp -f docker-compose.wvp.yml ps            # 四容器全 Up
curl -o /dev/null -w "%{http_code}\n" http://localhost:18080  # 200/404 均表示 WVP 存活
curl -o /dev/null -w "%{http_code}\n" http://localhost:80     # 200, ZLM 存活
docker logs wvp-server 2>&1 | grep "SIP.*启动成功"             # tcp/udp 5060 启动成功
docker logs wvp-server 2>&1 | grep "ZLM-连接成功"              # WVP 已连上 ZLM
```

WVP 管理后台：`http://<宿主IP>:18080`（admin / admin）。

**arm64 方案已知注意点**：
- **`WVP_HOST` 必须填摄像头可达的宿主机 IP**（默认 `172.16.168.9`，按实际网络改
  `docker-compose.wvp.yml` 的 `WVP_HOST`）。WVP 用 host 网络 bind 该 IP 的 5060 端口。
- **ZLM secret 漂移**：ZLM 认为配置里的 secret 非法时自动生成随机值，**ZLM 容器重启后
  secret 会变**，导致 WVP 日志刷 `ZLM-尝试连接失败`。恢复：从 ZLM 日志取新 secret
  （`docker logs wvp-zlmediakit 2>&1 | grep "modified it to"`），更新到
  `deploy/wvp/wvp/application.yml` 的 `media.secret`（或 `ZLM_SECRET` 环境变量），重启 WVP。
- WVP 专用 MySQL 映射宿主 `3307`（避让已占用的 3306）、Redis 映射 `6380`。

#### 4b. 摄像头（IPC）侧 GB28181 配置

IPC Web 管理界面 -> 网络 -> 平台接入 -> GB28181：

| 参数 | 值 |
|------|-----|
| SIP 服务器 ID | `34020000002000000001` |
| SIP 域 | `3402000000` |
| SIP 服务器地址 | `<宿主机IP>`（arm64 方案即 `WVP_HOST` 的值）|
| SIP 端口 | `5060` |
| SIP 密码 | `12345678` |
| 视频通道 | 建议选子码流（降低推理压力）|

#### 4c. 后端对接 WVP（关键：跨网络用宿主 IP）

`smartcity` 与 `wvp` 是两个隔离网络，**容器名互不可达**，`.env` 必须用宿主机 IP：

```bash
# .env 修改 (172.16.168.9 换成实际宿主 IP)
WVP_ENABLED=true
WVP_API_URL=http://172.16.168.9:18080     # 不要用 http://wvp:18080 (跨网络不通!)

docker compose -p smartcity up -d backend  # 重建 backend 生效
docker logs smartcity-backend-1 2>&1 | grep "WVP"   # 应看到 WVP 同步已启动
```

#### 4d. 设备上线与启流

```
IPC 注册 (上电/保存配置)
  -> WVP 管理后台「国标设备」显示在线
  -> POST /api/devices/sync                    # 手动触发同步入表 (或等 30s 自动)
  -> POST /api/devices/glm-5.3_common/enable   # 配计数线/ROI 并启动 AI 管道
  -> 之后设备上下线/断流由 wvp_sync(每30s)与 AI 断流刷新自动维护
```

### 完全离线部署（无外网机器）

镜像不随仓库分发。在**有网机器**构建/拉齐全部镜像后导出，拷贝到**离线机器**导入：

**镜像清单**：

| 镜像 | 用途 | 离线机器是否必需 |
|------|------|----------------|
| `smart-city-platform:latest` | 业务（AI+后端，含模型） | 最小部署必需 |
| `redis:7-alpine` | 业务状态存储 | 最小部署必需 |
| `wvp:2.7.4` | WVP 信令（arm64 需自建） | GB28181 部署必需 |
| `zlmediakit/zlmediakit:master` | 流媒体 | GB28181 部署必需 |
| `mysql:8.0` | WVP 数据库 | GB28181 部署必需 |
| `wvp 专用 redis` | 即 `redis:7-alpine`，复用 | - |

**有网机器导出**：

```bash
docker save smart-city-platform:latest redis:7-alpine \
  wvp:2.7.4 zlmediakit/zlmediakit:master mysql:8.0 \
  | gzip > smartcity-all-images.tar.gz
```

**离线机器导入并启动**：

```bash
docker load < smartcity-all-images.tar.gz
cd Intelligent-Transportation-System   # 仓库代码 git 内网克隆或拷贝
cp .env.example .env                   # 按需修改 (WVP_API_URL 用宿主 IP)
docker compose -p smartcity up -d      # 镜像已本地存在, 不再联网
docker compose -p wvp -f docker-compose.wvp.yml up -d
```

> 离线机器同样需要仓库代码目录（compose 文件 + 挂载的 `app/`、`configs/` 等），
> 通过内网 git 镜像或直接拷贝目录获取；模型已打包进业务镜像，无需单独放 `models/`。

### 常见部署问题排查

| 症状 | 原因与解决 |
|------|-----------|
| 构建 `COPY models` 失败 | 第一步的 `models/yolo11n.pt` 没放（gitignore 不入库）|
| 拉基础镜像超时 / `registry-1.docker.io` 报错 | Docker Hub 直连不通，用第二步加速器预拉并重打标 |
| arm64 拉 `648540858/wvp_pro` 失败 / `no matching manifest for linux/arm64` | 该镜像无 arm64 版本，走 4a 源码自建；DaoCloud 亦将其排除在白名单外 |
| arm64 用 `mysql:5.7` 报 `no matching manifest` | 5.7 无 arm64 镜像，统一用 `mysql:8.0` |
| backend 日志 WVP 同步失败 / 连接超时 | `WVP_API_URL` 用了容器名 `wvp`（跨网络不通），改成宿主机 IP |
| WVP 日志刷 `ZLM-尝试连接失败` | ZLM 重启后 secret 漂移，按 4a 注意点同步新 secret 后重启 WVP |
| WVP SIP 启动失败 `端口被占用或ip不正确` | `WVP_HOST` 不是本机网卡 IP；多网卡机器选摄像头可达的那个 |
| `docker compose up` 卡在创建 wvp-zlmediakit | 30000-30500 端口映射量大属正常，等 1-3 分钟 |
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
| GET | `/api/devices/{id}/play` | 前端播放地址（WVP→flv / RTSP→HLS，浏览器可播）|
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
