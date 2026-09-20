# 设备白名单对齐操作指南 (device_info ↔ WVP 通道名)

> 现象：设备计数配置工具 (/static/device-config.html) 里没有设备。
> 原因：WVP 同步只登记「通道名与 device_info 表内名称一致」的设备，名称对不上则全部被跳过。
> 本文档给出：定位 → 取基准名 → 重写数据库 → 验证的完整流程。

## 1. 机制说明（为什么没设备）

设备入列链路：WVP 上级平台 → 后端定时同步（每 30s，[wvp_sync.py](../app/backend/core/wvp_sync.py)）→ Redis 设备表 → 配置工具。

**白名单规则**（[wvp_sync.py L154-L163](../app/backend/core/wvp_sync.py)）：

| device_info 表状态 | 同步行为 |
|---|---|
| 有数据 | 只登记 **通道名去空格后 == device_info.name** 的通道，其余跳过（不入表、不拉流） |
| 无数据 | 过滤关闭，WVP 所有在线通道全部自动入表 |

关键点：
- **比对键是名称，不是国标编号**。WVP 的 `(gb_device_id, gb_channel_id)` 与 `device_info` 表之间没有 ID 映射，完全靠通道名桥接。
- 归一化规则：只去掉英文空格 `" "`（[device_info.py normalize()](../app/common/device_info.py)）。全角空格、括号中英文差异、不可见字符都会导致不匹配。
- 入表后设备 ID 形如 `GB-{gb_device_id}-{gb_channel_id}`，初始状态 `synced`（橙色按钮），还需画计数线并「启流计数」才上线。

## 2. 第一步：拿到 WVP 实际通道名（基准数据）

三选一，推荐方法 A（最快）。

### 方法 A：从后端日志提取（跳过的通道名全在日志里）

```bash
docker compose logs backend --tail=2000 | grep "未在 device_info 注册"
# 输出形如: [WVP同步] 通道 GAJK-2648和阳南门出口路南以东85米 未在 device_info 注册, 跳过 (不参与拉流/计数)

# 直接导出成名单 (供批量导入用):
docker compose logs backend --tail=2000 \
  | grep "未在 device_info 注册" \
  | sed 's/.*通道 //; s/ 未在.*//' | sort -u > names.txt
```

### 方法 B：直接调 WVP API（绕过后端，确认 WVP 侧正常）

```bash
WVP=http://23.45.1.112:18080

# 1. 登录拿 token
TOKEN=$(curl -s -X POST $WVP/api/login -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['accessToken'])")

# 2. 设备列表（记下 online=true 的 deviceId）
curl -s -H "access-token: $TOKEN" "$WVP/api/device/query/devices?page=1&count=100"

# 3. 逐设备拉通道（响应里的 name 字段就是通道名 = 基准名）
curl -s -H "access-token: $TOKEN" \
  "$WVP/api/device/query/devices/<deviceId>/channels?page=1&count=100"
```

### 方法 C：手动触发同步看结果

```bash
curl -X POST http://localhost:8000/api/devices/sync   # 注意是 /api/devices/sync，不是 /api
```

- 返回 `{"skipped":"wvp_disabled"}` → `.env` 里 `WVP_ENABLED` 未生效，先修配置。
- 返回 `{"added":0,...}` → 全被白名单过滤，继续第 3 步。
- WVP 连不上 → 看日志 `WVP 设备列表拉取失败`，检查 `WVP_API_URL` 从容器内是否可达。

## 3. 第二步：对比 device_info 现有名称

```bash
# 后端缓存视角（推荐，所见即比对所用）
curl -s http://localhost:8000/api/device-info | python3 -m json.tool

# 或直接查 MySQL（dt_stats 库；容器名按实际部署，单机编排默认 dt-mysql）
docker exec dt-mysql mysql -uroot -proot123456 dt_stats \
  -e "SELECT id, name, category, point_id FROM device_info ORDER BY id;"
```

人工比对：`WVP通道名.replace(" ", "")` 与 `device_info.name` 必须逐字符一致。

> 注意：若 `/api/device-info` 返回 503，说明 `MYSQL_ENABLED=false` 或 MySQL 没起——此时白名单实际是关闭的，没设备就不是名称问题，转查 WVP 连通性。

