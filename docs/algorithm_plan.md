# 智慧交管拥堵治理预警监控平台 - 算法方案

## 一、方案概述

本算法方案基于《智慧交管拥堵治理预警监控平台开发方案 (202607)》制定，涵盖 AI 分析服务层与业务后端的核心算法模块设计。所有方案均已落地实现并验证。

### 1.1 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                    AI 分析服务 (端口 8001)                    │
├─────────────────────────────────────────────────────────────┤
│  ┌──────────┐  ┌───────────┐  ┌──────────────────────┐     │
│  │ 视频流   │->│ YOLO11   │->│ BoT-SORT 跟踪        │     │
│  │ RTSP拉流 │  │ 检测      │  │ (自定义配置降ID切换)  │     │
│  └──────────┘  └──────────┘  └──────────────────────┘     │
│                                     │                       │
│                                     ▼                       │
│              ┌──────────────────────────────┐              │
│              │ 越线计数 (单线+锚点法向量方案)  │              │
│              │ - 任意角度计数线              │              │
│              │ - 内侧锚点归一化方向          │              │
│              │ - 滞回防抖 + 反向冷却 + 双向去重│              │
│              │ - ID切换过滤 + count_only     │              │
│              └──────────────┬───────────────┘              │
│                             │                              │
│              ┌──────────────▼───────────────┐              │
│              │ WebSocket 实时推送 + HTTP事件 │              │
│              └──────────────┬───────────────┘              │
└─────────────────────────────┼──────────────────────────────┘
                              │ HTTP POST /api/events
┌─────────────────────────────▼──────────────────────────────┐
│                    业务后端 (端口 8000)                      │
├─────────────────────────────────────────────────────────────┤
│  ┌──────────┐  ┌───────────┐  ┌──────────────────────┐     │
│  │ 事件接收 │->│ Redis     │->│ 告警规则引擎         │     │
│  │          │  │ 实时状态   │  │ (rules.yaml + 去重)  │     │
│  └──────────┘  └───────────┘  └──────────────────────┘     │
│                     │                                       │
│     ┌───────────────┼───────────────┐                      │
│     ▼               ▼               ▼                      │
│ ┌────────┐  ┌────────────┐  ┌──────────────┐              │
│ │ 实时   │  │ 小时聚合   │  │ Chronos      │              │
│ │ 统计   │  │ (8天TTL)  │  │ 时序预测     │              │
│ └────────┘  └────────────┘  └──────────────┘              │
└─────────────────────────────────────────────────────────────┘
```

### 1.2 核心算法清单

| 算法模块 | 算法选型 | 输入数据 | 输出结果 |
| ---- | --------------- | ---- | --------------- |
| 目标检测 | YOLO11 (yolo11n.pt) | 视频帧 | 车辆/行人检测框 |
| 目标跟踪 | BoT-SORT (自定义配置) | 检测结果 | 跟踪ID、轨迹历史、速度 |
| 越线计数 | 单计数线 + 内侧锚点 + 法向量投影 + 滞回防抖 + 双向去重 | 跟踪轨迹 | Enter/Exit 事件 |
| 实时统计 | Redis Hash 增量更新 | 事件流 | 当前数量、今日累计 |
| 时序预测 | Chronos-2 本地模型 (降级: 线性趋势外推) | 逐小时历史序列 | 未来 15/30/45/60 小时预测 |
| 告警引擎 | rules.yaml 规则匹配 + Redis 去重 | 实时统计 | 饱和告警 |

### 1.3 技术栈

| 层 | 技术 |
|----|------|
| AI 视觉 | ultralytics 8.4 (YOLO11 + BoT-SORT) + OpenCV + PyTorch |
| 时序预测 | Chronos-2 本地模型 (Chronos2Pipeline, 28M) + 线性趋势外推降级 |
| 业务后端 | FastAPI + Pydantic v2 + asyncio |
| 实时状态 | Redis (redis.asyncio) |
| 通信 | HTTPX (事件推送) + WebSocket (实时轨迹) |

***

## 二、目标检测算法（YOLO11）

### 2.1 算法选型

- **YOLO11**：Ultralytics 最新一代目标检测模型
- 预训练模型支持 COCO 数据集（含 car/truck/bus/person）
- nano 版本兼顾速度与精度，支持 GPU/CPU 自动切换

### 2.2 检测配置

配置位于 [app/common/config.py](file:///Users/bianwei/Desktop/codes/DT/app/common/config.py)，通过环境变量覆盖：

```python
yolo_model: str = "models/yolo11n.pt"
yolo_conf: float = 0.4    # 置信度阈值
yolo_iou: float = 0.5     # NMS 阈值
```

检测类别映射定义在 [app/ai/tracker.py](file:///Users/bianwei/Desktop/codes/DT/app/ai/tracker.py)：

```python
_DETECTION_CLASSES = {
    2: "car",      # 轿车
    7: "truck",    # 货车
    5: "bus",      # 公交车
    0: "person",   # 行人
}
```

### 2.3 处理流程

```
视频帧 -> YOLO11推理(conf=0.4, iou=0.5) -> NMS过滤 -> 类别筛选 -> 检测结果
```

实现位于 [app/ai/detector.py](file:///Users/bianwei/Desktop/codes/DT/app/ai/detector.py)。

***

## 三、目标跟踪算法（BoT-SORT）

### 3.1 算法选型与演进

**原方案**：ByteTrack（基于 IoU 匹配，ID 切换严重）

**实际选型**：BoT-SORT（内置 Ultralytics 8.4），通过激进调参降低 ID 切换

**选型理由**：
- ByteTrack 仅用 IoU 匹配，车辆靠近/遮挡时 ID 频繁切换
- BoT-SORT 支持更丰富的匹配机制（GMC 运动补偿、可选 ReID 外观特征）
- 即使不启用 ReID，BoT-SORT 的匹配机制也比 ByteTrack 鲁棒
- 测试验证：BoT-SORT 消除了同一 track 双向事件的 ID 切换问题

### 3.2 跟踪配置

配置文件：[configs/bytetrack.yaml](file:///Users/bianwei/Desktop/codes/DT/configs/bytetrack.yaml)

```yaml
tracker_type: botsort
track_high_thresh: 0.35    # 高置信度匹配阈值 (默认0.25, 提高减少误检)
track_low_thresh: 0.1      # 低置信度二次匹配
new_track_thresh: 0.5      # 新轨迹初始化阈值 (默认0.25, 提高减少ID分裂)
track_buffer: 60           # 丢失后保留帧数 (默认30, 提高处理遮挡)
match_thresh: 0.4          # IOU匹配阈值 (默认0.8, 降低减少漏匹配)
fuse_score: True           # 融合检测分数
gmc_method: none           # 固定摄像头无需运动补偿
with_reid: False           # ReID 外观匹配 (关闭, 无需额外模型)
```

**调参思路**：

| 参数 | 默认值 | 调整后 | 理由 |
|------|--------|--------|------|
| match_thresh | 0.8 | 0.4 | 大幅降低 IoU 匹配门槛，减少漏匹配 |
| track_buffer | 30 | 60 | 遮挡时保留轨迹更久（2秒 @30fps）|
| new_track_thresh | 0.25 | 0.5 | 更难创建新轨迹，减少 ID 分裂 |
| track_high_thresh | 0.25 | 0.35 | 只信任高置信度检测 |

### 3.3 跟踪流程

```
检测结果 -> BoT-SORT 卡尔曼预测 -> IoU匹配(match_thresh=0.4) -> 轨迹更新
     │                                                        │
     └── 低置信度二次匹配(track_low_thresh=0.1) ────────────────┘
                                                              │
                                                              ▼
                                              track_id + bbox + center + history
