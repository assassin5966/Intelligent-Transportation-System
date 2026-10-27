# 智慧古城车辆人流监管平台 (smart-city-platform)

基于 YOLO11 / ByteTrack 的视频 AI 越线计数 + FastAPI 业务后端（实时统计 / 告警 / 预报）+ Vue3 大屏前端，并通过 WVP-GB28181 / ZLMediaKit 接入监控视频流的一体化平台。

> 架构要点：**AI 分析服务与业务后端共用同一镜像** `smart-city-platform`（CPU 版 `:latest` / GPU 版 `:gpu`），前端为预构建 `dist` 由 nginx 托管。视频流接入依赖外部已部署的 WVP / ZLMediaKit（本仓库仅作消费方）。

---

## 1. 项目结构

```
smart_transport_git/
├─ app/                       # 应用代码: AI 分析服务 + FastAPI 业务后端
│  ├─ ai/                     # YOLO11/ByteTrack 越线计数、GPU 引擎、批量推理
│  ├─ backend/                # FastAPI 路由、实时统计、WVP 客户端、预测
│  └─ ...
├─ configs/                   # 业务/算法/预测配置 (business_rules.yaml 等)
├─ models/                    # YOLO 权重 (*.pt, 不入库, 需预置)  ← 构建必需
├─ data/                     # 设备种子等运行数据
├─ static/                   # 后端静态资源 (运维页 html 等)
├─ logs/                     # 运行日志
├─ scripts/                  # 启动/停止/校验/排障脚本 + systemd 单元
│  ├─ start_all.sh           # CPU 版一键启动 (幂等)
│  ├─ start_all_gpu.sh       # GPU 版一键启动 (幂等, detect-and-start)
│  ├─ stop_all.sh / stop_all_gpu.sh
│  ├─ dt-stack.service       # systemd 开机自启单元
│  ├─ gen_requirements_lock.sh   # 生成锁依赖 (联网构建机)
│  └─ check_dist_consistency.sh  # 前端 dist 一致性校验
├─ docs/                     # 设计 / 部署 / 排障文档
│  ├─ OFFLINE_BUILD.md       # 离线构建闭环 (目标机无外网)
│  └─ PROJECT_OPTIMIZATION_PLAN.md  # 项目优化待办
├─ map-marking-system-vue/   # 大屏前端 (Vue3 + Vite + Leaflet)
│  ├─ dist/                  # 构建产物 (build 生成, nginx 挂载)
│  ├─ public/tiles/          # 离线瓦片
│  └─ nginx.conf
├─ setting-server/           # WVP+ZLMediaKit 部署版 (CPU 服务器 <CPU_SERVER_IP>)
├─ setting-server-gpu/       # WVP+ZLMediaKit 部署版 (GPU 服务器 <GPU_SERVER_IP>)
├─ tool/                    # 工具脚本
├─ sim/                     # 本地模拟与调试工具 (GB28181 设备/推拉流模拟、WVP API 调试、RTP 打包/OSD/截图测试)
├─ Dockerfile                # CPU 版镜像构建 (python:3.11-slim)
├─ Dockerfile.gpu            # GPU 版镜像构建 (CUDA 12.1/12.2)
├─ docker-compose.yml        # CPU 版编排 (name: dt)
├─ docker-compose.gpu.yml    # GPU 版编排 (name: dt, 8x 4090)
├─ docker-compose.mock.yml   # Mock 模式编排
├─ requirements.txt          # Python 依赖 (区间约束)
└─ README-DEPLOY.txt         # 特定补丁的现场执行说明 (与本总览互补)
```

---

## 2. 项目依赖

运行时依赖由 `requirements.txt` 声明（**区间约束**，建议离线锁版，见 §3.2）。分为四层：

| 层 | 关键依赖 |
|---|---|
| AI 视觉分析 | `ultralytics>=8.3.0,<9.0.0`（锁定 <9：跟踪器输出列序兼容）、`opencv-python-headless>=4.9.0`、`numpy>=1.26.0`、`lap>=0.5.12` |
| 时序预测 | `chronos-forecasting>=1.4.0`、`transformers>=4.40.0`、`accelerate>=0.30.0` |
| 业务后端 | `fastapi>=0.111.0`、`uvicorn[standard]>=0.29.0`、`pydantic>=2.7.0`、`python-multipart`、`pydantic-settings` |
| 数据存储/通信 | `redis>=5.0.0`、`aiomysql>=0.2.0`、`httpx>=0.27.0`、`websockets>=12.0`、`python-dotenv`、`loguru`、`tenacity`、`pyyaml`、`cryptography` |

