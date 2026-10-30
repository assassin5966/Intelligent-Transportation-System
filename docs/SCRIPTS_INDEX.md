# scripts/ 可执行文件使用说明

> 一份完整的「**scripts/ 下每个可执行文件的说明书**」,按"使用场景"分组,每条都注明**用途 / 用法 / 前置 / 输出 / 风险**。
> 适用版本: 当前 `D:\散活\smart-transport` 仓库,2026-10-14 之后。
>
> 目录结构:
>
> 1. 速查表 (按"我想做 X"反查)
> 2. 启动 / 停止 (生产链路,每天用)
> 3. 运维 / 模式切换
> 4. 离线视频测试 (本地无 RTSP 流时)
> 5. RTSP 实时流测试 (mock 推流)
> 6. 异常检测端到端验证
> 7. 冒烟 / 基准测试 (性能 + 回归)
> 8. 数据导入工具 (运维一次性脚本)
> 9. 公共前置 / 全局约定
> 10. 故障速查

---

## 1. 速查表 — "我想做 X,跑哪个?"

| 我想做… | 用这个脚本 | 一句话说明 |
|---|---|---|
| **首次部署 / 重启整套系统(GPU 服务器)** | `start_all_gpu.sh` | 6 阶段拉起 WVP+ZLM+业务+前端 |
| **首次部署 / 重启整套系统(CPU 服务器)** | `start_all.sh` | 同上,但读 `.env` + `docker-compose.yml`,IP `.112` |
| **只起 WVP 套不拉业务套** | `start_all[_gpu].sh --no-backend` | 调试 WVP/ZLM 时用 |
| **停整套系统** | `stop_all[_gpu].sh` | 默认 `stop`(保留卷),加 `--down` 才删容器 |
| **开 Mock 数据(不拉流不推理)** | `mock_mode.sh start` | 自动禁用 WVP 同步 |
| **关 Mock** | `mock_mode.sh stop` | 改回 `.env` 原值 + 停 backend/ai |
| **看 Mock 是否在跑** | `mock_mode.sh status` | — |
| **GPU 服务器自检** | `check_gpu_env.sh` | 6 阶段探针,与服务解耦 |
| **本地测一个视频(车辆/人流)** | `offline_test.sh <视频> ...` | Docker 一键跑 video_processor |
| **后台跑长视频测试** | `offline_test.sh --background <视频>` | nohup + 日志落 `logs/` |
| **把长视频切短** | `cut_video.sh <视频> <秒数> [输出名]` | ffmpeg 取前 N 秒 |
| **测异常检测(黑屏/花屏)** | `anomaly_test.sh` | 合成异常视频 + 端到端校验 |
| **起 mock RTSP + 推流 + AI 实时跑** | `stream_test.sh [vehicle|person]` | 默认双设备 |
| **停 stream_test** | `stream_test.sh --stop` | — |
| **冒烟测后端 API** | `smoke_test.sh` | 启 Redis + 后端,验证 5 个 API |
| **CPU batch 推理冒烟** | `smoke_cpu_batch.py` | 加载 / 自检 / 多路组批 / 释放 |
| **GPU 1/8/32 路基准** | `zbny-bench-gpu-infer.py` | 跑在 ai 容器内 |
| **高速车漏检 + 缝合端到端** | `zbny-test-e2e-fast-car.py` | — |
| **CPU 推理饱和度** | `zbny-test-infer-saturation.py` | 不依赖 cv2 |
| **轨迹缝合 9 场景验证** | `zbny-test-track-stitching.py` | 不依赖 YOLO |
| **从 Excel 重建设备 JSON** | `regen_device_seed.py` | `data/设备信息汇总表.xlsx` → `device_geo.json` + `device_category.json` |
| **把设备 JSON 全量替换 MySQL** | `replace_device_info.py` | TRUNCATE + 批量 upsert,排云冈类 |
| **生成异常测试视频(单独)** | `gen_anomaly_video.py` | 宿主机直接跑,无需 Docker |

---