```

### 3.4 轨迹数据结构

跟踪器维护每个目标的轨迹历史，用于越线检测的方向判定：

```python
class Track:
    track_id: str          # 跟踪ID
    class_name: str        # 类别名
    bbox: List[float]      # [x1, y1, x2, y2]
    center: List[float]    # [(x1+x2)/2, (y1+y2)/2] 像素坐标
    confidence: float      # 检测置信度
    age: int               # 轨迹长度(帧数)
    velocity: List[float]  # 速度 [vx, vy]
    history: List[List[float]]  # 历史中心点序列 (旧->新)
```

实现位于 [app/ai/tracker.py](file:///Users/bianwei/Desktop/codes/DT/app/ai/tracker.py)，`max_history_length=50`。

### 3.5 ID 切换防御机制

除 BoT-SORT 调参外，越线计数器中增加了多层 ID 切换过滤：

1. **速度突变检测**：当前帧位移 > 3倍历史平均速度且差距 > 30像素时，判定为 ID 切换
2. **方向一致性验证**：跨线方向应与最近 5 帧的 offset 变化方向一致（矛盾幅度 > 50% 线长才拦截，避免误杀慢速来回运动）
3. **滞回防抖**：侧别反转需超过 28px 阈值，过滤线附近抖动导致的伪跨线
4. **反向跨线冷却**：同一轨迹反向事件需间隔 3 秒，过滤瞬时进出
5. **count_only 方向过滤**：已知单向流场景直接过滤反方向事件，处理跨物体 ID 复用

***

## 四、越线计数算法（单计数线 + 内侧锚点法向量方案）

### 4.1 算法设计

**核心方案**：单条计数线 + 内侧锚点 + 法向量投影，适用于**任意角度**的计数线。

#### 4.1.1 核心思想

1. 配置一条计数线 `P1->P2`（任意角度）和一个内侧锚点 `anchor`
2. 计算朝向锚点的法向量 `n_inner`，使 Enter/Exit 成为**绝对语义**
3. 所有点的侧别判定通过法向量投影 `offset` 计算
4. 跨线方向由起止 offset 异号判定，不受帧间抖动影响
5. 多层过滤：夹角过滤、投影范围、防抖距离、滞留确认、ID切换检测

#### 4.1.2 与旧方案的区别

| 维度 | 旧方案（双边线+叉积） | 新方案（单线+锚点+法向量） |
|------|---------------------|------------------------|
| 计数线 | 两条平行线（A外/B内） | 单条线 + 内侧锚点 |
| 方向判定 | 跨线先后顺序 | 法向量投影 offset 起止异号 |
| 线角度 | 仅水平/垂直 | **任意角度** |
| 方向语义 | 依赖线绘制方向 | **绝对语义**（锚点定义内侧）|
| 防抖 | 距离阈值 | 距离 + 滞留 + **滞回防抖** + **反向冷却** + ID切换过滤 |

### 4.2 配置参数

```python
# 计数线 (归一化坐标 [[x1,y1],[x2,y2]])
line_points: [[0.5, 0.1], [0.5, 0.9]]  # 默认垂直线

