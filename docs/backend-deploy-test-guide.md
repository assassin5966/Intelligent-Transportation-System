# 后端部署测试操作文档（简化版）

> 本文档为现场部署/验收测试用简化操作指引，基于 `.trae/documents/ops-guide.md` 提炼。
>
> **前提**：WVP 与 ZLM 上下级服务已由甲方部署完成，本平台仅作为消费方对接。现场只需向甲方获取 **WVP 服务 IP 地址与端口号**，即可完成从镜像拉取到技术接口测试的完整流程。

---

## 〇、流程总览

```
① 准备(.env配置) → ② 镜像构建/拉取 → ③ 后台服务启动 → ④ 基础联通检查
   → ⑤ WVP设备同步 → ⑥ 码流拉取 → ⑦ 运维工具绘图 → ⑧ 启流计数 → ⑨ 技术接口测试
```

全流程共 9 步，以下逐步说明。

---

## 一、准备：环境变量配置（.env）

### 1.1 复制环境变量示例

```bash
cd <项目目录>
cp .env.example .env
```

### 1.2 核心配置项（必改，修改位置：`.env`）

| 变量 | 默认值 | 现场必改项 | 说明 |
|------|--------|-----------|------|
| `WVP_ENABLED` | `false` | `true` | 启用 WVP 对接（**必须开启**） |
| `WVP_API_URL` | `http://wvp:18080` | **甲方提供的 WVP 地址** | 格式 `http://<甲方WVP-IP>:<端口>`，如 `http://192.168.1.200:18080` |
| `WVP_USERNAME` | `admin` | 按甲方填写 | WVP 管理账号 |
| `WVP_PASSWORD` | `admin` | 按甲方填写 | WVP 管理密码 |

### 1.3 可选调优项（修改位置：`.env`）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `WVP_SYNC_INTERVAL` | `30` | 设备同步间隔（秒），现场验收可保持默认 |
| `WVP_PLAY_PROTOCOL` | `flv` | AI 拉流协议（flv/rtsp） |
| `WVP_STREAM_SUB` | `true` | 拉子码流降低推理压力 |

> 其余变量（REDIS_URL、YOLO_MODEL 等）保持默认即可，无需修改。

### 1.4 后端参数配置全览（完整参考）