## 2. 启动 / 停止(生产链路,每天用)

### 2.1 `start_all.sh` — CPU 服务器部署版一键启动

- **角色**: `setting-server/` WVP 套 + 业务套 + 大屏前端,**整体冷启动**。
- **目标机器**: 内网 IP `23.45.1.112` 的 CPU 服务器(本仓库里 CPU 版本为"传统部署",GPU 已切到 `.env.gpu`)。
- **用法**:
  ```bash
  bash scripts/start_all.sh              # 完整启动
  bash scripts/start_all.sh --no-backend # 只启 WVP 套,不启业务套
  ```
- **6 阶段**:
  1. `docker compose config --quiet` 自检
  2. `up -d mysql redis` + 等 MySQL 就绪
  3. 缺表时自动从 WVP 镜像 extract `mysql.sql` 导入
  4. `up -d` 起全 WVP 套 + 等 Web 200
  5. curl ZLM API 验证
  6. 业务套 + 前端 `up -d --force-recreate`
- **关键参数(硬编码,改时要和 `docker-compose.yml` 同步)**:
  - `MYSQL_CONTAINER="wvp-upper-mysql"` / `WVP_CONTAINER="wvp-upper-wvp"`
  - `DB_PASS="Wvp@123456"` (与 compose MYSQL_PASSWORD)
  - `ZLM_SECRET="zlm_secret_7890"` (三处一致:application.yml / config.ini / 本脚本)
- **输出地址**: WVP `:18080` / ZLM `:12081` / 前端 `:5173` / 后端 `:8000`
- **幂等**: 已建表跳过、已运行容器不重建。

### 2.2 `start_all_gpu.sh` — GPU 服务器部署版一键启动

- **角色**: GPU 版,与 `start_all.sh` 结构一致,但读 `.env.gpu` + `docker-compose.gpu.yml`。
- **目标机器**: 内网 IP `23.45.1.115` 的 GPU 服务器。
- **额外 1~6 步: 启动内置 GPU 探针**(注意:`start_all_gpu_noprobe.sh` 不做探针,本脚本会做)。
- **用法**:
  ```bash
  bash scripts/start_all_gpu.sh              # 完整启动
  bash scripts/start_all_gpu.sh --no-backend # 只启 WVP 套
  ```
- **关键差异(相对 CPU 版)**:
  - `ENV_FILE=".env.gpu"` 显式 `--env-file`(compose 不自动读)
  - `GPU_IMAGE="smart-city-platform:gpu"` 必须先 `docker load` 或 `build` 完
  - 起业务套时 `docker compose --env-file .env.gpu -f docker-compose.gpu.yml`
- **风险**: 启动时调 GPU 探针会与刚拉起的服务抢 CUDA 上下文,**建议改用 `start_all_gpu_noprobe.sh`**,探针用 `check_gpu_env.sh` 单独跑。

### 2.3 `start_all_gpu_noprobe.sh` — GPU 版无探针启动(推荐生产用)

- 与 `start_all_gpu.sh` **完全等价但不做 GPU 探针**,启动更稳。
- 用法:
  ```bash
  bash scripts/start_all_gpu_noprobe.sh
  bash scripts/start_all_gpu_noprobe.sh --no-backend
  ```
- 何时用:
  - **生产 / 联调**首选本脚本
  - 想验证 GPU 时单独跑:`bash scripts/check_gpu_env.sh --run`

### 2.4 `stop_all.sh` / `stop_all_gpu.sh` — 一键停止

- **角色**: 与 start 脚本对应,先停业务套,再停 WVP 套。
- **用法**:
  ```bash
  bash scripts/stop_all.sh             # stop: 停容器但保留 (CPU 版)
  bash scripts/stop_all.sh --down      # down: 停 + 删容器 (保留数据卷)
  bash scripts/stop_all.sh --no-backend # 只停 WVP 套 (业务套未起时的对称用法)
  
  bash scripts/stop_all_gpu.sh         # GPU 版
  bash scripts/stop_all_gpu.sh --down
  ```