# 内侧锚点 (归一化坐标 [x,y])
anchor_points: [0.9, 0.5]  # 默认右侧为内侧

# 过滤参数
anti_jitter: True
min_distance_ratio: 0.02   # 距线最小距离占帧短边比例
min_distance_threshold: 20  # set_frame_size 按帧尺寸重算 (720p)
endpoint_sensitivity: 0.05  # 端点敏感区
hold_frames: 3              # 滞留确认帧数 (跨线后需在新侧连续保持)
min_motion: 2              # 最小跨线位移(像素)

# 滞回防抖 (解决线附近抖动导致方向翻转)
hysteresis_ratio: 0.04     # 占帧短边比例
hysteresis_threshold: 28   # set_frame_size 按帧尺寸重算 (720p)

# 反向跨线冷却 (防止瞬时进出)
reverse_crossing_cooldown: 3.0  # 秒, 同一轨迹反向事件最小间隔

# 计数方向
count_only: None           # None=双向, "enter"=只计Enter, "exit"=只计Exit

# 双向去重 TTL
counted_tracks_ttl: 300    # 秒, 已计数记录自动过期

# ID 切换检测
id_switch_speed_ratio: 3.0     # 速度超过历史平均 N 倍
id_switch_min_pixel: 30.0      # 速度差距至少 N 像素
id_switch_min_avg_speed: 5.0   # 历史平均速度低于此值不判
id_switch_history_window: 5    # 方向一致性验证窗口
```

### 4.3 核心数学原理

#### 4.3.1 几何量预处理（_precompute）

每帧或线变更时计算一次：

```python
# 线段向量
d = P2 - P1
line_len = |d|
d_hat = d / line_len           # 归一化线方向

# 法向 (左旋90°): n = (-dy, dx), 长度 = line_len
n = (-dy, dx)

# 用锚点归一化内侧法向
anchor_off = n · (anchor - P1)
if anchor_off < 0:
    n = -n                     # 使 n 指向锚点所在侧 (内侧)
n_inner = n                    # 内侧法向 (未归一化)
n_unit = n_inner / line_len    # 归一化法向 (用于夹角过滤)
```

#### 4.3.2 侧别判定（_offset）

点 Q 沿内侧法向的投影，**与线绘制方向无关**：

```python
def _offset(Q):
    """返回 Q 沿内侧法向的投影: >0 内侧, <0 外侧"""
    return n_inner · (Q - P1)
```

$$\text{offset}(Q) = \vec{n}_{\text{inner}} \cdot (\vec{Q} - \vec{P1})$$

#### 4.3.3 跨线检测

相邻两帧的点在异侧即发生跨线：

```python
prev_off = _offset(prev_point)
curr_off = _offset(curr_point)
crossed = prev_off * curr_off < 0   # 异号 = 跨线
```

#### 4.3.4 方向判定（起止 offset 对比）

用跨线序列的起止状态判定方向，而非单帧方向：

```python
start_off = _offset(start_pos)    # 跨线前记录的位置
end_off = _offset(curr_point)     # 当前确认位置

if start_off < 0 and end_off > 0:
    entry_exit = "enter"          # 外 -> 内
elif start_off > 0 and end_off < 0:
    entry_exit = "exit"           # 内 -> 外
```

#### 4.3.5 夹角过滤（法向量投影）

运动向量在法向方向的投影 >= min_motion，适用于任意角度计数线：

```python
v = curr_point - prev_point      # 运动向量
cross_component = v · n_unit     # 法向投影 = 跨线方向位移
if abs(cross_component) < min_motion:
    skip                         # 运动几乎平行于线, 擦边