> **注意**：`torch` / `torchvision` **不在** `requirements.txt` 内，而是在 Dockerfile 中按架构单独安装（CPU 走 PyTorch 官方 CPU 源、GPU 走 cu121 源），以避免跨平台 wheel 拉取错误。

---

## 3. 项目使用版本

| 组件 | 版本 / 说明 |
|---|---|
| Python | 3.11（镜像 base `python:3.11-slim-bookworm`） |
| PyTorch | 2.7.0 跨平台 wheel（CPU 源 / GPU cu121 轮子） |
| CUDA | GPU 服务器 12.2（cu121 轮子向下兼容，驱动 R530+） |
| YOLO | YOLO11n（默认推理尺寸 640，权重 `models/yolo11n.pt`） |
| MySQL | 8.0（`mysql:8.0`，宿主机映射 3307→3306） |
| Redis | 7（`redis:7-alpine`，宿主机映射 16379→6379） |
| nginx | 1.27（`nginx:1.27-alpine`，宿主 5173→容器 80） |
| WVP / ZLMediaKit | WVP `2.7.1-2024110702` / ZLM `master`（外部已部署） |
| Node.js | 18+（仅前端 `npm run build` 时本地使用） |
| Docker / Compose | Docker Engine + Compose v2 插件 |

业务套容器固定镜像名：`dt-ai` / `dt-backend` / `dt-mysql` / `dt-redis` / `dt-frontend`，共享网络 `dt_default` + 外部 `wvp-shared`。

---

## 4. 部署使用

### 4.1 离线一键（推荐，目标机无外网）
目标机只 `docker load` 预构建镜像，**不在目标机 build**。准备项见 `docs/OFFLINE_BUILD.md`。

```bash
# GPU 服务器 (<GPU_SERVER_IP>)
docker load -i smart-city-platform_gpu.tar
bash scripts/start_all_gpu.sh

# CPU 服务器 (<CPU_SERVER_IP>)
docker load -i smart-city-platform.tar
bash scripts/start_all.sh
# 注: 部署时把 <GPU_SERVER_IP>/<CPU_SERVER_IP> 替换为真实内网服务器地址
```

启动脚本已做**幂等闭环**：WVP 套采用 detect-and-start（容器已存在则直接 `docker start`，绕开 project 名歧义撞名）；业务套 `up -d`（有变更才重建）。可重复执行。

可选参数：
- `bash scripts/start_all_gpu.sh --no-backend`：只起 WVP 套，不启业务套。

### 4.2 本地 / 联网构建
```bash
# CPU
docker compose up -d
# GPU (需先装 nvidia-container-toolkit)
docker compose -f docker-compose.gpu.yml up -d --build
```

### 4.3 大屏前端
在 `map-marking-system-vue/` 下 `npm install && npm run build` 生成 `dist/`，由 nginx 挂载（宿主 5173）；仅改 `dist`/瓦片/配置后 `docker exec dt-frontend nginx -s reload` 即可，无需重建镜像。

### 4.4 开机自启 / 重启自愈
```bash
cp scripts/dt-stack.service /etc/systemd/system/dt-stack.service
# 若部署根不是 /opt/smart-transport, 改 ExecStart 路径
systemctl daemon-reload
systemctl enable --now dt-stack.service
```
配合容器内 `restart: unless-stopped` 实现 reboot 后自动恢复。

### 4.5 Mock 模式（无真实视频流调试）
`.env` / `.env.gpu` 设 `MOCK_ENABLED=true`，AI 不拉流、由模拟器生成事件，便于离线联调。

### 4.6 校验
```bash
# 前端产物一致性 (资源完整/是否过期/是否残留外网 CDN)
bash scripts/check_dist_consistency.sh
# 服务健康检查
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/api/health
```

### 4.7 本地模拟推流 (sim/)
`sim/` 提供 **GB28181 下级设备模拟器**，可模拟一台 IPC 向上级平台（WVP）完成 SIP 注册、Catalog/Keepalive，并在平台发起点播（INVITE）后用 ffmpeg 生成 MPEG-PS 流经 RTP 推给 ZLMediaKit，**用于无真实摄像头时的联调与压测**。