- **安全**: **不会删除任何卷/镜像**,生产数据零影响。
- **重启**: `bash scripts/start_all[_gpu].sh`(幂等)。

---

## 3. 运维 / 模式切换

### 3.1 `mock_mode.sh` — Mock 模拟模式开关

- **角色**: 开启后,后端走"模拟 32 设备事件+拥挤"数据,**不拉流不推理**;WVP 同步自动互斥禁用。
- **用法**:
  ```bash
  bash scripts/mock_mode.sh start         # 开 mock (读 .env, CPU)
  bash scripts/mock_mode.sh start --gpu   # GPU 版 (读 .env.gpu)
  bash scripts/mock_mode.sh stop [--gpu]  # 关 mock
  bash scripts/mock_mode.sh status        # 看状态
  ```
- **机制**:
  - 改 `.env` 或 `.env.gpu` 的 `MOCK_ENABLED=true/false`(就地 sed 改)
  - `docker compose up -d --force-recreate backend ai` 重新拉起
  - 等 `/health` 返回 `"mock":true` 才算开启成功
- **风险**: 关 mock 后必须重新执行 `start_all[_gpu].sh` 才能跑真实拉流链路。

### 3.2 `check_gpu_env.sh` — GPU 6 阶段探针(独立)

- **角色**: 与 `start_all_gpu.sh` 内置探针相同,但**单独拆出**避免与启动冲突。
- **6 阶段**:
  1. torch 导入 / CUDA 枚举
  2. 首次 CUDA 初始化(走 `nvidia_uvm`)
  3. YOLO 构造(加载权重)
  4. 首次 track 推理
  5. 跟踪器 API 自检(stage1~4 的 ultralytics 表面契约)
  6. 多图同批 + FP16
- **用法**:
  ```bash
  bash scripts/check_gpu_env.sh          # ai 容器在跑 → compose exec;否则临时容器
  bash scripts/check_gpu_env.sh --run    # 强制临时容器
  ```
- **超时**: `-k 5 90`,即 90s 软超时 + 5s 强杀(SIGTERM 无响应时 SIGKILL)。
- **诊断输出**: 失败时按"卡在 stage N"打印修复命令(`restart ai` / `rmmod nvidia_uvm` / `dmesg` / 调小 `INFER_HALF`)。

---

## 4. 离线视频测试(本地无 RTSP 流时)

### 4.1 `offline_test.sh` — 离线视频 Docker 一键处理(推荐)

- **角色**: 把视频丢进 Docker 跑 `tool/video_processor.py`,带全部配置和挂载。
- **用法**:
  ```bash
  bash scripts/offline_test.sh data/车辆识别20s.mp4                            # 车辆(默认配置)
  bash scripts/offline_test.sh data/人流识别20s.mp4 --camera-type person       # 人流
  bash scripts/offline_test.sh data/人流识别20s.mp4 --frame_skip 1             # 逐帧处理
  bash scripts/offline_test.sh data/车辆识别20s.mp4 --count-only enter         # 单向
  bash scripts/offline_test.sh data/车辆识别20s.mp4 --line "0.1,0.7,0.9,0.7"   # 自定义计数线
  bash scripts/offline_test.sh data/车辆识别20s.mp4 --verbose --background      # 后台 + 详细日志
  tail -f logs/offline_<video>_<时间>.log                                        # 看后台日志
  ```
- **默认计数线**: `--line "0.1,0.75,0.9,0.75" --anchor "0.5,0.9"` (脚本未传 `--line` 时自动加)。
- **`--background` 行为**: `nohup docker run ... >> logs/...log 2>&1 &`,容器名随机带秒级时间戳。
- **video_processor 参数透传**: `--line / --anchor / --count-only / --camera-type / --frame_skip / --no-annotated / --verbose`。
- **前置**: 镜像 `smart-city-platform:latest` 已构建(`docker compose build ai` 或 `docker load`)。

### 4.2 `cut_video.sh` — 视频切分(ffmpeg)