```

#### 4.3.6 投影范围检查

跨线点必须落在线段内，不能在延长线上：

```python
t = [(Q - P1) · d] / |d|²
if not (0 <= t <= 1):
    skip                         # 跨线点在线段延长线上
```

### 4.4 状态机设计

```
                    ┌─────────────┐
                    │   TRACKING  │ ← 初始状态/跟踪中
                    └──────┬──────┘
                           │ offset 异号 (跨线检测) + 夹角/投影过滤
                           ▼
                    ┌─────────────┐
                    │  CROSSING   │ ← 待确认 (记录 start_pos + confirm_side)
                    └──────┬──────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
     |offset| < 滞回阈值   │  同侧     |offset| ≥ 滞回阈值
     (抖动, 跳过本帧)      │ (递增     (真实侧别反转)
              │            │ hold)        │
              │            │              ▼
              │            │    更新 start_pos + confirm_side
              │            │    重置 hold_count
              │            │
              │            │ 连续 hold_frames 帧同侧
              │            ▼
              │    ┌───────────────┐
              │    │ 方向判定       │
              │    │ start_off vs  │
              │    │ end_off 符号   │
              │    └───────┬───────┘
              │            │
              │    ┌───────┴───────┐
              │    │               │
              │ start_off<0,    start_off>0,
              │ end_off>0       end_off<0
              │    │               │
              │    ▼               ▼
              │ ┌──────┐      ┌──────┐
              │ │ENTER │      │ EXIT │
              │ └──┬───┘      └──┬───┘
              │    │              │
              │    └──────┬───────┘
              │           │ ① 双向去重: "track_id|direction"
              │           │ ② 反向冷却: 反向事件间隔 < 3s → 拦截
              │           │ ③ count_only: 单向模式过滤反向
              │           ▼
              │    ┌─────────────┐
              └───>│   TRACKING  │ ← 回到跟踪状态 (已计数)
                   └─────────────┘
```

### 4.5 多层过滤机制

| 层 | 过滤器 | 作用 | 代码位置 |
|----|--------|------|---------|
| 0 | ROI 多边形过滤 | 中心点不在多边形内的轨迹跳过 | _point_in_roi |
| 1 | 速度突变检测 | 过滤 BoT-SORT ID 切换导致的异常跨线 | process_tracks |
| 2 | 跨线检测 + 夹角过滤 | offset 异号 + 法向投影 >= min_motion | _angle_filter |
| 3 | 投影范围 | 排除线段延长线上的误判 | _in_segment |
| 4 | 防抖距离 | 跨线后需远离线才确认 | _is_clear_of_line |
| 5 | 端点过滤 | 连续2帧靠近端点才过滤 | _filter_endpoint_false_positive |
| 6 | **滞留确认 + 滞回防抖** | 连续 hold_frames 帧同侧; 带内抖动不触发反转 | track_hold_count + hysteresis |
| 7 | 方向一致性验证 | 跨线方向与最近5帧 offset 变化一致 (矛盾>50%线长才拦截) | process_tracks |
| 8 | **双向去重** | `"track_id\|direction"` 已计数则跳过 | counted_tracks |
| 9 | **反向跨线冷却** | 同轨迹反向事件间隔 < 3秒则拦截 | reverse_crossing_cooldown |
| 10 | **count_only 过滤** | 单向模式: 反方向事件丢弃 (不标记 counted) | count_only |

### 4.6 双向计数机制

#### 4.6.1 双向去重

允许同一轨迹来回跨线时各方向各计一次，而非永久锁定为单次计数：

```python
# 去重 key 格式: "track_id|direction"
# 例: "7|enter", "7|exit" — 同一轨迹 Enter 和 Exit 独立去重
counted_tracks: Dict[str, float]  # key -> 计数时间戳

# TTL: 300 秒自动过期, 防止内存泄漏与流重连 ID 重用漏计
counted_tracks_ttl = 300
```

| 场景 | 行为 |
|------|------|
| 同轨迹同方向再次跨线 | **去重**（key 已存在，跳过） |
| 同轨迹反方向跨线 | **允许计数**（key 不同，各计一次） |
| 300 秒后同轨迹同方向 | **允许计数**（TTL 过期，视为新轨迹） |

#### 4.6.2 滞回防抖（Hysteresis Band）

**解决的问题**：单向车流行驶时，跟踪框中心在计数线附近抖动 1-2 像素就导致侧别翻转，误产反向事件。

```
         confirm_side = +1 (inner)
              │
  ─────●──────│───────────  计数线
  滞回带       │    滞回带
  (< -28px)    │   (> +28px)
              │
  抖动区: |offset| < 28px → 跳过本帧 (不重置, 不递增)
  反转区: |offset| ≥ 28px → 真实反转, 更新 start_pos 允许反向计数
