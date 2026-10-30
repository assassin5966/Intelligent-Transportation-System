# 实时位置数据接入 — 集成说明

> 对接规范：《实时位置数据接入规范 V1.0》（石家庄轻简科技有限公司，2026-09）
> 接入方式：Apache Pulsar 客户端消费 `persistent://dtzhjg/device/location_realtime_v1`
> 本文档说明：① Java 示例如何转 Python；② 如何接入本平台 backend；③ 前端如何调用 API。

---

## 1. 规范要点

### 1.1 数据结构（JSON 传输，消费端按字段解析）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | long | 主键唯一值 |
| `deviceId` | string | 设备编号 |
| `deviceType` | string | 设备类型：`HANDHELD_RADIO`(手台) / `BODY_CAMERA`(执法记录仪) / `MOTORCYCLE_PATROL`(铁骑) |
| `longitude` | double | 经度，**WGS84** |
| `latitude` | double | 纬度，**WGS84** |
| `speed` | float | 速度，米/秒 |
| `heading` | float | 方向，正北为 0，顺时针（仅手台/执法记录仪） |
| `altitude` | float | 海拔，米（仅手台） |
| `positioningTime` | string | 定位时间，`yyyy-MM-dd HH:mm:ss` |
| `extension` | string | 扩展信息 |

坐标统一为 WGS84，前端地图若用高德/百度需在展示层做坐标偏移转换。

### 1.2 接入配置（规范附件 config.txt）

| 项 | 值 |
|---|---|
| Pulsar 服务地址 | `pulsar://23.45.1.99:6650` |
| WebSocket 端口 | `8082` |
| 认证 Token | `eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJkdGpnemQifQ...`（JWT，接入方凭证） |
| Topic | `persistent://dtzhjg/device/location_realtime_v1` |
| 订阅名称 | `client_接入方标识`（多接入方用不同名区分） |
| 订阅类型 | `Shared` |

---

## 2. Java 示例 → Python 转换

Java 示例 `gps-pulsar-demo` 用 Spring Boot + `spring-boot-starter-pulsar`：

```java
@PulsarListener(
    topics = "persistent://dtzhjg/device/location_realtime_v1",
    subscriptionName = "client_sub_001",
    subscriptionType = SubscriptionType.Shared,
    schemaType = SchemaType.BYTES)
public void onGps(byte[] payload) {
    String json = new String(payload, StandardCharsets.UTF_8);
    System.out.println("收到 GPS 数据: " + json);
}
```

等价的**独立 Python 示例**见 `examples/gps_pulsar_consumer.py`（纯 Pulsar 消费、打印 JSON，不依赖本平台）：

```python
import pulsar
client = pulsar.Client("pulsar://23.45.1.99:6650",
                       authentication=pulsar.AuthenticationToken(TOKEN))
consumer = client.subscribe(TOPIC, SUB, consumer_type=pulsar.ConsumerType.Shared)
while True:
    msg = consumer.receive(timeout_millis=1000)
    print("收到 GPS 数据:", msg.data().decode("utf-8"))   # 等价 Java 的 UTF-8 解码
    consumer.acknowledge(msg)
```

对应表：

| Java (Spring) | Python (pulsar-client) |
|---|---|
| `@PulsarListener(topics=...)` | `client.subscribe(topic, sub, ...)` |
| `subscriptionType = Shared` | `consumer_type=pulsar.ConsumerType.Shared` |
| `schemaType = BYTES` | 默认即 BYTES，`msg.data()` 返回原始字节 |
| `new String(payload, UTF_8)` | `msg.data().decode("utf-8")` |

> 运行独立示例：`pip install pulsar-client && python examples/gps_pulsar_consumer.py`

---

## 3. 接入本平台 backend

### 3.1 新增文件

| 文件 | 作用 |
|---|---|
| `app/backend/core/gps_location.py` | Pulsar 消费核心：收消息 → 校验 → 写 Redis（最新位置）+ 发布更新频道 |
| `app/backend/api/location.py` | REST + SSE 接口：`/api/location/devices`、`/api/location/devices/{id}`、`/api/location/stream` |
| `examples/gps_pulsar_consumer.py` | 独立 Python 示例（Java→Python 转换演示） |

### 3.2 改动文件

- `app/common/config.py`：新增 `pulsar_*` / `location_*` 配置项（默认 `pulsar_enabled=false`）。
- `app/backend/main.py`：`lifespan` 中启动/停止消费者（用 `asyncio.to_thread` 包同步客户端），并 `include_router(location.router)`。
- `requirements.txt`：新增 `pulsar-client>=3.0.0`。

### 3.3 数据流

```
Pulsar(location_realtime_v1)
   │  Shared 订阅, Token 鉴权
   ▼
core/gps_location._consume_loop  (独立守护线程, 同步 pulsar 客户端)
   │  解析/校验 → LocationPoint
   ▼
Redis:
   sc:location:device:{deviceId}   = JSON(最新位置)   TTL=location_ttl_seconds
   sc:location:devices            = Set<deviceId>
   PUBLISH sc:location:updates    = 单条位置 JSON
   ▼
API:
   GET /api/location/devices        → 读 Redis 全部最新位置
   GET /api/location/devices/{id}   → 读 Redis 单设备
   GET /api/location/stream  (SSE)  → 订阅 sc:location:updates 实时推送
```