前置：一台可达的 WVP（含 ZLMediaKit），以及可调用的 ffmpeg（默认通过 `docker exec -i <wvp容器> ffmpeg` 复用容器内的 ffmpeg，无需本机安装）。

在 `sim/` 目录下用控制脚本 `simctl.sh` 启停：
```bash
./simctl.sh check     # 校验 sim.json 配置是否合法
./simctl.sh start     # 普通启动: 平台发起点播(INVITE)才推流
./simctl.sh keep      # 持续推流: 通道无活跃会话时自动调 WVP 点播接口补拉, 适合长时间测试
./simctl.sh stop      # 停止并清理容器内残留推流 ffmpeg 进程
./simctl.sh restart   # 重启 (普通模式)
./simctl.sh rekeep    # 重启 (keep 模式)
./simctl.sh status    # 查看运行状态
./simctl.sh log [n]   # 查看最近 n 行日志 (默认 40)
```

也可直接用 Python 跑模拟器本体：
```bash
python3 gb28181_device.py --config sim.json --verbose   # 普通模式
python3 gb28181_device.py --config sim.json --keep       # 持续补拉模式
python3 gb28181_device.py --check                        # 仅校验配置
```

配置：内置默认参数在 `gb28181_device.py` 的 `DEFAULT_CONFIG`，可用 `sim/sim.json` 覆盖关键字段（占位符请替换为真实值）：
- `server_ip` / `server_port`：上级平台 SIP 地址（WVP 的 SIP 端口，默认 5060）
- `device_id` / `password`：本设备国标编号与注册密码（需与 WVP 平台侧一致）
- `channels[]`：通道列表（`id` / `name` / `rtsp` 源 / `mark`）
- `auto_replay`：自动补拉开关（也可经 `--keep` 开启）
- `ffmpeg_cmd`：ffmpeg 调用方式（默认复用 wvp 容器）

> 部署到服务器：先改 `up.py` / `push.py` 顶部的 `HOST`、`PASSWORD` 为目标机真实值，再 `python3 up.py` 把整个 `sim/` 上传到服务器 `/home/sim/`；`push.py <local> <remote>` 可单文件推送。
> `simctl.sh stop` 会按容器名（`wvp-server` / `wvp-upper-wvp` / `wvp` / `sim-*`）清理残留推流进程。

### 4.8 基于 start_all_gpu.sh 的 Docker Compose 部署（GPU 全量）
适用：在 GPU 服务器（`<GPU_SERVER_IP>`，如 8×4090）上以 Docker Compose 编排一次性拉起 **WVP 套（mysql/redis/zlm/wvp）+ 业务套（backend/ai/mysql/redis）+ 大屏前端**。`start_all_gpu.sh` 本质是对多份 compose 文件的封装，已做幂等闭环（可重复执行；WVP 段 detect-and-start、业务套"有变更才重建"）。

#### 前置准备
1. **密钥一致**：确认 `setting-server-gpu/` 下 `application.yml` / `conf/config.ini` / `docker-compose.yml` 三处密钥一致（WVP 数据库密码、ZLM `API_SECRET` / `MEDIASERVER_ID` 等）。
2. **业务环境变量**：根目录 `.env.gpu` 已写好 `WVP_ENABLED`、`ZLM_PUBLIC_BASE`、`SERVER_IP`、MySQL/Redis 连接等。脚本通过 `--env-file .env.gpu` 显式传入，compose 不会自动读取。
3. **GPU 镜像就绪**：`docker load -i smart-city-platform_gpu.tar`（或本地 `docker compose -f docker-compose.gpu.yml build ai`）。
4. **NVIDIA 环境**：宿主机已装驱动（R530+）+ `nvidia-container-toolkit`，`docker info` 能看到 nvidia runtime。

#### 执行
```bash
cd <项目根目录>
bash scripts/start_all_gpu.sh            # 全量 (WVP 套 + 业务套 + 前端)
bash scripts/start_all_gpu.sh --no-backend   # 仅起 WVP 套做联调
```