```

滞回阈值按帧短边比例计算（720p ≈ 28px），`_hysteresis_offset = hysteresis_threshold × line_len`。

**效果**：车辆单向行驶时不再误产 Exit；人流单向行走时不再误产 Enter。

#### 4.6.3 反向跨线冷却

防止线附近抖动导致的瞬时进出（如同一秒内 Enter + Exit）：

```python
reverse_crossing_cooldown = 3.0  # 秒

# 检查逻辑: 若反方向已计数且间隔 < 冷却时间, 拦截当前事件
opposite_key = f"{track_id}|{opposite_dir}"
if opposite_key in counted_tracks:
    time_since = current_time - counted_tracks[opposite_key]
    if time_since < reverse_crossing_cooldown:
        skip  # 冷却中, 拦截
```

#### 4.6.4 时间基准修复

冷却时间基于**场景时间**（视频时间 / 实时时间），而非处理时间：

```python
def process_tracks(self, track_result, camera_id="CAM001", current_time=None):
    if current_time is None:
        current_time = datetime.now().timestamp()  # 实时流默认
    # 离线处理由 video_processor 传入视频时间
```

**修复原因**：离线处理 0.2fps 时，0.6 秒视频时间 ≈ 15 秒处理时间。使用处理时间会导致冷却永远不触发，3 秒冷却形同虚设。

#### 4.6.5 count_only 方向过滤

对于已知单向流场景，直接过滤反方向事件：

| 配置 | 适用场景 | 效果 |
|------|---------|------|
| `None` | 双向人流/车流 | Enter 和 Exit 各计一次 |
| `"enter"` | 单向车流（向下行驶） | 只产 Enter，Exit 丢弃 |
| `"exit"` | 单向人流（向上行走） | 只产 Exit，Enter 丢弃 |

被 `count_only` 过滤的事件**不标记 counted**，允许后续同方向事件正常计数。

**与滞回/冷却的关系**：滞回和冷却处理线附近抖动导致的误判；`count_only` 处理 ID 切换导致的跨物体反向事件（如 person→bicycle ID 复用后产生反方向跨线）。两者互补，无法互相替代。

### 4.7 事件输出格式

```json
{
  "event_type": "VehicleEnter",
  "track_id": "3",
  "class_name": "car",
  "timestamp": "2026-07-26T12:08:15.123456",
  "camera_id": "CAM001",
  "cross_point": [640.0, 680.0],
  "cross_line": "line_a",
  "direction": "outer_to_inner",
  "confidence": 0.85
}
```

事件类型：`VehicleEnter`、`VehicleExit`、`PersonEnter`、`PersonExit`

实现位于 [app/ai/counter.py](file:///Users/bianwei/Desktop/codes/DT/app/ai/counter.py)。

***

## 五、实时统计模块

### 5.1 统计指标

| 指标名称 | Redis Key | 更新方式 |
|---------|-----------|---------|
| 当前车辆数 | `sc:realtime:current` -> `current_vehicles` | VehicleEnter +1, VehicleExit -1 |
| 当前人员数 | `sc:realtime:current` -> `current_persons` | PersonEnter +1, PersonExit -1 |
| 今日车辆进入 | `sc:realtime:daily:{YYYYMMDD}` -> `today_vehicle_in` | 只增 |
| 今日车辆离开 | `sc:realtime:daily:{YYYYMMDD}` -> `today_vehicle_out` | 只增 |
| 今日人员进入 | `sc:realtime:daily:{YYYYMMDD}` -> `today_person_in` | 只增 |
| 今日人员离开 | `sc:realtime:daily:{YYYYMMDD}` -> `today_person_out` | 只增 |
| N分钟区间 | `sc:realtime:interval:{YYYYMMDDHHmm}` | TTL=(序列长度+10)×N分钟×60秒，供Chronos-2预测 |
| 活跃设备数 | `sc:devices:active` (Set) | 设备事件时自动加入 |

### 5.2 数据更新流程

```
AI推送事件 -> POST /api/events -> apply_event()
    │
    ├── 当前数量: HINCRBY sc:realtime:current (+1/-1)
    ├── 今日累计: HINCRBY sc:realtime:daily:{date} (+1)
    ├── N分钟区间: HINCRBY sc:realtime:interval:{interval} (+1, TTL=(序列长度+10)×N分钟×60秒)
    ├── 设备活跃: SADD sc:devices:active {device_id}
    └── 负数钳位: 当前数量不允许为负