- **角色**: 用 ffmpeg 从长视频截前 N 秒,供离线测试用。
- **用法**:
  ```bash
  bash scripts/cut_video.sh data/人流识别.mp4 20              # → data/人流识别20s.mp4
  bash scripts/cut_video.sh data/人流识别.mp4 20 人流识别20s  # 自定义输出名(不带 .mp4)
  ```
- **编码**: `libx264 -preset ultrafast`,速度快但文件大。

### 4.3 `run_video_processor.sh` — 老的 Docker 入口(已被 offline_test.sh 取代)

- **角色**: 历史遗留入口,处理 `output/test_50f.mp4` 默认视频。
- **用法**:
  ```bash
  bash scripts/run_video_processor.sh                              # 默认参数
  FRAME_SKIP=1 bash scripts/run_video_processor.sh                # 逐帧
  bash scripts/run_video_processor.sh --no-annotated               # 不标注
  bash scripts/run_video_processor.sh --line 0.5,0.1,0.5,0.9      # 垂直线
  ```
- **建议**: 改用 `offline_test.sh`(功能一致 + 更灵活)。

---

## 5. RTSP 实时流测试(mock 推流)

### 5.1 `stream_test.sh` — mock RTSP + 推流 + AI 实时跑

- **角色**: 起 `redis + rtsp-server + rtsp-streamer-vehicle/person + ai + backend`,用 mock RTSP 流跑实时计数。
- **用法**:
  ```bash
  bash scripts/stream_test.sh                    # 默认: 双设备(车辆+人流)
  bash scripts/stream_test.sh vehicle            # 仅车辆
  bash scripts/stream_test.sh person             # 仅人流
  bash scripts/stream_test.sh --count-only enter # 只计 enter
  VEHICLE_VIDEO=data/other.mp4 bash scripts/stream_test.sh   # 自定义视频
  bash scripts/stream_test.sh --stop             # 停止
  ```
- **环境变量**: `VEHICLE_VIDEO` / `PERSON_VIDEO` / `LINE` / `ANCHOR`。
- **运行时序**: 启动 → 等 10s → `FLUSHALL` Redis → 注册设备 → 等 25s → 打印设备列表 / 实时统计 / AI 日志。
- **前置**: `docker-compose.yml` 含 `redis / rtsp-server / rtsp-streamer-vehicle / rtsp-streamer-person / ai / backend` 服务。
- **停止**: `bash scripts/stream_test.sh --stop`(DELETE 设备 + `compose down`)。

---

## 6. 异常检测端到端验证

### 6.1 `anomaly_test.sh` — 异常检测(黑屏/花屏)E2E

- **角色**: 生成合成异常视频 → Docker 跑 `tool/video_processor.py` → 校验异常 JSON 是否检出 `black_screen` + `flower_screen` 的 onset。
- **用法**:
  ```bash
  bash scripts/anomaly_test.sh                       # 默认 90 帧/段,生成标注视频
  bash scripts/anomaly_test.sh --no-annotated        # 不标注(更快)
  SEGMENT=120 bash scripts/anomaly_test.sh           # 自定义每段帧数
  CAMERA_ID=MY_CAM bash scripts/anomaly_test.sh      # 自定义摄像头 ID
  ```
- **流程**:
  1. `python3 scripts/gen_anomaly_video.py` 合成 `data/anomaly_test.mp4`
  2. `docker run ... tool/video_processor.py --frame_skip 1` 逐帧处理
  3. 读 `output/anomaly_test_anomalies.json` 校验 onset 事件
- **前置**: 镜像 `smart-city-platform:latest` 已构建;宿主机有 `python3 + numpy + opencv`(`gen_anomaly_video.py` 用)。

### 6.2 `gen_anomaly_video.py` — 合成异常测试视频(单独跑)

- **角色**: 在宿主机直接生成合成视频,无需 Docker / YOLO。
- **用法**:
  ```bash
  python3 scripts/gen_anomaly_video.py                          # 默认: 90 帧/段, 25fps, 640x480
  python3 scripts/gen_anomaly_video.py --output data/test.mp4   # 自定义输出
  python3 scripts/gen_anomaly_video.py --segment 120            # 自定义段长
  python3 scripts/gen_anomaly_video.py --fps 10 --width 1280 --height 720  # 自定义规格
  ```