> ⚠️ **重要说明**：环境变量有 `app/common/config.py` 中定义的全部参数，但 [`.env.example`](file:///home/zbnyai/DT/.env.example) **仅列出 18 个常用项**（WVP 对接、服务通信、YOLO 基础等）。**未列出的参数不写入 .env 也完全可用**——`pydantic-settings` 会自动取代码内置默认值。
>
> 需要调整某个未列参数（如预测间隔、警力权重、黑屏/花屏阈值）时的方式：
> 1. **在 `.env` 中自行新增对应键值**（格式 `变量名=值`，变量名见下表），然后重启对应服务；
> 2. 键名必须与下表/代码中完全一致（`pydantic-settings` 自动映射为大写环境变量名）。
>
> 现场联调通常只需关注 **WVP 对接**、**AI 推理** 与 **时序预测** 三组，其余保持默认。

> **修改位置总览**：环境变量全部写在后端根目录 **[`.env`](file:///home/zbnyai/DT/.env)**（`.env.example` 是它的模板）；**告警规则** 在 **[`configs/rules.yaml`](file:///home/zbnyai/DT/configs/rules.yaml)**；**跟踪器参数** 在 **[`configs/bytetrack.yaml`](file:///home/zbnyai/DT/configs/bytetrack.yaml)**。

#### ① 数据存储与端口（修改位置：`.env`）

> 下表参数**均已在 `.env.example` 中**，直接改即可。

| 变量 | 默认值 | 说明 | 现场建议 |
|------|--------|------|---------|
| `REDIS_URL` | `redis://redis:6379/0` | Redis 连接地址（compose 内网互通） | 保持默认 |
| `BACKEND_URL` | `http://backend:8000` | 业务后端地址（AI 推送事件用） | 保持默认 |
| `AI_SERVICE_URL` | `http://ai:8001` | AI 服务地址（后端转发注册/启停用） | 保持默认 |
| `BACKEND_PORT` | `8000` | 后端服务端口 | 保持默认（对外按需映射） |
| `AI_PORT` | `8001` | AI 服务端口 | 保持默认 |
| `REDIS_PREFIX` | `sc` | Redis key 前缀 | 保持默认 |

#### ② AI 推理参数（修改位置：`.env`，部分需自行新增；影响识别精度/算力）

> `YOLO_MODEL` / `YOLO_CONF` / `YOLO_IOU` 已在 `.env.example` 中；`TRACK_BUFFER` **不在其中**，如需调整请自行新增。

| 变量 | 默认值 | 说明 | 现场建议 |
|------|--------|------|---------|
| `YOLO_MODEL` | `models/yolo11n.pt` | YOLO11 权重路径 | 保持默认 |
| `YOLO_CONF` | `0.4` | 检测置信度阈值（越低检越多、误检越多） | 默认；低光照可降 0.3 |
| `YOLO_IOU` | `0.5` | NMS IOU 阈值 | 保持默认 |
| `TRACK_BUFFER` | `30` | 跟踪轨迹保留帧数 | 遮挡频繁可调大 |

> **关联文件**：跟踪器的详细匹配/阈值参数（`track_high_thresh`、`track_buffer`、`new_track_thresh` 等）在 **[`configs/bytetrack.yaml`](file:///home/zbnyai/DT/configs/bytetrack.yaml)** 中，修改后需 **重启 AI 服务** 生效。

#### ③ 时序预测参数（修改位置：`.env`，部分需自行新增）

> `CHRONOS_MODEL` 已在 `.env.example` 中；`PREDICTION_INTERVAL_MINUTES` / `PREDICTION_SERIES_LENGTH` / `VEHICLE_PERSON_MIN` / `VEHICLE_PERSON_MAX` **不在其中**，如需调整请自行新增。

| 变量 | 默认值 | 说明 | 现场建议 |
|------|--------|------|---------|
| `CHRONOS_MODEL` | `models` | Chronos-2 本地模型目录 | 默认；缺模型自动降级线性外推 |
| `PREDICTION_INTERVAL_MINUTES` | `15` | 预测间隔（分钟） | 默认；高峰可缩至 10 |
| `PREDICTION_SERIES_LENGTH` | `30` | 历史序列长度（区间数） | 保持默认 |
| `VEHICLE_PERSON_MIN` | `2` | 每车最少人数（车流转人流随机采样下界） | 保持默认（勿改为确定性值） |
| `VEHICLE_PERSON_MAX` | `5` | 每车最多人数（随机采样上界） | 保持默认 |

> ⚠️ `VEHICLE_PERSON_MIN/MAX` 的随机性为专门设计：真实场景每车承载人数有波动，确定性期望会低估序列方差、降低 Chronos 预测效果，请勿改为固定值。

#### ④ 警力分配参数（修改位置：`.env`，需自行新增）

> 以下 4 个参数**均不在 `.env.example` 中**，如需调整请在 `.env` 中自行新增。

| 变量 | 默认值 | 说明 | 现场建议 |
|------|--------|------|---------|
| `POLICE_DEMAND_WEIGHT_CURRENT` | `0.3` | 需求计算：当前人数权重 α | 保持默认 |
| `POLICE_DEMAND_WEIGHT_PREDICT` | `0.7` | 需求计算：预测人数权重 β | 保持默认（α+β 建议=1） |
| `POLICE_MOVEMENT_RATIO` | `0.5` | 每轮最大移动比例（占总警力） | 保持默认 |
| `POLICE_MIN_PER_REGION` | `1` | 每区域最少警力 | 保持默认 |

#### ⑤ 视频异常识别参数（修改位置：`.env`，需自行新增；黑屏/花屏）

> 以下 12 个参数**均不在 `.env.example` 中**，如需调整请在 `.env` 中自行新增。

| 变量 | 默认值 | 说明 | 现场建议 |
|------|--------|------|---------|
| `ANOMALY_CHECK_INTERVAL` | `30` | 每 N 帧检测一次（约 1s@30fps） | 默认；频繁可调大降负载 |
| `ANOMALY_CONFIRM_FRAMES` | `2` | 连续确认帧数（去抖） | 保持默认 |
| `ANOMALY_COOLDOWN_SECONDS` | `60` | 同设备同异常告警冷却（秒） | 保持默认 |
| `ANOMALY_ANALYSIS_WIDTH` | `480` | 分析帧宽度（等比缩放） | 保持默认 |
| `BLACK_SCREEN_BRIGHTNESS` | `20` | 黑屏判定：灰度均值阈值 | 保持默认 |
| `BLACK_SCREEN_RATIO` | `0.95` | 黑屏判定：近黑像素占比 | 保持默认 |
| `BLACK_PIXEL_VALUE` | `20` | 近黑像素亮度上限 | 保持默认 |
| `FLOWER_BLOCK_GRID` | `8` | 花屏分析块网格（NxN） | 保持默认 |
| `FLOWER_NOISE_STD` | `35.0` | 花屏判定：块均标准差 | 保持默认 |
| `FLOWER_UNIFORMITY` | `0.6` | 花屏判定：噪声均匀度 1-CV | 保持默认 |
| `FLOWER_CHANNEL_CORR` | `0.5` | 花屏判定：通道相关性 | 保持默认 |
| `FLOWER_TEMPORAL_DIFF` | `25.0` | 花屏判定：时域差分 | 保持默认 |

#### ⑥ 告警与安全（修改位置：`.env` + `configs/rules.yaml`）

| 变量 / 文件 | 默认值 | 说明 | 现场建议 |
|------|--------|------|---------|
| `RULES_FILE` | `configs/rules.yaml` | 告警规则文件路径 | 保持默认 |
| **`configs/rules.yaml`** | 内置 4 条实时规则 + 2 条预测规则 | **告警阈值直接在此文件修改**（`threshold`/`level`/`message` 等） | 保存即热重载生效，无需重启 |
| `CORS_ORIGINS` | `*` | 允许跨域来源（逗号分隔） | 生产建议限定前端域名，如 `https://screen.example.com` |

> 若需调整告警阈值，**编辑 [`configs/rules.yaml`](file:///home/zbnyai/DT/configs/rules.yaml)** 保存即热重载生效，无需重启（基于文件 mtime 检测）。

#### ⑦ 修改配置后的生效方式

| 配置文件路径 | 生效方式 |
|------|---------|
| **[`.env`](file:///home/zbnyai/DT/.env)**（环境变量） | 修改后需重启对应服务：`docker compose -p smartcity restart backend`（AI 相关重启 ai） |
| **[`configs/rules.yaml`](file:///home/zbnyai/DT/configs/rules.yaml)** | 热重载，保存即生效 |
| **[`configs/bytetrack.yaml`](file:///home/zbnyai/DT/configs/bytetrack.yaml)** | 需重启 AI 服务：`docker compose -p smartcity restart ai` |

---

## 二、镜像构建 / 拉取

### 2.1 构建（推荐，本项目镜像不入仓库分发）

```bash
docker build -t smart-city-platform:latest .
```

- 单镜像承载 AI 服务 + 业务后端，构建时自动适配 CPU 架构（amd64 走 CPU 版 PyTorch）。
- 国内已内置 pip/apt 清华镜像加速；若 Docker Hub 基础镜像拉取慢，可先预拉：
  ```bash
  docker pull docker.1ms.run/library/python:3.11-slim && \
  docker tag docker.1ms.run/library/python:3.11-slim python:3.11-slim
  ```

**预期结果**：构建成功，`docker images` 可见 `smart-city-platform:latest`。

**常见问题**：
| 现象 | 处理 |
|------|------|
| 构建时提示下载 `yolo11n.pt` 失败 | 网络问题，重试或配置代理后重新 `docker build`（权重校验失败会中断构建） |
| 拉取基础镜像慢 | 使用上方 `docker.1ms.run` 预拉重打标 |

### 2.2 直接拉取（若甲方/仓库已提供镜像）

```bash
docker pull <镜像仓库地址>:<tag>
docker tag <镜像仓库地址>:<tag> smart-city-platform:latest
```

---

## 三、后台服务启动（3 个核心容器）

```bash
# 启动核心三服务（ai + backend + redis）
docker compose -p smartcity up -d ai backend redis

# 查看运行状态
docker compose -p smartcity ps
```

**预期结果**：3 个容器均为 `Up` 状态（`restart: unless-stopped` 自动拉起）。

| 容器 | 端口 | 用途 |
|------|------|------|
| `ai` | 8001 | AI 分析服务 |
| `backend` | 8000 | 业务后端 |
| `redis` | 16379→6379 | 实时状态存储 |

> 测试推流容器（rtsp-server / rtsp-streamer-*）仅联调用，生产/验收现场可不启动。

**常见问题**：
| 现象 | 处理 |
|------|------|
| 容器启动后反复重启 | `docker compose -p smartcity logs <服务名>` 查看启动日志；检查 `models/yolo11n.pt` 是否存在 |
| `backend` 连不上 Redis | `docker compose -p smartcity exec redis redis-cli ping`，预期 `PONG` |
| 改过 `.env` 不生效 | 重启对应服务：`docker compose -p smartcity restart backend` |

---

## 四、后台服务拉起与基础联通检查

### 4.1 查看后台拉起情况（后端启动日志）

后端启时会依次拉起 4 个后台调度器：时序预测、离线检测、警力分配、WVP 同步。查看日志确认：

```bash
docker compose -p smartcity logs -f backend
```

**预期看到的关键日志**：
```
时序预测调度器已启动
摄像头离线检测已启动
警力分配调度器已启动
```

> WVP 设备同步日志：`WVP_ENABLED=true` 时出现 `WVP 设备同步已启动 (每 30s)`。

### 4.2 健康检查

```bash
# 后端 /health
curl -s http://localhost:8000/health
# 预期: {"status":"ok","service":"backend"}

# AI 服务 /health（含活跃设备数）
curl -s http://localhost:8001/health
# 预期: {"status":"ok","service":"ai","active_devices":0}

# 预测服务 /health（降级状态）
curl -s http://localhost:8000/api/prediction/health
# 预期: {"status":"ok", ...}  degraded 字段 true/false
```

### 4.3 网络联通检查（关键前置）

```bash
# ① 后端 -> 甲方 WVP REST API（用甲方 IP 替换）
curl -s "http://<WVP-IP>:<端口>/api/device/query/devices?page=1&count=1" | head
# 预期: 返回 JSON（WVP 登录页/设备列表接口，连通即返回数据而非连接错误）

# ② AI 服务 -> 甲方 ZLM HTTP-FLV（默认 80 端口）
curl -s -o /dev/null -w "%{http_code}" "http://<WVP-IP>:80/"
# 预期: 200/404 等 HTTP 状态码（而非 connection refused）

# ③ 后端 -> AI（转发通道）
curl -s http://localhost:8000/api/devices
# 预期: 返回设备列表 JSON
```

**常见问题**：此处不通 90% 是 `WVP_API_URL` 填错或防火墙未放行 WVP 端口（18080/HTTP-FLV 80/SIP 5060），核对甲方提供的 IP:端口。

---

## 五、WVP 设备同步

后端已启动 WVP 同步调度器（每 30s 自动同步一次）。也可手动触发：

```bash
# 手动触发一次同步
curl -X POST http://localhost:8000/api/devices/sync | python -m json.tool
# 预期: {"added": N, "started": 0, "stopped": 0, "recovered": 0, ...}
```

**预期结果**：`added` > 0，表示甲方 WVP 上的摄像头通道已同步入本地设备表（状态为 `synced`）。

**验证已同步设备**：

```bash
curl -s http://localhost:8000/api/devices | python -m json.tool
```

设备状态含义：

| 状态 | 说明 |
|------|------|
| `synced` | 已同步入表，**待配置计数线**（本阶段所有新设备应为此状态） |
| `online` | 在线运行中（配置计数线启流后） |
| `offline` | 离线 |

**常见问题**：
| 现象 | 处理 |
|------|------|
| 同步返回 `{"skipped":"wvp_disabled"}` | `.env` 中 `WVP_ENABLED` 未设为 `true`，修改后 `restart backend` |
| `added` 始终为 0 | 检查 4.3 步骤①②的 WVP/ZLM 联通性；确认摄像头已在 WVP 后台显示在线 |
| 报错 `token` / 401 | 核对 `WVP_USERNAME`/`WVP_PASSWORD` |

---

## 六、码流拉取（准备绘图画面）

`synced` 设备需先能拉到画面，才能在其上绘制计数线。先做流地址验证：

```bash
# ① 获取设备流地址（触发 WVP 点播 play/start）
curl -s http://localhost:8000/api/devices/<设备ID>/stream | python -m json.tool
# 预期: {"device_id":..., "stream_url":"http://<WVP-IP>:80/...flv", "stream_id":...}

# ② 截取一帧画面（运维工具会用，此处先验证可截帧）
curl -s http://localhost:8000/api/devices/<设备ID>/snapshot -o snapshot.jpg && file snapshot.jpg
# 预期: output image/jpeg (JPEG image data)
```

**预期结果**：能拿到 `stream_url` 且能截帧，说明码流链路（后端→WVP→ZLM→HTTP-FLV）已打通，AI 可正常拉流。

**常见问题**：
| 现象 | 处理 |
|------|------|
| `/stream` 报 503 | `WVP_ENABLED` 未开启 |
| `/stream` 报 502 "WVP 点播失败" | 设备离线或 WVP 未正常点播；确认摄像头在 WVP 后台为在线状态 |
| `/snapshot` 报 504 截帧超时 | 流未就绪（15s 超时），设备在线后重试 |

---

## 七、运维工具绘图（计数线/锚点绘制）

浏览器访问运维工具页面：

```
http://<后端服务器IP>:8000/static/device-config.html
```

页面操作步骤（页面内功能与提示见上方 hint 行）：

1. **填写后端地址**：默认 `http://localhost:8000`，跨机器访问需改为后端实际地址。
2. **同步设备**：点击「同步设备」→ 确认列表出现 `synced` 状态的设备按钮。
3. **选择设备**：点击设备按钮（仅 `synced`/`online` 可选）→ 页面自动截帧（约 3-8 秒）。
4. **画计数线**：在画面上**点击两个点**，连成一条计数线（绿线）。
5. **画锚点**：再**点击一个点**作为内侧锚点（红线虚线连接线中点）。锚点所在侧 = 内侧 = Enter 方向。
6. **选类型/方向**（可选）：
   - 类型：全部检测 / 只检车 / 只检人
   - 方向：双向计数 / 只计进入 / 只计离开（单向车道建议「只计进入」）
7. **启流计数**：点击「启流计数」提交配置并启动 AI 推理管道。

**预期结果**：页面提示 `启流成功! 设备 <ID> 已上线，AI 开始计数`，设备状态变为 `online`。

**常见问题**：
| 现象 | 处理 |
|------|------|
| 截帧失败（设备可能离线） | 确认设备在线后重试；检查 4.3 的 ZLM 联通性 |
| 页面打不开 | 确认 `backend` 容器运行、8000 端口可访问；确认 `./static` 已挂载（见 docker-compose.yml） |
| 画错了 | 点击「清除画线」重新画 |

---

## 八、启流操作（API 方式等价命令）

运维工具点击「启流计数」即调用下面的 `POST /api/devices/{id}/enable`。也可用 API 方式执行：

```bash
curl -X POST http://localhost:8000/api/devices/<设备ID>/enable \
  -H "Content-Type: application/json" \
  -d '{
    "line_coords": "0.5,0.1,0.5,0.9",
    "anchor_coords": "0.6,0.5",
    "camera_type": "vehicle",
    "roi_coords": "0.1,0.1,0.9,0.1,0.9,0.9,0.1,0.9"
  }'
# 预期: {"device_id":"...","status":"online","stream_url":"..."}
```

参考坐标说明（归一化 0-1）：

| 参数 | 含义 |
|------|------|
| `line_coords` | 计数线两端点 `x1,y1,x2,y2`（必填） |
| `anchor_coords` | 内侧锚点 `x,y`（决定 Enter/Exit 方向） |
| `camera_type` | `vehicle` / `person` / 空=全部 |
| `count_only` | `enter` / `exit` / 空=双向 |
| `roi_coords` | ROI 多边形顶点 `x1,y1,x2,y2,...`（≥3 顶点） |

**验证启流成功**：

```bash
# AI 服务已运行管道数应为 1+
curl -s http://localhost:8001/health
# 预期: active_devices: 1

# 设备状态
curl -s http://localhost:8000/api/devices | python -m json.tool
# 预期: 目标设备 status: "online"
```

**常见问题**：
| 现象 | 处理 |
|------|------|
| enable 报 400 "非 WVP 同步设备" | 该设备无 gb_device_id/gb_channel_id，手动注册设备请用 `POST /api/devices` |
| `active_devices` 仍为 0 | `docker compose -p smartcity logs ai` 查看管道启动/拉流日志 |

---

## 九、技术接口测试（验收）

### 9.1 实时统计（验证有数据产生）

```bash
curl -s http://localhost:8000/api/stats/realtime | python -m json.tool
# 预期: 返回 current_vehicles/current_persons/today_vehicle_in 等字段
```

### 9.2 越线事件历史（验证 AI 事件入库）

```bash
# 实时有车/人越线后，查询最近事件
curl -s "http://localhost:8000/api/events?limit=20" | python -m json.tool
# 预期: 出现 event_type 为 VehicleEnter/Exit 或 PersonEnter/Exit 的记录
```

### 9.3 告警列表

```bash
curl -s "http://localhost:8000/api/alerts?limit=20" | python -m json.tool
# 预期: 返回告警数组（无告警时为 []）
```

### 9.4 预测服务

```bash
# 预测健康（degraded 表示是否降级为线性外推）
curl -s http://localhost:8000/api/prediction/health | python -m json.tool

# 手动触发一次预测
curl -X POST http://localhost:8000/api/prediction/predict | python -m json.tool
# 预期: 返回 predicted_total 预测值
```

### 9.5 WebSocket 实时推送（可选，验证大屏数据通道）

```bash
# 使用 websocat 或 wscat 连接测试
websocat ws://localhost:8000/ws
# 预期: 2 秒内收到 {"type":"stats",...} 实时统计帧
```

### 9.6 接口冒烟测试（一次性回归所有 REST 端点）

服务运行中执行：

```bash
bash scripts/interface_test.sh
# 预期: 38/38 通过, 0 失败
```

**验收通过标准汇总**：
- ✅ `/health`、`/api/prediction/health` 返回 ok
- ✅ 设备已 `online`，AI `active_devices >= 1`
- ✅ `/api/stats/realtime` 有实时数据
- ✅ `/api/events` 有越线事件记录
- ✅ 冒烟测试全通过

---

## 附：常用运维命令速查

```bash
# 日志
docker compose -p smartcity logs -f backend     # 后端实时日志
docker compose -p smartcity logs -f ai          # AI 实时日志
docker compose -p smartcity logs --tail 50 backend | grep "\[告警\]"   # 过滤告警

# Redis 查看
docker compose -p smartcity exec redis redis-cli hgetall sc:realtime:current   # 实时统计
docker compose -p smartcity exec redis redis-cli lrange sc:alerts 0 9          # 最近告警
docker compose -p smartcity exec redis redis-cli lrange sc:events 0 9          # 最近事件

# 重启
docker compose -p smartcity restart backend
docker compose -p smartcity restart ai
```

---

## 附：现场排查速查表

| 现象 | 首选排查 |
|------|---------|
| WVP 同步未启动 | `WVP_ENABLED=true` 是否生效（restart backend） |
| 设备同步不上 | 4.3 的 WVP REST / ZLM 80 端口联通性 |
| 截帧失败 | 设备是否在线、ZLM 端口、WVP 能否点播 |
| 无事件数据 | AI `active_devices` 是否 >=1、设备是否 online |
| 预测 degraded | 属正常降级可用；确认 `models/` 下有 Chronos-2 模型则重启 backend 恢复 |