```

### 5.3 数据存储策略

```
实时状态 -> Redis Hash (持久, 无TTL)
今日累计 -> Redis Hash (按日key, 自然按天切换)
N分钟区间 -> Redis Hash (TTL=(序列长度+10)×N分钟×60秒, 供Chronos-2预测)
```

**设计决策**：不使用 MySQL 持久化事件，Redis 性能足够支撑实时统计需求。

实现位于 [app/backend/core/realtime.py](file:///Users/bianwei/Desktop/codes/DT/app/backend/core/realtime.py)。

***

## 六、时序预测模块（Chronos-2 本地模型）

### 6.1 算法选型

**选型**：Chronos-2-Small（本地模型，28M 参数，`Chronos2Pipeline`）

- 模型文件位于 `models/` 目录（`model.safetensors` + `config.json`）
- `config.json` 中 `chronos_pipeline_class: Chronos2Pipeline`，架构为 `Chronos2Model`（T5 backbone）
- 支持 13 个分位数输出（0.01~0.99），预测时沿分位数维度取均值得到点估计
- 相较 Chronos-1（T5 编解码器，token 化序列），Chronos-2 采用 patch 化输入 + 分位数回归，对单变量时序更精确

**降级方案**：chronos 库不可用或模型加载失败时，降级为线性趋势外推

### 6.2 预测配置

```python
chronos_model: str = "models"              # 本地 Chronos-2 模型目录
prediction_interval_minutes: int = 15      # N 分钟预测间隔
prediction_series_length: int = 30         # 历史序列长度 (30 个 N 分钟区间)
vehicle_person_min: int = 2                # 每车最少人数 (车流转人流)
vehicle_person_max: int = 5                # 每车最多人数 (车流转人流)
```

### 6.3 输入输出张量规格

Chronos-2 的 `predict()` 接口与 Chronos-1 不同，需 3D 输入：

```python
# 输入: (batch, n_variates, history_length)
ctx = torch.tensor([history], dtype=torch.float32).unsqueeze(0)  # (1, 1, L)

# 输出: list[torch.Tensor], 每个元素形状 [n_variates, num_quantiles, horizon]
forecast = pipeline.predict(inputs=ctx, prediction_length=horizon)

# 后处理: 沿分位数维度取均值 -> [n_variates, horizon] -> 取第0维 -> [horizon]
arr = forecast[0].cpu().numpy()
arr = arr.mean(axis=1)[0]  # 平均 13 个分位数
```

### 6.4 预测流程

```
Redis N分钟区间(30点) -> load_interval_history() -> 车流×random(2,5)转化为人流 + 人流加总
    -> Chronos-2预测下一个N分钟总人数 -> 取整 -> 缓存到Redis
```

- 定时调度：每 N 分钟执行一次，预测总人数并推送 WebSocket + 评估告警
- 实时预测：POST /api/prediction/predict（无需参数）
- 缓存查询：GET /api/prediction/latest

### 6.5 降级方案

当 chronos 库不可用或模型加载失败时，使用最近窗口的线性趋势外推：

```python
window = history[-10:]
trend = (window[-1] - window[0]) / (len(window) - 1)
forecast = max(0, window[-1] + trend)  # 预测下一个点
```

实现位于 [app/prediction/chronos_model.py](file:///Users/bianwei/Desktop/codes/DT/app/prediction/chronos_model.py)。

***

## 七、告警规则引擎

### 7.1 告警规则

配置文件：[configs/rules.yaml](file:///Users/bianwei/Desktop/codes/DT/configs/rules.yaml)

```yaml
rules:
  - id: vehicle_saturate_warning
    metric: current_vehicles
    threshold: 200
    level: warning
    message: "车辆数量接近饱和 ({value}/{threshold})"

  - id: vehicle_saturate_critical
    metric: current_vehicles
    threshold: 300
    level: critical
    message: "车辆饱和红色告警 ({value}/{threshold})"

  - id: person_saturate_warning
    metric: current_persons
    threshold: 8000
    level: warning

  - id: person_saturate_critical
    metric: current_persons
    threshold: 10000
    level: critical
```

### 7.2 告警流程

```
事件接收 -> BackgroundTask evaluate()
    │
    ├── 读取实时统计 get_stats()
    ├── 遍历规则 rules.yaml
    ├── 指标 >= 阈值?
    │   ├── 是 -> Redis SET 去重(TTL=5分钟)
    │   │        ├── 去重成功 -> 生成告警, LPUSH sc:alerts (保留1000条)
    │   │        └── 去重失败 -> 跳过
    │   └── 否 -> 跳过