- **片段序列**: 正常 → 黑屏 ��� 正常 → 花屏(随机噪声) → 正常。

---

## 7. 冒烟 / 基准测试(性能 + 回归)

### 7.1 `smoke_test.sh` — 后端 API 冒烟

- **角色**: 起一个临时 Redis + 后端容器,跑 5 个核心 API 验证。
- **用法**:
  ```bash
  bash scripts/smoke_test.sh
  ```
- **测试的 API**:
  - `GET /health`
  - `GET /api/stats/realtime`
  - `GET /api/alerts`
  - `POST /api/devices`(注册测试摄像头)
  - `GET /api/devices`
- **容器清理**: 脚本结束自动 `docker rm -f` + `docker network rm`,不污染环境。

### 7.2 `smoke_cpu_batch.py` — CPU batch 推理冒烟

- **角色**: 直接跑在宿主机,验证 `cpu_batch` 模式的 model_pool 加载/分配/释放。
- **用法**:
  ```bash
  python3 scripts/smoke_cpu_batch.py
  ```
- **环境变量(脚本内强制设置)**:
  - `INFER_MODE=cpu_batch`
  - `INFER_CPU_ENGINES=2`
  - `INFER_IMGSZ=640`
- **断言**: 模式为 cpu_batch / 三路设备均能分配槽位 / 三路并发组批成功 / 释放后能复用槽位。

### 7.3 `zbny-bench-gpu-infer.py` — GPU 1/8/32 路批量推理基准

- **角色**: 跑在 ai 容器内,合成帧压 `GpuInferEngine.submit` 路径,输出**有效 fps / 帧延迟 / 均批 / 峰值显存 / EngineBusy 丢帧**。
- **用法**:
  ```bash
  # 在 ai 容器内跑 (compose 已把 scripts/ 挂到 /app/scripts)
  docker compose --env-file .env.gpu -f docker-compose.gpu.yml exec -T ai \
      python scripts/zbny-bench-gpu-infer.py --levels 1,4,8,16,32
  
  # 自定义测量轮数 + 预热
  docker compose --env-file .env.gpu -f docker-compose.gpu.yml exec -T ai \
      python scripts/zbny-bench-gpu-infer.py --levels 1,8,32 --rounds 50 --warmup 5
  ```
- **参数**:
  - `--levels 1,8,32`:逐级压测的路数
  - `--rounds 30`:每级测量轮数
  - `--warmup 3`:预热轮数(不计)
  - `--gpus 0,1`:参与卡号,空=全部
  - `--short-side N`:合成帧短边,默认 `settings.infer_imgsz`
- **对照目标**(本脚本注释):
  - 单路帧延迟 P50 ≤ 130ms
  - 8 路单批推理 ≤ 150ms
  - 均批 = 每卡设备数(组批阈值=本卡设备数)

### 7.4 `zbny-test-e2e-fast-car.py` — 高速车漏检 + 缝合端到端

- **角色**: 验证"高速车漏检 4 帧 → 旧 ID 死亡 → 新 ID 缝合"场景下,**恰好 1 次 enter, 0 次 exit**。
- **用法**:
  ```bash
  python3 scripts/zbny-test-e2e-fast-car.py
  ```
- **前置**: `app.ai.counter.LineCrossingCounter` + `app.ai.tracker.ByteTracker` 路径可达(需装 ultralytics,会强制 `YOLO_CONFIG_DIR=/tmp/...`)。

### 7.5 `zbny-test-infer-saturation.py` — 推理饱和度

- **角色**: 模拟 8 路并发,验证 `InferBusy` 丢帧 + 饱和统计 + 调度器丢帧对账。
- **用法**:
  ```bash
  python3 scripts/zbny-test-infer-saturation.py
  ```
