# Mock 数据模拟模式实现计划

## Context

GPU 服务器排查期间需要在前端验证功能，但没有真实视频流可用。需求：在 `.env` / `.env.gpu` 增加 `MOCK_ENABLED` 开关，开启后系统**不拉流、不推理**，由模拟器按真实交通规律生成车流/人流事件，走正常事件链路入 Redis/MySQL，前端全功能可见数据跳动。

用户已确认的选项：
- **覆盖全部 32 个已注册设备**（8 城门 × 车流卡口2 + 人流便道2）
- **实时随机事件流**粒度（非整分钟批量入账）
- **不生成历史数据**（只从开启时刻起累积）

## 数据规律（用户指定）

**车流日总量（按城门权重分配）**：
- 永泰东门（最繁华）：13000~14000 辆/天
- 武定西门（最少）：~9000 辆/天
- 其余 6 门：介于两者之间线性插值

**人流**：约为车流的 40%，规律相同。

**时段曲线**：夜间（0-6点）极少；上午缓慢增长；下午+晚上（14-22点）最多、增速快。

## 实现方案

模拟器放在 **AI 服务侧**（`app/ai/mock_simulator.py` 新文件），因为：
- 事件推送路径 `POST {BACKEND_URL}/api/events` 的 payload 只需 `device_id/event_type/occurred_at`，AI 侧已有 httpx client 模式可复用
- 后端 `apply_event` 会自动写全部 Redis key（realtime/daily/interval/hourly/events 历史），MySQL 归档链路自动生效——零后端改动
- 职责清晰：mock 关闭时 AI 服务行为与现在完全一致

### 1. 配置：`app/common/config.py`

新增字段（仿 `wvp_enabled` L103 风格）：
```python
# ---- Mock 数据模拟 (无真实视频流时验证前后端全链路) ----
mock_enabled: bool = False  # true: AI 不拉流不推理, 由模拟器生成车流/人流事件
```

### 2. 环境变量：`.env` / `.env.gpu`

两个文件末尾各加一段（注释风格与现有一致）：
```
# === Mock 数据模拟 (无视频流环境验证前端/联调用) ===
# MOCK_ENABLED=true 时 AI 服务不拉流不推理, 按真实车流/人流规律模拟 32 个注册
# 设备的事件数据 (永泰东门 1.3-1.4万/天 递减到武定西门 ~9千/天, 人流为车流 40%);
# 事件走正常链路入 Redis/MySQL, 前端全功能可见. 正式部署务必保持 false
MOCK_ENABLED=false
```
默认 false，不影响现有部署；GPU 服务器验证时改 true + 重启容器。

### 3. 模拟器：`app/ai/mock_simulator.py`（新文件，核心）

**参数解析**：
- 从 `data/device_geo.json` 读 32 设备（`devices` dict），按 category 含"卡口"判定 vehicle、否则 person（复用 `data/device_category.json` 的 `point_type` 字段更直接）
- 城门名提取：从设备名匹配 8 城门关键词（永泰东门/永泰西门/武定东门/武定西门/和阳南门/和阳北门/清远南门/清远北门）
- 城门日总量权重：永泰东门 13500（1.3-1.4万中值），武定西门 9000，其余 6 门在 9500~13000 间用固定 dict 分配（保证可复现）
- 人流 = 同城门车流 × 0.40，均分到该门 2 个人流设备；车流均分到 2 个卡口设备

**时段权重曲线**（24 元素 list，和为 1）：
```python
_HOURLY_W = [0.005,0.004,0.003,0.003,0.004,0.008,0.015,0.03,0.045,0.05,0.05,0.055,
             0.06,0.065,0.07,0.075,0.08,0.085,0.08,0.075,0.07,0.055,0.03,0.013]
```
夜间 ~2%，上午平缓爬升，14-22 高峰（合计 ~60%）。

**事件生成循环**（每设备一个 asyncio task，统一由 `MockSimulator.start()/stop()` 管理）：
- 每设备每 tick（2~5 秒随机）计算泊松期望值 λ：
  `λ = 日总量 × 当前小时权重 × tick秒数/3600 × jitter(0.5~1.5)`
- 用 `random` 或小 λ 泊松近似产生 0~N 个事件；对每个事件随机 `occurred_at`（tick 内随机偏移）、随机 enter/exit（人流双向 enter:exit ≈ 55:45 模拟净流入；车流按设备 entrance_type：入口设备 enter 为主，出口设备 exit 为主）
- 事件类型：vehicle → `VehicleEnter/VehicleExit`；person → `PersonEnter/PersonExit`
- 推送：`httpx.AsyncClient` 直接 `POST {settings.backend_url}/api/events`，body 为 `{"device_id": ..., "event_type": ..., "occurred_at": ISO时间}`（与 `EventIn` schema 一致，参考 `pipeline.py:309-324` 的 `_push`）
- 失败仅记日志不重试（与 outbox 策略一致，周期性数据下一 tick 覆盖）

**心跳**：mock 模式下每个模拟设备也需定期 `POST /api/devices/{id}/heartbeat`（否则前端显示离线）。复用 pipeline 心跳逻辑（30 秒一次），在模拟 task 内顺带执行。

### 4. AI 服务接线：`app/ai/service.py`

在 `lifespan`（L23-29）中：
- 启动时：`if settings.mock_enabled:` 则跳过正常流程提示，创建 `MockSimulator` 实例并 `await sim.start()`；logger.info 明确提示 "Mock 模式已开启：不拉流不推理，32 设备数据由模拟器生成"
- 关闭时：`await sim.stop()`（取消全部 task）
- mock 开启时 `/devices` 接口返回模拟设备列表（running=True），保证 `/health` 的 `active_devices` 与前端设备状态正常

**注意**：mock 模式下后端 `WVP_ENABLED` 也应置 false（.env 示例中同步注释说明），否则 WVP 同步会尝试向 AI 注册真实设备。模拟器不经过注册接口（直接内部启动），两者不冲突但语义上 mock 时不需要 WVP。

### 5. 后端无需改动

`POST /api/events` → `apply_event` 自动写：`sc:realtime:*`（当前/日累计/区间）、`sc:hourly:{dev}:{YmdH}`、`sc:hourly:all:*`、`sc:events` 历史列表；`archive_scheduler` 自动归档 MySQL。前端 WebSocket `/ws` 的 stats 推送自动带出。全链路零改动。

## 文件清单

| 文件 | 动作 | 内容 |
|---|---|---|
| `app/common/config.py` | 修改 | +2 行：`mock_enabled` 字段 |
| `.env` | 修改 | +MOCK_ENABLED=false 段落 |
| `.env.gpu` | 修改 | +MOCK_ENABLED=false 段落 |
| `app/ai/mock_simulator.py` | 新建 | 模拟器（~200 行） |
| `app/ai/service.py` | 修改 | lifespan 中接线 mock 开关 |

## 验证

1. 语法：`python3 -m py_compile app/ai/mock_simulator.py app/ai/service.py`
2. 本机起 Redis + backend（或 docker compose up backend redis mysql），`.env` 临时置 `MOCK_ENABLED=true`，启动 `python -m app.ai.service`
3. 观察日志：模拟器启动提示、周期事件推送
4. `curl http://localhost:8000/api/stats/realtime`：today_* 持续增长
5. `curl http://localhost:8000/api/events?limit=5`：occurred_at 为当前时间
6. 前端页面：今日累计/最近事件/小时累计跳动
7. 验证完把 `.env` 改回 `MOCK_ENABLED=false`
