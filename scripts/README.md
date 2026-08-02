# 测试脚本使用说明

## 脚本清单

| 脚本 | 用途 |
|------|------|
| `scripts/cut_video.sh` | 视频切分（从长视频截取指定时长片段） |
| `scripts/offline_test.sh` | 离线视频测试（使用 video_processor 处理本地视频） |
| `scripts/stream_test.sh` | RTSP 流测试（启动 Docker 服务进行实时流测试） |
| `scripts/anomaly_test.sh` | 视频异常识别端到端测试（黑屏 + 花屏，含合成视频生成） |
| `scripts/gen_anomaly_video.py` | 生成视频异常识别测试用合成视频（正常/黑屏/花屏段） |
| `scripts/run_video_processor.sh` | 旧版视频处理脚本（保留兼容） |
| `scripts/smoke_test.sh` | 后端冒烟测试（验证核心 API） |

---

## 1. 视频切分 (`cut_video.sh`)

从长视频中截取前 N 秒，输出到 `data/` 目录。

### 用法

```bash
./scripts/cut_video.sh <输入视频> <秒数> [输出文件名(不带扩展名)]
```

### 示例

```bash
# 截取前20秒 -> data/人流识别20s.mp4
./scripts/cut_video.sh data/人流识别.mp4 20

# 指定输出文件名
./scripts/cut_video.sh data/人流识别.mp4 20 人流识别20s
```

### 参数

| 参数 | 必填 | 说明 |
|------|:---:|------|
| 输入视频 | 是 | 视频文件路径（如 `data/人流识别.mp4`） |
| 秒数 | 是 | 截取时长（秒） |
| 输出文件名 | 否 | 不带扩展名，默认为 `原文件名+时长s` |

---

## 2. 离线视频测试 (`offline_test.sh`)

使用 `video_processor.py` 处理本地视频文件，输出事件 JSON、标注视频等。

### 用法

```bash
./scripts/offline_test.sh <视频文件> [video_processor 参数...]
```

### 默认配置

- 计数线：`0.1,0.75,0.9,0.75`（水平线，下方 1/4 处）
- 锚点：`0.5,0.9`（计数线下方）
- 跳帧：5（每 5 帧处理 1 帧）

### 示例

```bash
# 车辆视频测试（默认配置）
./scripts/offline_test.sh data/车辆识别20s.mp4

# 人流视频测试（只检测人员/非机动车）
./scripts/offline_test.sh data/人流识别20s.mp4 --camera-type person

# 逐帧处理（最精确，最慢）
./scripts/offline_test.sh data/人流识别20s.mp4 --camera-type person --frame_skip 1

# 单向计数（只计进入）
./scripts/offline_test.sh data/test_50f.mp4 --count-only enter

# 自定义计数线
./scripts/offline_test.sh data/车辆识别20s.mp4 --line "0.5,0.1,0.5,0.9" --anchor "0.9,0.5"
```

### 可选参数

| 参数 | 说明 |
|------|------|
| `--line "x1,y1,x2,y2"` | 计数线坐标（归一化 0-1，默认水平线 `0.1,0.75,0.9,0.75`） |
| `--anchor "x,y"` | 内侧锚点（归一化 0-1，默认 `0.5,0.9`） |
| `--count-only enter\|exit` | 单向计数模式 |
| `--camera-type vehicle\|person` | 摄像头类型：`vehicle`=只检测机动车，`person`=只检测人流（含非机动车） |
| `--frame_skip N` | 跳帧间隔（1=逐帧，5=每 5 帧 1 帧，默认 5） |
| `--no-annotated` | 不生成标注视频（更快） |

### 输出文件

输出到 `output/` 目录：

| 文件 | 说明 |
|------|------|
| `<camera_id>_events.json` | 越线事件数据 |
| `<camera_id>_summary.json` | 汇总报告 |
| `<camera_id>_statistics.csv` | 统计时间线 |
| `<camera_id>_annotated.mp4` | 标注视频（含计数线、轨迹） |
| `<camera_id>_alarms.json` | 告警数据 |

---

## 3. RTSP 流测试 (`stream_test.sh`)

启动完整 Docker 服务（Redis + RTSP 服务器 + FFmpeg 推流 + AI 分析 + 后端），注册设备进行实时流测试。

### 前置条件

- Docker Desktop 已启动
- 镜像 `smart-city-platform:latest` 已构建

### 用法