```

### 7.3 告警查询

- `GET /api/alerts?limit=100`：查询最近告警（Redis List，按时间倒序）

实现位于 [app/backend/core/alerts.py](file:///Users/bianwei/Desktop/codes/DT/app/backend/core/alerts.py)。

***

## 八、服务架构与 API

### 8.1 双服务架构

| 服务 | 端口 | 职责 |
|------|------|------|
| AI 分析服务 | 8001 | 视频拉流、检测跟踪、越线计数、WebSocket推送 |
| 业务后端 | 8000 | 事件接收、实时统计、告警、时序预测、设备管理 |

### 8.2 AI 分析服务 API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| GET/POST/DELETE | `/devices` | 注册/列出/停止摄像头管道 |
| WebSocket | `/ws` | 实时推送轨迹与越线事件 |

设备注册请求体：

```json
{
  "device_id": "CAM001",
  "stream_url": "rtsp://...",
  "line": [[0.1, 0.75], [0.9, 0.75]],
  "anchor": [0.5, 0.9]
}
```

### 8.3 业务后端 API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| POST | `/api/events` | 接收 AI 推送的事件 |
| GET | `/api/stats/realtime` | 实时统计 |
| GET | `/api/alerts?limit=100` | 告警列表 |
| GET/POST/DELETE | `/api/devices` | 设备管理（含计数线+锚点配置）|
| POST | `/api/prediction/predict` | 时序预测（总人数，无需参数） |
| GET | `/api/prediction/latest` | 最近预测缓存 |

### 8.4 WebSocket 消息格式

**越线事件**：

```json
{
  "type": "crossing_event",
  "device_id": "CAM001",
  "event_type": "VehicleEnter",
  "track_id": "3",
  "class_name": "car",
  "timestamp": "2026-07-26T12:08:15",
  "cross_point": [640.0, 680.0],
  "direction": "outer_to_inner",
  "confidence": 0.85
}
```

**轨迹数据**（每帧推送）：

```json
{
  "type": "tracks",
  "device_id": "CAM001",
  "frame_id": 12345,
  "timestamp": "2026-07-26T12:08:15",
  "tracks": [
    {"track_id": "3", "class_name": "car", "bbox": [...], "center": [...], "confidence": 0.85}
  ]
}
```

***

## 九、视频处理管道

### 9.1 实时管道

```
RTSP/GB28181 视频流
    │
    ├── stream_frames() 异步拉流 (tenacity 重试, 断流自动重连)
    │
    ▼
每帧处理:
    ├── tracker.track(frame)     -> BoT-SORT 跟踪
    ├── counter.process_tracks() -> 越线计数
    ├── 事件 -> POST /api/events  -> 业务后端
    ├── 事件 -> WebSocket 推送
    └── 轨迹 -> WebSocket 推送
```

实现位于 [app/ai/pipeline.py](file:///Users/bianwei/Desktop/codes/DT/app/ai/pipeline.py)。

### 9.2 离线视频处理器

用于测试和验证算法效果：

```bash
python tool/video_processor.py \
  --video /data/video/test_20s.mp4 \
  --output /data/output \
  --camera_id CAM001 \
  --frame_skip 1 \
  --line 0.1,0.75,0.9,0.75 \
  --anchor 0.5,0.9 \
  --count-only enter
```

输出：标注视频（计数线+检测框+轨迹）、事件JSON、统计CSV、汇总报告。

实现位于 [tool/video_processor.py](file:///Users/bianwei/Desktop/codes/DT/tool/video_processor.py)。

***

## 十、部署方案

### 10.1 环境要求

```yaml
hardware:
  gpu: "NVIDIA (可选, CPU可运行)"    # GPU加速推理
  memory: 16GB

software:
  python: "3.11+"
  pytorch: "2.0+"
  redis: "7.0+"
  docker: "24+"
```

### 10.2 Docker 部署

```yaml
# docker-compose.yml
services:
  ai:          # AI 分析服务 (端口 8001)
  backend:     # 业务后端 (端口 8000)
  redis:       # Redis (端口 6379)
  ai-processor # 离线视频处理器 (测试用)
