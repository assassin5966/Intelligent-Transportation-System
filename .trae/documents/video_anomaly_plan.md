# 视频异常识别实现方案（黑屏 + 花屏）

> 目标：在视频处理流水线中实时识别两类视频异常——**黑屏**与**花屏**，复用现有告警体系（Redis 持久化 + WebSocket 推送），同时覆盖在线（`DevicePipeline`）与离线（`tool/video_processor.py`）两条处理路径。

---

## 1. 现状分析

经代码探查确认的关键集成点：

| 模块 | 文件 | 与异常识别的关系 |
|------|------|-----------------|
| 在线管道 | [pipeline.py](file:///Users/bianwei/Desktop/codes/DT/app/ai/pipeline.py) | `DevicePipeline._run()` 逐帧读流，是异常检测的挂载点；已有 `_push`（POST 越线事件到后端）与 `broadcast_ws_message`（AI WS 推送）可复用 |
| 拉流 | [stream.py](file:///Users/bianwei/Desktop/codes/DT/app/ai/stream.py) | `stream_frames` 产出原始帧，断流重连逻辑已具备 |
| 离线处理器 | [video_processor.py](file:///Users/bianwei/Desktop/codes/DT/tool/video_processor.py) | 独立组件（硬约束：必须保留），自带 `evaluate_alarms`，需并行接入异常检测并输出到 JSON |
| 告警引擎 | [core/alerts.py](file:///Users/bianwei/Desktop/codes/DT/app/backend/core/alerts.py) | `evaluate()`/`evaluate_prediction()` 为**轮询阈值型**告警；异常属**事件驱动型**，不适用 `evaluate()`，需直接注入告警流。持久化逻辑（`_ALERTS_KEY`/`_ALERT_SEQ_KEY`/`_ALERT_MAX`）可抽取复用 |
| 告警 API | [api/alerts.py](file:///Users/bianwei/Desktop/codes/DT/app/backend/api/alerts.py) | 仅有 `GET /api/alerts`；需新增 `POST /api/alerts/anomaly` 接收 AI 上报 |
| 后端 WS | [api/ws.py](file:///Users/bianwei/Desktop/codes/DT/app/backend/api/ws.py) | `broadcast_alert()` 可直接推送异常告警给前端 |
| 事件 schema | [schemas/events.py](file:///Users/bianwei/Desktop/codes/DT/app/schemas/events.py) | `EVENT_DELTA` 影响车/人计数；异常**不应**走 `/api/events`（不改变在场人数），走告警通道 |
| 配置 | [common/config.py](file:///Users/bianwei/Desktop/codes/DT/app/common/config.py) | `Settings` 类，新增异常检测阈值字段 |
| 依赖 | requirements.txt | 已有 `numpy`、`opencv-python-headless`，**无需新增依赖**（不引入 scipy） |

**关键约束/约定（来自 project_memory）**：
- `video_processor.py` 必须作为独立组件保留 → 离线路径单独集成。
- 阈值类参数应可配置（项目惯例）。
- 多层过滤去抖（参考越线计数的 hold_frames / 滞回带思路）。

---

## 2. 算法设计

### 2.1 分析帧预处理
为使阈值与分辨率无关且低开销，检测前将原始帧等比缩放到固定分析宽度（默认 480px），插值采用 `cv2.INTER_NEAREST`（**不平滑**，保留噪声特性，避免 INTER_AREA 平均化削弱花屏噪声导致漏检）。

### 2.2 黑屏检测（black_screen）
摄像头故障/信号丢失导致整帧近黑。**双条件 AND**，避免夜间合法低光场景误报：

```
gray = cvtColor(frame, GRAY)
brightness = mean(gray)                       # 整体亮度
black_ratio = count(gray < black_pixel_value) / total_pixels   # 近黑像素占比
is_black = (brightness < black_screen_brightness) AND (black_ratio > black_screen_ratio)
```

- 纯黑帧：brightness≈0、ratio≈1.0 → 命中。
- 夜间有路灯：brightness 偏高或 ratio < 0.95 → 不命中。

### 2.3 花屏检测（flower_screen）
花屏表现为随机彩色噪声（雪花点）或 MPEG 块状损坏。采用**多信号复合 AND**，单信号均易误报，复合后鲁棒：

1. **空间噪声** `spatial_noise`：将灰度图按 `NxN` 网格分块，计算每块 std，取均值。花屏每块都高噪 → `mean_block_std` 高。
2. **噪声均匀度** `uniformity = 1 - CV`，CV = `std(块std)/mean(块std)`。花屏噪声遍布全图 → 块间 std 接近 → CV 小 → uniformity 高；正常场景纹理集中在局部 → CV 大 → uniformity 低。
3. **通道相关性** `channel_corr`：计算 R-G、G-B 通道相关系数的均值绝对值。彩色雪花像素随机 → 通道去相关 → corr 低；正常场景通道高度相关 → corr 高。
4. **时域差分** `temporal_diff`：`mean(|gray - prev_gray|)`（有前一帧时）。花屏每帧随机 → 时域差分高；正常动态场景运动局部化 → 全局时域差分中等。

**判定（区分是否有前帧）**：
```
有前帧:  is_flower = spatial_noise_high AND uniform AND (decorrelated OR temporal_high)
无前帧:  is_flower = spatial_noise_high AND uniform AND decorrelated
```
- 彩色雪花：decorrelated 命中（+ temporal_high 一致）。
- 块状损坏：channel_corr 可能仍高，但 temporal_high 命中（块逐帧抖动）。
- 正常复杂静态场景（如树冠）：spatial/uniform 高，但 decorrelated=false、temporal_diff≈0 → 不命中。
- 正常繁忙动态：temporal_diff 中等、channel_corr 高 → 不命中。

**优先级**：先判黑屏，命中则直接返回（黑帧空间噪声低，不会误触花屏）。

### 2.4 去抖与状态机（AnomalyMonitor）
- **采样**：每 `anomaly_check_interval` 帧检测一次（默认 30 帧 ≈ 1s@30fps），降低 CPU。
- **连续确认** `anomaly_confirm_frames`（默认 2）：连续 N 次命中才确认，过滤单帧毛刺。
- **状态机**：`normal ⇄ black_screen/flower_screen`，仅在**状态转移**时产出事件（onset 异常开始 / recovery 恢复），避免持续告警刷屏。

```
check(frame):
  if frame_counter % interval != 0: return None
  result = detector.detect(frame)            # 更新内部 _prev_gray
  if result.type is not None:
      consecutive += 1
      if consecutive >= confirm_frames and state != result.type:
          old = state; state = result.type
          return AnomalyEvent(type, phase="onset")
  else:
      consecutive = 0
      if state != "normal":
          old = state; state = "normal"
          return AnomalyEvent(old, phase="recovery")
  return None
```

**双层去重**（参考越线计数多层过滤惯例）：
- 管道层：`confirm_frames` 连续确认 + 状态转移（仅报 onset/recovery）。
- 后端层：`anomaly_cooldown_seconds` Redis 去重（兜底，防管道重启重复上报）。

---

## 3. 改动清单（逐文件）

### 3.1 新增 `app/ai/anomaly.py`（核心模块）
- `AnomalyResult`（dataclass）：`anomaly_type: Optional[str]`、`scores: dict`。
- `AnomalyEvent`（dataclass）：`anomaly_type`、`phase`（onset/recovery）、`scores`、`timestamp`。
- `AnomalyDetector`：持 `_prev_gray`；`detect(frame) -> AnomalyResult`。实现 §2.1–2.3。构造参数从 `settings` 读默认，允许测试覆盖。
- `AnomalyMonitor`：持 `AnomalyDetector`；`check(frame) -> Optional[AnomalyEvent]`。实现 §2.4 采样/去抖/状态机。构造从 `settings` 读默认。
- 纯 numpy/opencv，无新依赖。

### 3.2 修改 `app/common/config.py`
在 `Settings` 增加「视频异常检测」分组（12 个字段）：
```python
# ---- 视频异常检测 ----
anomaly_check_interval: int = 30        # 每 N 帧检测一次 (≈1s@30fps)
anomaly_confirm_frames: int = 2         # 连续确认帧数 (去抖)
anomaly_cooldown_seconds: int = 60      # 同设备同异常告警冷却 (后端去重, 秒)
anomaly_analysis_width: int = 480       # 分析帧宽度 (等比缩放, 阈值稳定)
black_screen_brightness: int = 20       # 灰度均值 < 此值 (黑屏条件1)
black_screen_ratio: float = 0.95        # 近黑像素占比 > 此值 (黑屏条件2)
black_pixel_value: int = 20             # 近黑像素亮度上限
flower_block_grid: int = 8              # 花屏分析块网格 (NxN)
flower_noise_std: float = 35.0          # 块均标准差 > 此值 (花屏: 高噪)
flower_uniformity: float = 0.6          # 噪声均匀度 1-CV > 此值 (花屏: 均匀)
flower_channel_corr: float = 0.5        # 通道相关性 < 此值 (花屏: 去相关)
flower_temporal_diff: float = 25.0      # 时域差分 > 此值 (花屏: 时域高噪, 有前帧时)
```

### 3.3 修改 `app/ai/pipeline.py`（在线集成）
- `DevicePipeline.__init__`：创建 `self._anomaly_monitor = AnomalyMonitor()`。
- `_run()` 帧循环中（`frame is not None` 分支内，跟踪之前）调用：
  ```python
  ev = self._anomaly_monitor.check(frame)
  if ev is not None:
      ev.timestamp = datetime.now().isoformat()
      await self._handle_anomaly(ev)
  ```
- 新增 `_handle_anomaly(ev)`：
  - AI WS 广播 `{"type": "video_anomaly", "device_id", "anomaly_type", "phase", "scores", "timestamp"}`。
  - POST 到后端 `POST {backend_url}/api/alerts/anomaly`，payload `{device_id, anomaly_type, phase, scores}`。
  - 失败仅记日志，不阻塞视频处理（与 `_push` 一致）。

### 3.4 修改 `app/backend/core/alerts.py`（抽取持久化 helper）
- 新增 `async def persist_alert(alert: dict) -> dict`：封装 `incr(_ALERT_SEQ_KEY)` → 赋 `id` → `lpush(_ALERTS_KEY)` → `ltrim`。返回带 id 的 alert。
- 重构 `evaluate()` 与 `evaluate_prediction()` 内的持久化段调用 `persist_alert`（消除重复，零行为变更）。

### 3.5 修改 `app/backend/api/alerts.py`（新增上报端点）
- `AnomalyAlertIn`（BaseModel）：`device_id: str`、`anomaly_type: str`（black_screen/flower_screen）、`phase: str = "onset"`、`scores: Optional[dict] = None`。
- `POST /api/alerts/anomaly`：
  1. Redis 去重 key `{prefix}:alert:anomaly:{device_id}:{anomaly_type}:{phase}`，`SET ... EX anomaly_cooldown_seconds NX`；未获取 → 返回 `{"status":"deduplicated"}`。
  2. 组装 alert：`rule_id=f"video_{anomaly_type}"`、`level="critical" if onset else "info"`、`category="video_anomaly"`、`message`（中文描述）、`anomaly_type/device_id/phase/scores`、`created_at`。
  3. `await persist_alert(alert)`。
  4. `await broadcast_alert(alert)`（后端 WS）。
  5. 返回 `{"status":"ok","alert":alert}`。
- 前端经现有 `GET /api/alerts` 与后端 `/ws` 即可看到异常告警，**无需前端改动**。

### 3.6 修改 `tool/video_processor.py`（离线集成）
- 顶部 `from app.ai.anomaly import AnomalyMonitor`。
- 新增 `--no-anomaly` 参数（默认开启异常检测），构造 `anomaly_monitor = AnomalyMonitor() if not args.no_anomaly else None`。
- 帧循环中（`processed_idx` 递增后）：
  ```python
  if anomaly_monitor is not None:
      ev = anomaly_monitor.check(frame)
      if ev is not None:
          ev.timestamp = current_video_time.isoformat()
          all_anomalies.append({...})   # 异常事件落 JSON
          print(f"  [异常] {ev.anomaly_type} {ev.phase}")
  ```
- `all_anomalies` 列表初始化；保存到 `{video_name}_anomalies.json`。
- `summary["totals"]` 增加 `anomaly_onset`/`anomaly_recovery` 计数。
- `draw_annotations` 在异常状态时叠加红色边框 + "ANOMALY: <type>" 文字（仅当 monitor 当前处于异常态时）。

### 3.7 新增 `tests/test_anomaly.py`（单元测试，pytest 风格）
合成帧验证（不依赖真实视频）：
- 全黑帧 → `detect` 返回 black_screen。
- `np.random.randint` 随机噪声帧 → 返回 flower_screen。
- 渐变/真实纹理帧 → 返回 None。
- `AnomalyMonitor`：连续 `confirm_frames` 次噪声帧后才 onset；清除后 recovery。
- 用 `monkeypatch` 覆盖阈值，确保可独立运行（无 Redis/模型依赖）。

### 3.8 新增 `scripts/gen_anomaly_video.py` + `scripts/anomaly_test.sh`（端到端）
- `gen_anomaly_video.py`：用 `cv2.VideoWriter` 生成合成测试视频：60 正常帧（彩色渐变+几何图形）→ 30 黑帧 → 30 正常 → 30 随机噪声帧 → 30 正常，25fps，输出 `data/anomaly_test.mp4`。
- `anomaly_test.sh`：调用生成脚本 → 运行 `video_processor.py --video data/anomaly_test.mp4 --frame_skip 1` → 校验 `*_anomalies.json` 含 black_screen onset 与 flower_screen onset → 打印结果。延续 `scripts/` 现有 shell 脚本风格。

### 3.9 文档更新
- [.trae/documents/api.md](file:///Users/bianwei/Desktop/codes/DT/.trae/documents/api.md)：§6 告警 API 增加 `POST /api/alerts/anomaly`；§9 WebSocket 增加 `video_anomaly` 消息类型；§10 数据模型补 anomaly 字段。
- [.trae/documents/algorithm_plan.md](file:///Users/bianwei/Desktop/codes/DT/.trae/documents/algorithm_plan.md)：新增「视频异常识别」章节（算法原理 + 阈值 + 去抖状态机 + 双路径集成）。
- [scripts/README.md](file:///Users/bianwei/Desktop/codes/DT/scripts/README.md)：补充 `anomaly_test.sh` 用法。

---

## 4. 假设与决策

1. **异常走告警通道，不走 `/api/events`**：异常不改变车/人在场计数，且 `EVENT_DELTA` 无对应映射；复用告警体系（Redis List + WS）更自然，前端零改动。
2. **onset=critical、recovery=info 均持久化**：告警日志完整呈现异常生命周期，便于审计；recovery 同样走 `POST /api/alerts/anomaly`（phase=recovery），后端 cooldown 去重。
3. **在线+离线均集成**：遵循「video_processor 保留为独立组件」硬约束；两条路径共用 `AnomalyMonitor`，行为一致。
4. **阈值默认值经合成帧验证可调**：所有阈值集中在 `Settings`，可通过环境变量/.env 覆盖；默认值面向 480px 分析宽度标定。
5. **不引入新依赖**：仅用 numpy + opencv（已存在）。
6. **INTER_NEAREST 缩放**：保留噪声特性，避免平均化导致花屏漏检。
7. **不实现「画面冻结」检测**：超出本次需求（仅黑屏+花屏）；状态机预留扩展位。

---

## 5. 验证步骤

1. **单元测试**：`python -m pytest tests/test_anomaly.py -v`（或直接 `python tests/test_anomaly.py`），确认黑屏/花屏/正常三类帧判定正确，monitor 去抖与状态转移正确。
2. **离线端到端**：`bash scripts/anomaly_test.sh`，确认 `anomaly_test.mp4` 的黑屏段与噪声段均被检出 onset，恢复段产出 recovery，正常段无事件。
3. **在线集成**：用 FFmpeg 模拟 RTSP 流（参考 `scripts/stream_test.sh`）注册设备，注入黑屏/噪声段，确认：
   - AI WS 收到 `video_anomaly` 消息；
   - `GET /api/alerts` 出现 `category=video_anomaly` 告警；
   - 后端 `/ws` 收到 alert 推送；
   - 冷却期内重复 onset 被去重。
4. **回归**：对 `data/车辆识别20s.mp4` 跑 `video_processor.py`，确认正常视频不产生异常误报，且越线计数/告警原有行为不变。
5. **性能**：确认 `anomaly_check_interval=30` 下检测开销可忽略（单次 <5ms，1s 一次）。

---

## 6. 实施顺序

1. `app/common/config.py` 增配置字段。
2. `app/ai/anomaly.py` 核心模块（detector + monitor）。
3. `tests/test_anomaly.py` 单元测试（先验证算法正确性）。
4. `app/backend/core/alerts.py` 抽 `persist_alert`。
5. `app/backend/api/alerts.py` 新增 `POST /api/alerts/anomaly`。
6. `app/ai/pipeline.py` 在线集成。
7. `tool/video_processor.py` 离线集成。
8. `scripts/gen_anomaly_video.py` + `scripts/anomaly_test.sh` 端到端验证。
9. 文档更新（api.md / algorithm_plan.md / scripts/README.md）。