- **环境变量(脚本内强制)**:
  - `INFER_MAX_WORKERS=2`
  - `INFER_MAX_QUEUED=1`
- **不依赖** cv2 / ultralytics,推理函数用 `time.sleep(0.03)` 模拟 30ms。

### 7.6 `zbny-test-track-stitching.py` — 轨迹缝合 9 场景验证

- **角色**: 直接注入 `track_history/miss_count`,不加载 YOLO,验证 9 个场景的缝合判定。
- **用法**:
  ```bash
  python3 scripts/zbny-test-track-stitching.py
  ```
- **场景矩阵**: A/B 正例(高速车漏检 + 缝合),C~H 反例(类别不同 / 反方向 / 超 gap / 在场 / 防双计 / 跨画面跳变),I 端到端(缝合后跨线计数)。

---

## 8. 数据导入工具(运维一次性脚本)

### 8.1 `regen_device_seed.py` — 从 Excel 重建设备 JSON

- **角色**: 读 `data/设备信息汇总表.xlsx` 的 Sheet1 → 生成 `data/device_geo.json` + `data/device_category.json`。
- **用法**:
  ```bash
  python3 scripts/regen_device_seed.py
  ```
- **Excel 列对应**:
  - E 列:点位编号 (GAJK-xxxx / GAKK-xxxx / JTKK-xxxx)
  - F 列:点位分类(出入口便道监控点位 / 入口车辆卡口 / 出口车辆卡口)
  - G/H 列:设备名称(旧/新,白名单用新名)
  - J/K 列:经纬度
  - L 列:状态(已验证/待验证)
- **派生规则**: 出入口类型 + 点位类型由 F 列派生(规则与 `app/common/device_info.py` 的 `derive_*` 一致)。
- **排除**: JTKK 卡口(云冈类)不入库。

### 8.2 `replace_device_info.py` — 全量替换 MySQL `device_info` 表

- **角色**: 读 `data/device_geo.json` + `data/device_category.json` → `TRUNCATE` 后批量 upsert 进 `dt_stats.device_info`。
- **用法**:
  ```bash
  python3 scripts/replace_device_info.py
  ```
- **连接**: `127.0.0.1:3307`(宿主机映射容器内 3306), `root / root123456 / dt_stats`。
- **动作**:
  1. `CREATE TABLE IF NOT EXISTS device_info` (含 `region / entrance_type / point_type` 3 列)
  2. 旧表自动 `ALTER TABLE ADD COLUMN` 补列(幂等)
  3. `TRUNCATE TABLE device_info`
  4. 按 `device_geo.json devices` 全量 `executemany` 插入
- **典型链路**: Excel 更新 → `regen_device_seed.py` → 检查 JSON → `replace_device_info.py` → 后端重启读新表。

### 8.3 `gen_anomaly_video.py`

见 § 6.2。

---

## 9. 公共前置 / 全局约定

### 9.1 Python 包入口

- `scripts/__init__.py` — Python 包标识,使 `scripts` 可作为模块导入。**不要删**。

### 9.2 环境变量优先级

| 优先级 | 文件 | 用途 |
|---|---|---|
| 1 | `.env.mock` | mock 单机调试(本机进程,不走容器) |
| 2 | `.env.local` | 本机开发(读真实 WVP/ZLM 但本机 IP) |
| 3 | `.env` | CPU 服务器部署(默认 compose 读) |
| 4 | `.env.gpu` | GPU 服务器部署(必须显式 `--env-file`) |

### 9.3 共享网络

`wvp-shared` 必须存在(由 `setting-server[-gpu]/docker-compose.yml` 创建),业务套按服务名 `wvp:18080` / `zlmediakit:80` 直连上游。

### 9.4 三处一致密钥

| 密钥 | 位置 1 | 位置 2 | 位置 3 |
|---|---|---|---|
| WVP DB | `setting-server[-gpu]/docker-compose.yml` `MYSQL_PASSWORD` | `start_all[_gpu].sh` `DB_PASS` | WVP 启动自动 |
| ZLM API | `setting-server[-gpu]/application.yml` `media.secret` | `setting-server[-gpu]/conf/config.ini` `[api]` `secret` | `start_all[_gpu].sh` `ZLM_SECRET` |

