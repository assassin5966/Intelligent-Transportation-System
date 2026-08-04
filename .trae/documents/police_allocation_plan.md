# 警力区域分配算法方案

## Context

需求文档要求实现"警力部署模块"：根据各区域人流量预估，将 N 名警力分配到 M 个区域，并考虑当前分配状态进行最优调度。当前系统已具备逐设备的人流进出统计和 Chronos-2 总人数预测能力，但缺少按设备维度的在場人数追踪和警力调度算法。

## 算法设计

### 问题定义

- **输入**：N 名警力，M 个区域（每个区域有中心坐标 + 关联设备），各区域当前人数与预测人数，当前警力分布
- **输出**：每个区域的目标警力数 + 警力调度方案（从哪移到哪）

### 三阶段算法

#### 阶段 1：需求计算

```
per_device_crowd_i = Redis 读取 sc:realtime:device:{device_id} -> current_persons
total_predicted = Chronos-2 预测的下一个 N 分钟总人数 (已缓存)
share_i = crowd_i / Σ crowd_j                         # 各区域当前占比
predicted_i = total_predicted × share_i                # 按占比分摊预测值
demand_i = α × crowd_i + β × predicted_i               # 加权需求 (α=0.3, β=0.7)
```

- α 偏重当前状态，β 偏重预测趋势，默认 α=0.3 β=0.7（预测导向）
- 若所有区域当前人数均为 0，则 demand_i = 1（均匀分配）

#### 阶段 2：目标分配（比例分配 + 最小保障）

```
base = 1                                    # 每区域最少 1 人 (N >= M 时)
remaining = N - M
target_i = base + round(remaining × demand_i / Σ demand)

# 修正取整误差: Σ target 可能 != N
diff = N - Σ target
按 demand 降序, |diff| 个区域各 +1/-1 直到 Σ target = N
```

#### 阶段 3：调度规划（贪心最近优先）

```
surplus = {i : current_i > target_i}       # 需调出的区域
deficit = {i : current_i < target_i}       # 需调入的区域

# 计算所有 (surplus, deficit) 对的欧氏距离
moves = [(dist(i, j), i, j, min(surplus_i, deficit_j)) for i in surplus for j in deficit]
moves.sort(by distance ascending)           # 距离近的优先

# 贪心分配
for (d, from, to, count) in moves:
    actual = min(count, surplus[from], deficit[to], remaining_move_limit)
    if actual > 0:
        plan.append({from, to, actual, distance: d})
        surplus[from] -= actual
        deficit[to] -= actual

# 移动上限: 每轮最多移动 ceil(N × movement_ratio) 人 (默认 50%)
```

### 覆盖度评分

```
coverage = Σ (demand_i / max(1, target_i)) / M    # 越低越好, 理想值接近 1
efficiency = 1 - Σ(move_distance × count) / (N × max_distance)  # 移动效率
score = 0.7 × coverage + 0.3 × efficiency
```

## 实现方案

### 新增文件

| 文件 | 职责 |
|------|------|
| `app/backend/api/police.py` | 警力管理 + 分配 API 路由 |
| `app/backend/core/allocator.py` | 核心分配算法 (三阶段) |
| `app/backend/core/police_scheduler.py` | 定时调度 (每 N 分钟运行一次分配) |

### 修改文件

| 文件 | 改动 |
|------|------|
| `app/backend/core/realtime.py` | `apply_event` 中新增逐设备在場人数写入 `sc:realtime:device:{device_id}` |
| `app/backend/main.py` | 挂载 police 路由 + 启动 police_scheduler |
| `app/backend/api/ws.py` | 新增 `broadcast_police()` 推送分配方案 |
| `app/common/config.py` | 新增警力分配配置参数 |

### 数据模型 (Redis)

```
sc:police:regions              Hash   区域注册表 {region_id: json}
sc:police:region:{id}          Hash   {name, center_x, center_y, device_id, current_officers}
sc:police:total                int    总警力数 N
sc:police:plan:latest          String 最新分配方案 JSON (TTL=2×N分钟)
sc:realtime:device:{device_id} Hash   {current_persons, current_vehicles} (新增, TTL=24h)
```

### API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/police/regions` | 注册区域 `{id, name, center_x, center_y, device_id}` |
| GET | `/api/police/regions` | 区域列表（含当前人数、当前警力） |
| DELETE | `/api/police/regions/{id}` | 删除区域 |
| POST | `/api/police/total` | 设置总警力数 `{total: N}` |
| GET | `/api/police/allocation` | 当前分配状态 |
| POST | `/api/police/optimize` | 手动触发优化，返回分配方案 |
| GET | `/api/police/plan` | 获取最近一次自动分配方案 |

### 分配方案输出格式

```json
{
  "total_officers": 20,
  "regions": [
    {
      "region_id": "R1",
      "name": "北门",
      "current_crowd": 120,
      "predicted_crowd": 180,
      "demand": 156.0,
      "current_officers": 5,
      "target_officers": 7,
      "delta": 2
    }
  ],
  "movements": [
    {"from_region": "R3", "to_region": "R1", "count": 2, "distance": 350.5}
  ],
  "summary": {
    "total_movements": 4,
    "coverage_score": 0.92,
    "efficiency_score": 0.85,
    "generated_at": "2026-08-02T15:00:00+08:00"
  }
}
```

### 定时调度

- 与预测调度器同步：每 N 分钟（`prediction_interval_minutes`）运行一次
- 复用 Chronos-2 预测缓存作为输入
- 分配方案推送到 WebSocket（`type: "police_plan"`）

### 配置参数

```python
# app/common/config.py 新增
police_demand_weight_current: float = 0.3    # 需求计算中当前人数权重 α
police_demand_weight_predict: float = 0.7   # 需求计算中预测人数权重 β
police_movement_ratio: float = 0.5          # 每轮最大移动比例
police_min_per_region: int = 1              # 每区域最少警力
```

### 逐设备在場人数追踪 (realtime.py 改动)

在 `apply_event` 中新增：

```python
device_key = f"{settings.redis_prefix}:realtime:device:{device_id}"
for field, d in delta.items():
    if field in _CURRENT_FIELDS:
        pipe.hincrby(_CUR_KEY, field, d)
        pipe.hincrby(device_key, field, d)       # 新增: 逐设备追踪
pipe.expire(device_key, 24 * 3600)                # 24h TTL
```

## 验证方案

1. 注册 3 个区域（关联 3 个设备），设置总警力 20 人
2. 通过 `POST /api/events` 向不同设备注入 PersonEnter 事件，制造各区域不同人数
3. 调用 `POST /api/police/optimize` 触发分配
4. 验证：人数多的区域分配更多警力，移动方案合理
5. 再次注入事件改变各区域人数比例，验证调度方案考虑了当前分配状态（不会全部重新调配）