## 4. 第三步：重写 device_info 表（三种方式）

### 方式 A：运维页面（少量设备，最简单）

浏览器打开 `http://<backend>:8000/static/device-info.html`：
1. 「新增设备信息」→ 设备名称**直接粘贴 WVP 通道名**（可带空格，后端保存时自动去空格）
2. 填点位分类（`城墙出入口便道监控点位` / `城墙入口车辆卡口点位` / `城墙出口车辆卡口点位`）、经纬度、区域
3. 保存——后端会自动触发一次 WVP 同步，匹配通道立即入 Redis 设备表

### 方式 B：REST API 批量导入

把第 2 步拿到的 WVP 通道名存成 `names.txt`（每行一个），循环注册：

```bash
# category 按现场实际改；含"车/卡口"建议 车辆卡口类，含"人/便道"用 便道类
while IFS= read -r name; do
  [ -z "$name" ] && continue
  curl -s -X POST http://localhost:8000/api/device-info \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"$name\",\"category\":\"城墙入口车辆卡口点位\",\"status\":\"待验证\",\"region\":\"大同古城\"}"
  echo
done < names.txt

# 再手动触发一次同步（CRUD 后其实已自动触发）
curl -X POST http://localhost:8000/api/devices/sync
```

### 方式 C：直接写 SQL（批量+补经纬度时用）

```bash
docker exec dt-mysql mysql -uroot -proot123456 dt_stats
```

```sql
-- name 是唯一键，冲突则覆盖；⚠️ 名称必须手工去掉空格后再入库
INSERT INTO device_info (name, category, status, region) VALUES
  ('WVP通道名1', '城墙入口车辆卡口点位', '待验证', '大同古城'),
  ('WVP通道名2', '城墙出口车辆卡口点位', '待验证', '大同古城')
ON DUPLICATE KEY UPDATE category=VALUES(category), status=VALUES(status);
```

SQL 写完**必须让后端重载缓存**（内存缓存不会自动感知外部改库）：

```bash
docker compose restart backend
```

> 注意：通过 API 注册会自动触发同步且即时刷缓存，SQL 直写则必须重启 backend，推荐方式 A/B。

### 两条路线二选一

- **以 WVP 通道名为准**（推荐）：在 device_info 补/改记录去匹配 WVP 现场名称——WVP 名称来自摄像头现场配置，是权威。
- **或改 WVP 侧**：在 WVP 管理后台把通道名改成本项目 device_info 里的名称（需要 WVP 管理权限，改完等下次同步）。

## 5. 第四步：验证

```bash
# 1. 同步结果应出现 added>0
curl -X POST http://localhost:8000/api/devices/sync

# 2. 设备列表应出现 GB-{dev}-{ch}, status=synced
curl -s http://localhost:8000/api/devices
```

后端日志应出现 `[WVP同步] 新增设备 GB-xxx (通道名), 待配置计数线`。

然后回到配置工具：刷新列表 → 点设备截帧 → 画计数线/ROI → 「启流计数」。

## 6. 常见问题

| 现象 | 处理 |
|---|---|
| curl `/api` 返回 `{"detail":"Not Found"}` | 路径不对。同步是 `POST /api/devices/sync`；设备列表 `GET /api/devices` |
| `/api/device-info` 返回 503 | `MYSQL_ENABLED` 未开或 MySQL 容器未启动（此时白名单关闭，问题在别处） |
| 日志全是「未注册，跳过」 | 就是名称不一致，按本文第 2~4 步对齐 |
| 名称看着一样仍不匹配 | 检查全角空格（normalize 只去英文空格）、中英文括号、首尾不可见字符；用 `cat -A names.txt` 查看隐藏字符 |
| POST 返回 400「云冈类设备暂不支持入库」 | 名称含「云冈」或 JTKK 开头的设备暂不入库（设计如此） |
| 对齐后工具里还是空 | ① 等同步周期(30s)或手动 `POST /api/devices/sync`；② 工具页点「刷新列表」；③ 确认 WVP 通道 online=true |
| docker-compose.offline.yml 部署 | 该编排没有 mysql 服务，device_info 缓存为空 → 过滤关闭，所有 WVP 在线通道都应入表；若无设备优先查 WVP 连通性（`WVP_API_URL` 是否容器内可达） |