```bash
./scripts/stream_test.sh [模式] [选项]
```

### 模式

| 模式 | 说明 |
|------|------|
| （无参数） | 双设备（车辆 + 人流）同时测试 |
| `vehicle` | 仅车辆摄像头 |
| `person` | 仅人流摄像头 |

### 示例

```bash
# 双设备测试（车辆 + 人流）
./scripts/stream_test.sh

# 仅车辆摄像头
./scripts/stream_test.sh vehicle

# 仅人流摄像头
./scripts/stream_test.sh person

# 只计进入事件
./scripts/stream_test.sh --count-only enter

# 停止所有服务
./scripts/stream_test.sh --stop
```

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `VEHICLE_VIDEO` | `data/车辆识别20s.mp4` | 车辆视频文件 |
| `PERSON_VIDEO` | `data/人流识别20s.mp4` | 人流视频文件 |
| `LINE` | `0.1,0.75,0.9,0.75` | 计数线坐标 |
| `ANCHOR` | `0.5,0.9` | 内侧锚点 |

### 测试流程

```
1. 启动 Redis + RTSP 服务器
2. 启动 FFmpeg 推流（循环播放视频）
3. 启动 AI 分析服务 + 业务后端
4. 清理 Redis 历史数据
5. 注册设备（camera_type 自动设置）
6. 等待 25 秒后显示统计结果
```

### 测试中常用命令

```bash
# 查看实时统计
curl http://localhost:8000/api/stats/realtime

# 查看设备列表
curl http://localhost:8001/devices

# 查看 AI 服务日志
docker compose logs ai --tail 20

# 连接 WebSocket 接收实时数据
wscat -c ws://localhost:8001/ws
```

### 停止服务

```bash
./scripts/stream_test.sh --stop
# 或手动
docker compose down
```

---

## 4. 后端冒烟测试 (`smoke_test.sh`)

独立验证后端核心 API（不依赖 AI 服务）。

```bash
./scripts/smoke_test.sh
```

验证内容：
- `GET /health` - 健康检查
- `GET /api/stats/realtime` - 实时统计
- `GET /api/alerts` - 告警列表
- `POST /api/devices` - 设备注册
- `GET /api/devices` - 设备列表

---

## 5. 视频异常识别测试 (`anomaly_test.sh`)

端到端验证黑屏与花屏检测：生成含正常/黑屏/花屏段的合成视频 → Docker 运行 `video_processor` 逐帧处理 → 校验异常 JSON。

### 前置条件

- Docker 已启动且 `smart-city-platform:latest` 镜像已构建。
- 生成视频仅需宿主机 `python3 + numpy + opencv`（无需 YOLO）。

### 用法

```bash
./scripts/anomaly_test.sh                    # 默认: 生成视频 + 逐帧处理 + 校验
./scripts/anomaly_test.sh --no-annotated     # 不生成标注视频（更快）
SEGMENT=120 ./scripts/anomaly_test.sh        # 自定义每段帧数
```

### 测试流程

1. `gen_anomaly_video.py` 生成 `data/anomaly_test.mp4`（640x480 @ 25fps，450 帧，正常→黑屏→正常→花屏→正常 各 90 帧）。
2. Docker 运行 `video_processor.py --frame_skip 1`，输出 `output/anomaly_test_anomalies.json`。
3. 校验 JSON 包含 `black_screen` onset 与 `flower_screen` onset，打印 PASS/FAIL。

### 仅生成测试视频

```bash
python3 scripts/gen_anomaly_video.py --output data/anomaly_test.mp4 --segment 90
```

---

## 摄像头类型说明

| camera_type | 检测类别 | 适用场景 |
|-------------|---------|---------|
| `vehicle` | car, truck, bus | 机动车道监控 |
| `person` | person, bicycle, motorcycle | 人行道/非机动车道监控 |
| （不设置） | 全部类别 | 混合场景 |

## 计数线与方向说明

```
画面 (1280x720)
┌───────────────────────────────────┐
│                                   │
│         外侧 (offset < 0)         │
│                                   │
├─────── 计数线 y=0.75 ─────────────┤  ← 水平线在下方1/4处
│                                   │
│         内侧 (offset > 0)         │  ← 锚点 (0.5, 0.9) 在此侧
│                                   │
└───────────────────────────────────┘

方向定义:
  外侧 → 内侧 = Enter (进入)
  内侧 → 外侧 = Exit  (离开)
```