#### 脚本内部流程（6 步）
1. **语法自检**：`docker compose config` 校验 `setting-server-gpu/docker-compose.yml` 与 `docker-compose.gpu.yml`（含 `.env.gpu`）。
2. **WVP 套 detect-and-start**：若 `wvp-upper-wvp` 容器已存在 → 直接 `docker start` 四个容器（幂等，绕开 project 名歧义撞名）；否则首次 `up -d mysql redis`。
3. **建表**：全新库时从 WVP 镜像提取 `mysql.sql` 导入（`wvp_media_server` 等表），再 `docker restart wvp`；已有表则跳过。
4. **WVP 套全起**：`docker compose -f setting-server-gpu/docker-compose.yml up -d`（zlm/wvp）。
5. **验证 ZLM API**：`curl .../index/api/getServerConfig?secret=<ZLM_SECRET>`。
6. **业务套**：预检 `wvp-shared` / `dt_default` 网络，再 `docker compose --env-file .env.gpu -f docker-compose.gpu.yml up -d`（有变更才重建），启动 `dt-ai`（预留 8 卡）/ `dt-backend` / `dt-mysql` / `dt-redis` / `dt-frontend`。

#### 访问地址
- WVP 平台：http://`<LAN_IP>`:18080/ （admin / admin）
- ZLM webassist：http://`<LAN_IP>`:12081/webassist/index.html
- 大屏前端：http://`<LAN_IP>`:5173
- 后端 API：http://`<LAN_IP>`:8000/api/health

#### 查看日志
```bash
docker logs -f wvp-upper-wvp     # WVP 平台
docker logs -f wvp-upper-zlm     # ZLM 流媒体
docker logs -f dt-ai             # AI 推理 (关注: 推理模式/引擎数/各路 fps)
docker logs -f dt-backend        # 后端 (WVP 同步/事件上报)
```

#### 关闭
```bash
bash scripts/stop_all_gpu.sh
```

> **（附注）手动 Docker Compose 等价操作**：不想跑一键脚本时可分步执行（注意先 `cd` 到对应目录、`--env-file` 显式传入）：
> ```bash
> cd setting-server-gpu && docker compose up -d          # WVP 套 (首次需建表)
> cd .. && docker compose --env-file .env.gpu -f docker-compose.gpu.yml up -d   # 业务套
> ```
> `wvp-shared` 网络由 WVP 套创建，业务套依赖它；CPU/GPU 两套 compose 同名 `dt`，同机勿混跑（CPU 版用 `start_all.sh` / `docker-compose.yml`）。

---

## 5. 注意事项

1. **离线约束**：目标机无外网，禁止 `docker build`；镜像必须先在联网构建机 `docker save` 成 tar 再拷入。前端 `dist`、瓦片、权重 `models/`、WVP 配置 `conf/` 均随离线包预置。
2. **权重必须预置**：`models/*.pt` 不入库、不联网下载；缺失时 Dockerfile 会**清晰报错**而非含糊失败。
3. **CPU/GPU 不可同机混跑**：两套 compose 均为 `name: dt`，同时运行会容器撞名。切换版本前先 `bash scripts/stop_all.sh`（或 `stop_all_gpu.sh`）清理旧容器。
4. **WVP 套 project 名歧义**：`setting-server` 与 `setting-server-gpu` 目录名不同导致 compose project 名不同，但容器内 `wvp-upper-*` 名称固定，旧版 `up -d` 会撞名。当前启动脚本已用 detect-and-start 规避。
5. **环境变量收口**：`WVP_ENABLED` / `ZLM_PUBLIC_BASE` / `SERVER_IP` 等需在 `.env`(CPU) / `.env.gpu`(GPU) 中配置；`.env` 不入库，请按示例补齐。
6. **前端本地化**：`tile-preview.html` 与 leaflet 依赖已本地化（`public/vendor/leaflet/`），离线 tar 需包含该目录，**勿留外网 CDN**。
7. **依赖可复现（P0.1）**：在联网构建机执行 `bash scripts/gen_requirements_lock.sh` 生成 `requirements.lock.txt`（精确 `==`），构建时改用锁文件以保证跨时间/机器一致。
8. **端口映射速查**：后端 `8000`、AI `8001`、前端 `5173`、MySQL `3307`、Redis `16379`、WVP `18080`/SIP `5060`、ZLM `12081`(API)/`80`(FLV)/`554`/RTP `30000-30100`。
9. **排障文档**：详细离线流程与排障见 `docs/OFFLINE_BUILD.md`；优化待办见 `docs/PROJECT_OPTIMIZATION_PLAN.md`；特定补丁现场步骤见 `README-DEPLOY.txt`。