任何一处改了,**另两处必须同步**。

### 9.5 镜像命名约定

| 镜像 | 用途 | 构建方式 |
|---|---|---|
| `smart-city-platform:latest` | CPU 业务套(`Dockerfile`) | `docker compose build` |
| `smart-city-platform:gpu` | GPU 业务套(`Dockerfile.gpu`) | `docker load -i *.tar` 或 `docker compose -f docker-compose.gpu.yml build ai` |
| `dt-frontend:latest` | 前端(预构建产物) | 由前端流水线构建 |

### 9.6 容器命名约定

| 容器名前缀 | 角色 |
|---|---|
| `wvp-upper-*` | WVP 套(`wvp-upper-mysql / redis / zlm / wvp`) |
| `dt-*` | 业务套(`dt-backend / dt-ai / dt-frontend / dt-mysql / dt-redis`) |
| `sctest-*` | smoke_test.sh 临时容器 |

---

## 10. 故障速查

| 现象 | 优先怀疑 | 用哪个脚本定位 |
|---|---|---|
| 启动卡 GPU 探针 | `nvidia_uvm` / 驱动 | `check_gpu_env.sh` 看 stage1~2 |
| ai 容器启动失败 | 镜像 / 权重 / 配置文件 | `check_gpu_env.sh --run` 强制临时容器 |
| 推理结果不对 | 计数规则 / 配置漂移 | `offline_test.sh --verbose` 看 trace.json |
| 计数漏计高速车 | 轨迹缝合逻辑 | `zbny-test-e2e-fast-car.py` |
| CPU 推理 fps 异常 | 饱和 / 并发 | `zbny-test-infer-saturation.py` |
| 8 路雪崩 | 模型池槽位 | `zbny-bench-gpu-infer.py --levels 1,8,32` |
| 设备点位不同步 | Excel → JSON → DB | `regen_device_seed.py` + `replace_device_info.py` |
| 视频流黑屏 / 花屏 | 异常检测 | `anomaly_test.sh` |
| 后端 API 整体不通 | Redis / 配置 | `smoke_test.sh` |
| 想用 mock 但跑成了真实 | `MOCK_ENABLED` 没改 | `mock_mode.sh status` |

---

## 11. 文件清单

```
scripts/
├── __init__.py                          # Python 包标识(勿删)
├── start_all.sh                         # CPU 服务器启动(老, IP .112)
├── start_all_gpu.sh                     # GPU 服务器启动(含探针)
├── start_all_gpu_noprobe.sh             # GPU 服务器启动(无探针,推荐生产)
├── stop_all.sh                          # CPU 服务器停止
├── stop_all_gpu.sh                      # GPU 服务器停止
├── mock_mode.sh                         # Mock 模式开关
├── check_gpu_env.sh                     # GPU 6 阶段独立探针
│
├── offline_test.sh                      # 离线视频测试(推荐)
├── run_video_processor.sh               # 离线视频测试(老入口,功能一致)
├── cut_video.sh                         # ffmpeg 切视频
├── stream_test.sh                       # mock RTSP + 实时测试
│
├── anomaly_test + gen_anomaly_video.py     # 异常检测端到端
├── smoke_test.sh                        # 后端 API 冒烟
├── smoke_cpu_batch.py                   # CPU batch 推理冒烟
│
├── zbny-bench-gpu-infer.py              # GPU 1/8/32 路基准
├── zbny-test-e2e-fast-car.py            # 高速车漏检 + 缝合
├── zbny-test-infer-saturation.py        # 推理饱和度
├── zbny-test-track-stitching.py         # 轨迹缝合 9 场景
│
├── regen_device_seed.py                 # Excel → 设备 JSON
└── replace_device_info.py               # 设备 JSON → MySQL
```

**共 21 个可执行文件 + 1 个 `__init__.py` + 1 个 `__pycache__/`(自动生成)**。