设计要点：
- **Pulsar 客户端同步运行在独立线程**，不阻塞 FastAPI 事件循环；`pulsar` 模块懒加载，未安装且 `pulsar_enabled=false` 时不影响后端启动。
- **最新位置落 Redis**，因此多 worker（`uvicorn --workers N`）下数据一致、重启不丢。
- **`pulsar_enabled=false` 为默认**，无 Pulsar 时后端照常启动，接口返回空（或 Redis 中既有数据）。

### 3.4 配置（.env 示例）

```env
# 实时位置接入 (Pulsar) — 默认关闭
PULSAR_ENABLED=true
PULSAR_SERVICE_URL=pulsar://23.45.1.99:6650
PULSAR_TOKEN=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJkdGpnemQifQ...   # 你的接入 Token
PULSAR_TOPIC=persistent://dtzhjg/device/location_realtime_v1
PULSAR_SUBSCRIPTION=client_sub_001
LOCATION_TTL_SECONDS=3600
LOCATION_ONLINE_TIMEOUT_SECONDS=120
```

> ⚠️ Token 通过 `.env` 注入，**不要写进代码/提交进 git**（本平台 `.gitignore` 已屏蔽 `.env*`）。

### 3.5 部署注意（离线环境）

`pulsar-client` 封装了 C++ 客户端，离线部署需在**构建镜像阶段**预装：

```dockerfile
# 在现有 backend 镜像基础上
RUN pip install --no-index --find-links=/offline/wheels pulsar-client
```

或把 `pulsar-client` 及其 wheel 加入离线依赖包（参考 `scripts/gen_requirements_lock.sh` 的锁包流程）。
导入已做懒加载保护：即便未安装，只要 `PULSAR_ENABLED=false` 后端也能启动。

---

## 4. API 用法（curl）

```bash
# 所有设备最新位置
curl http://localhost:8000/api/location/devices

# 按设备类型过滤（手台）
curl "http://localhost:8000/api/location/devices?deviceType=HANDHELD_RADIO"

# 单个设备
curl http://localhost:8000/api/location/devices/DEV-001

# SSE 实时推送（浏览器 EventSource / curl 均可）
curl -N http://localhost:8000/api/location/stream
```

返回示例（单个设备）：

```json
{
  "deviceId": "DEV-001",
  "deviceType": "BODY_CAMERA",
  "longitude": 113.123456,
  "latitude": 34.56789,
  "speed": 1.2,
  "heading": 45.0,
  "altitude": 80.5,
  "positioningTime": "2026-09-23 12:00:00",
  "extension": "",
  "receivedAt": "2026-09-23T04:00:00.123456+00:00",
  "online": true
}
```

- `online`：后端收到时间距当前 < `LOCATION_ONLINE_TIMEOUT_SECONDS`（默认 120s）为 `true`，前端可直接用于"在线/离线"标识。
- SSE 事件：`snapshot`（首帧，全部当前位置）+ `update`（每收到一条新位置推送该单点）。

---

## 5. 前端对接示例

前端已统一用 `API_BASE`（`import.meta.env.VITE_API_BASE || location.origin`）拼接接口。

### 5.1 轮询（最简单）

```javascript
// 每 3 秒刷新一次设备位置
async function loadLocations() {
  const res = await fetch(`${API_BASE}/api/location/devices`)
  return await res.json()   // LocationOut[]
}
setInterval(loadLocations, 3000)
```

### 5.2 实时推送（SSE，推荐用于地图打点）

```javascript
const es = new EventSource(`${API_BASE}/api/location/stream`)
es.addEventListener('snapshot', (e) => {
  const list = JSON.parse(e.data)      // 全部设备 LocationOut[]
  renderMarkers(list)                  // 在 MapPanel 地图打点
})
es.addEventListener('update', (e) => {
  const p = JSON.parse(e.data)         // 单设备 LocationOut
  updateMarker(p)                      // 更新/新增一个标记
})
```

### 5.3 地图打点（MapPanel.vue）

现有 `map-marking-system-vue/src/components/MapPanel.vue` 已加载地图瓦片。可在其基础上：
- 调 `GET /api/location/devices` 拿设备经纬度（WGS84），按 `deviceType` 用不同图标；
- 用 `online` 控制标记颜色（在线绿/离线灰）；
- 经纬度若需高德/百度底图，先做 WGS84→GCJ-02/BD-09 偏移。

---

## 6. 安全与脱敏

- **Token 入 `.env`**，`PULSAR_TOKEN` 切勿提交进 git（本仓库 `.gitignore` 已屏蔽 `.env*`）。
- 位置数据含设备实时轨迹，建议前端展示按权限控制；Redis 中位置 `TTL` 默认 1 小时，不会无限堆积。
- `pulsar_enabled` 默认 `false`：未配置 Pulsar 时接口返回空，不影响既有车流/人流监管功能。