```

统一镜像 `smart-city-platform:latest`，包含 AI + 后端全部代码。

### 10.3 多摄像头扩展

```
摄像头1 ─┐
摄像头2 ──┤ AI服务实例 ─┐
摄像头3 ─┘              ├── Redis ── 业务后端
摄像头4 ─┐              │
摄像头5 ──┤ AI服务实例 ─┘
摄像头6 ─┘
```

每路摄像头独立管道，共享 Redis 状态。

***

## 十一、测试验证

### 11.1 车流验证（车辆识别20s.mp4）

测试条件：1280x720, 25fps, frame_skip=5, 水平线 y=0.75, 锚点 y=0.9, camera_type=vehicle

| 阶段 | Enter | Exit | 误判 | 说明 |
|------|:---:|:---:|------|------|
| 原始代码 | 15 | 16 | 16 个假 Exit | 线附近抖动导致方向翻转 |
| +滞回防抖 | 13 | 13 | 13 个假 Exit | 减少 3 个，但仍有大量误判 |
| +滞回+时间修复 | 13 | 13 | 13 个假 Exit | 冷却生效但间隔 >3s 无法拦截 |
| **+count_only=enter** | **9** | **0** | **0** ✅ | 单向流直接过滤反向 |

最终结果：9 VehicleEnter + 0 VehicleExit，每个轨迹只计一次，零误判。

### 11.2 人流验证（人流识别20s.mp4）

测试条件：1280x720, 25fps, frame_skip=1, 水平线 y=0.75, 锚点 y=0.9, camera_type=person

| 阶段 | Enter | Exit | 误判 | 说明 |
|------|:---:|:---:|------|------|
| 原始代码 | 3 | 4 | 3 个假 Enter | 线附近抖动 + ID 切换 |
| +滞回防抖 | 1 | 5 | 1 个假 Enter | 减少 2 个 |
| +滞回+时间修复 | 1 | 6 | 1 个假 Enter | ID 切换 (person→bicycle), 间隔 8.6s |
| **+count_only=exit** | **0** | **6** | **0** ✅ | 单向流直接过滤反向 |

最终结果：0 PersonEnter + 6 PersonExit，含行人/摩托车/自行车均正确归为人流，零误判。

### 11.3 修复效果汇总

| 指标 | 修复前 | 修复后 |
|------|:---:|:---:|
| 车流 Enter | 15 | **9** |
| 车流 Exit（误判） | 16 | **0** |
| 人流 Enter（误判） | 3 | **0** |
| 人流 Exit | 4 | **6** |
| 总误判率 | 19/31 = 61% | **0/15 = 0%** |

### 11.4 关键验证点

- ✅ 滞回防抖消除线附近抖动导致的方向翻转
- ✅ 反向冷却拦截瞬时进出（视频时间基准）
- ✅ count_only 过滤 ID 切换导致的跨物体反向事件
- ✅ 法向量投影方向判定正确（任意角度计数线）
- ✅ 每条轨迹每方向只计一次（双向去重）
- ✅ 时间基准修复保证离线/实时冷却一致

***

## 十二、风险与应对

| 风险点 | 应对措施 |
|-------|---------|
| BoT-SORT ID 切换 | 激进调参 + 速度突变检测 + 方向一致性验证 |
| 线附近抖动导致方向翻转 | **滞回防抖** (28px 阈值, 带内抖动忽略) |
| 瞬时进出 (同秒 Enter+Exit) | **反向跨线冷却** (3秒, 视频时间基准) |
| ID 复用导致跨物体反向事件 | **count_only 方向过滤** (单向流场景) |
| 离线处理冷却失效 | **时间基准修复** (传入视频时间而非处理时间) |
| 车辆遮挡 | track_buffer=60 保留轨迹 2 秒 |
| 边界徘徊误判 | 防抖距离 + 滞留确认 + 双向去重 (TTL 300s) |
| 端点误触 | 端点敏感区校验 (连续2帧靠近才过滤) |
| 线角度变化 | 法向量投影方案，任意角度通用 |
| Chronos-2 模型加载失败 | 降级为线性趋势外推 |
| Redis 单点故障 | appendonly 持久化 + 数据可重建 |
| 夜间低光照 | YOLO11 预训练模型适应性 + 可换权重 |

***

## 十三、文件索引

| 模块 | 文件 |
|------|------|
| 越线计数 | [app/ai/counter.py](file:///Users/bianwei/Desktop/codes/DT/app/ai/counter.py) |
| 目标跟踪 | [app/ai/tracker.py](file:///Users/bianwei/Desktop/codes/DT/app/ai/tracker.py) |
| 目标检测 | [app/ai/detector.py](file:///Users/bianwei/Desktop/codes/DT/app/ai/detector.py) |
| 视频拉流 | [app/ai/stream.py](file:///Users/bianwei/Desktop/codes/DT/app/ai/stream.py) |
| 处理管道 | [app/ai/pipeline.py](file:///Users/bianwei/Desktop/codes/DT/app/ai/pipeline.py) |
| AI服务入口 | [app/ai/service.py](file:///Users/bianwei/Desktop/codes/DT/app/ai/service.py) |
| 跟踪配置 | [configs/bytetrack.yaml](file:///Users/bianwei/Desktop/codes/DT/configs/bytetrack.yaml) |
| 告警规则 | [configs/rules.yaml](file:///Users/bianwei/Desktop/codes/DT/configs/rules.yaml) |
| 实时统计 | [app/backend/core/realtime.py](file:///Users/bianwei/Desktop/codes/DT/app/backend/core/realtime.py) |
| 告警引擎 | [app/backend/core/alerts.py](file:///Users/bianwei/Desktop/codes/DT/app/backend/core/alerts.py) |
| 设备管理 | [app/backend/api/devices.py](file:///Users/bianwei/Desktop/codes/DT/app/backend/api/devices.py) |
| 时序预测 | [app/prediction/chronos_model.py](file:///Users/bianwei/Desktop/codes/DT/app/prediction/chronos_model.py) |
| 离线处理器 | [tool/video_processor.py](file:///Users/bianwei/Desktop/codes/DT/tool/video_processor.py) |
| 全局配置 | [app/common/config.py](file:///Users/bianwei/Desktop/codes/DT/app/common/config.py) |
| 事件Schema | [app/schemas/events.py](file:///Users/bianwei/Desktop/codes/DT/app/schemas/events.py) |
