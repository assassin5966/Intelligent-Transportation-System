# 后端离线生产部署方案

> 本文档面向**纯内网/离线环境**的后端部署，覆盖从镜像准备到启动验证的完整流程。
> 核心原则：**一切依赖在联网环境提前准备好，内网服务器零外网访问即可完成部署。**

---

## 一、部署架构

```
┌─────────────────────────────────────────────────────┐
│                    内网服务器                          │
│                                                       │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐          │
│  │  AI 服务  │  │  后端    │  │ 前端     │          │
│  │ (8001)   │  │ (8000)   │  │ (5173)   │          │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘          │
│       │              │              │                │
│  ┌────▼──────────────▼──────────────▼───────────┐   │
│  │              Docker 内网网络                    │   │
│  └────┬──────────────┬──────────────┬───────────┘   │
│       │              │              │                │
│  ┌────▼─────┐  ┌─────▼──────┐  ┌───▼──────────┐    │
│  │  Redis   │  │  MySQL     │  │  MediaMTX    │    │
│  │ (6379)   │  │ (3306)     │  │ (8554/8888)  │    │
│  └──────────┘  └────────────┘  └──────────────┘    │
│                                                       │
│  外部 WVP/ZLM ◄──── 后端通过 WVP API 对接              │
│  (甲方部署)        需内网连通 WVP 的 IP:端口            │
└─────────────────────────────────────────────────────┘
```

**核心服务清单（4 个必选 + 3 个可选）：**

| 服务 | 必选 | 说明 |
|------|------|------|
| `backend` | 是 | FastAPI 业务后端，提供 REST/WS/统计/告警/预测 |
| `ai` | 是 | YOLO11 视觉分析服务，拉流推理 |
| `redis` | 是 | 实时状态存储，所有服务依赖 |
| `mysql` | 否 | 长期归档（关闭则小时数据仅存 Redis 30 天） |
| `rtsp-server` | 否 | 测试用 RTSP 服务器，生产不需要 |
| `rtsp-streamer-*` | 否 | 测试推流，生产不需要 |
| `frontend` | 否 | 大屏前端，可单独部署 |

---

## 二、离线准备工作（在联网环境执行）

### 2.1 准备离线镜像包

#### 2.1.1 构建业务镜像

```bash
# 克隆/拷贝项目代码到联网构建机
cd DT

# 确保模型文件存在
ls -la models/yolo11n.pt          # YOLO 权重
ls -la models/                     # Chronos-2 预测模型（可选，缺失则降级线性外推）

# 构建镜像（CPU 模式）
docker build -t smart-city-platform:latest .

# 验证构建成功
docker images smart-city-platform:latest
```

> **GPU 模式**：如内网服务器有 NVIDIA GPU，构建时需指定 CUDA 源：
> ```bash
> docker build --build-arg TORCH_INDEX=https://download.pytorch.org/whl/cu121 \
>              -t smart-city-platform:latest .
> ```
> 同时取消 `docker-compose.yml` 中 `ai` 服务的 `deploy.reservations` 注释。

#### 2.1.2 导出镜像为 tar 包

```bash
# 导出业务镜像
docker save smart-city-platform:latest -o smart-city-platform.tar

# 导出依赖服务镜像（Redis 必选，MySQL 可选）
docker pull redis:7-alpine
docker save redis:7-alpine -o redis.tar

# 如需要 MySQL
docker pull mysql:8.0
docker save mysql:8.0 -o mysql.tar

# 如需要 MediaMTX（测试用）
docker pull bluenviron/mediamtx:latest
docker save bluenviron/mediamtx:latest -o mediamtx.tar

# 压缩（可选，减少传输体积）
gzip *.tar
```

#### 2.1.3 准备离线文件清单

将以下文件拷贝到 U 盘或内网可达的传输路径：

```
offline-pack/
├── images/
│   ├── smart-city-platform.tar.gz
│   ├── redis.tar.gz
│   ├── mysql.tar.gz              # 可选
│   └── mediamtx.tar.gz           # 可选
├── project-code/                  # 项目代码（含 docker-compose.yml、configs/ 等）
│   ├── docker-compose.yml
│   ├── .env.example
│   ├── configs/
│   │   ├── rules.yaml
│   │   ├── bytetrack.yaml
│   │   ├── business_rules.yaml
│   │   ├── police.yaml
│   │   └── mediamtx.yml
│   ├── models/
│   │   ├── yolo11n.pt
│   │   └── ... (Chronos-2 模型目录)
│   ├── data/
│   │   ├── device_geo.json
│   │   └── device_category.json
│   └── static/                    # 运维工具页面
│       └── device-config.html
├── deploy.sh                      # 一键部署脚本（见下文）
└── README.txt                     # 部署说明
```

---

## 三、内网服务器部署步骤

### 3.1 导入镜像

```bash
# 解压
gunzip -k *.tar.gz

# 导入所有镜像
docker load -i smart-city-platform.tar
docker load -i redis.tar
docker load -i mysql.tar           # 可选

# 验证
docker images
```

### 3.2 准备项目目录

```bash
# 在服务器上创建部署目录
mkdir -p /opt/dt
cd /opt/dt

# 将 project-code/ 下所有文件拷贝到 /opt/dt/
# （scp / U盘 / 内网共享等方式）
cp -r /path/to/offline-pack/project-code/* /opt/dt/
```

### 3.3 配置环境变量

```bash
cd /opt/dt

# 基于示例创建 .env
cp .env.example .env
```

**关键配置项（按现场情况修改）：**

| 变量 | 说明 | 现场必改 |
|------|------|---------|
| `WVP_ENABLED` | 是否对接上级 WVP | 有 WVP 设为 `true`，否则 `false` |
| `WVP_API_URL` | 上级 WVP 地址 | 必改，如 `http://192.168.1.200:18080` |
| `WVP_USERNAME` / `WVP_PASSWORD` | WVP 账号密码 | 按甲方提供 |
| `MYSQL_ENABLED` | 是否启用 MySQL 归档 | 无 MySQL 设为 `false` |
| `CORS_ORIGINS` | 跨域白名单 | 生产建议限定前端域名 |
| `ZLM_PUBLIC_BASE` | ZLM 对外地址 | 前端播放 FLV 需要时设置 |

**不需要改的项（保持默认即可）：**

- `REDIS_URL`、`BACKEND_URL`、`AI_SERVICE_URL` → 容器内网通信，无需修改
- `YOLO_MODEL`、`YOLO_CONF`、`YOLO_IOU` → 推理参数，默认即可
- `CHRONOS_MODEL` → 模型目录，保持默认

### 3.4 启动服务

```bash
cd /opt/dt

# 方式一：启动全部（含 MySQL）
docker compose -p smartcity up -d

# 方式二：仅启动核心（无 MySQL，推荐内网部署）
# 先修改 .env 中 MYSQL_ENABLED=false
docker compose -p smartcity up -d ai backend redis

# 查看服务状态
docker compose -p smartcity ps
```

**预期结果：** 所有容器均为 `Up` 状态。

### 3.5 验证部署

```bash
# 1. 后端健康检查
curl -s http://localhost:8000/health
# 预期: {"status":"ok","service":"backend"}

# 2. AI 服务健康检查
curl -s http://localhost:8001/health
# 预期: {"status":"ok","service":"ai","active_devices":0}

# 3. 预测服务健康检查
curl -s http://localhost:8000/api/prediction/health
# 预期: {"status":"ok",...}

# 4. 查看后端启动日志（确认调度器均已启动）
docker compose -p smartcity logs backend | grep -E "调度器|已启动"
# 预期看到: 时序预测调度器已启动 / 摄像头离线检测已启动 / 警力分配调度器已启动
```

---

## 四、对接外部 WVP（生产必配）

> 只有 WVP 对接成功，AI 才能拉取摄像头视频流进行计数。

### 4.1 网络连通性确认

```bash
# 从后端容器内测试 WVP 可达性
docker compose -p smartcity exec backend -- curl -s -o /dev/null -w "%{http_code}" http://<WVP-IP>:18080/
# 预期: 200（或 302/404 等 HTTP 状态码，非 connection refused）
```

### 4.2 配置 WVP 参数

编辑 `.env`，确保以下值正确：

```ini
WVP_ENABLED=true
WVP_API_URL=http://<实际WVP-IP>:18080
WVP_USERNAME=admin
WVP_PASSWORD=<实际密码>
```

修改后重启后端：

```bash
docker compose -p smartcity restart backend
```

### 4.3 验证设备同步

```bash
# 等待自动同步（每 30s），或手动触发
curl -X POST http://localhost:8000/api/devices/sync

# 查看设备列表
curl -s http://localhost:8000/api/devices
# 预期: added > 0，设备状态为 synced
```

---

## 五、运维管理

### 5.1 常用命令

```bash
# 查看日志
docker compose -p smartcity logs -f backend     # 后端实时日志
docker compose -p smartcity logs -f ai          # AI 实时日志
docker compose -p smartcity logs --tail 50 backend | grep "\[告警\]"

# 重启服务（修改 .env 后必须重启）
docker compose -p smartcity restart backend
docker compose -p smartcity restart ai

# 进入容器排查
docker compose -p smartcity exec backend sh
docker compose -p smartcity exec redis redis-cli

# 停止所有服务
docker compose -p smartcity down
```

### 5.2 数据查看

```bash
# Redis 实时统计
docker compose -p smartcity exec redis redis-cli hgetall sc:realtime:current

# 最近事件
docker compose -p smartcity exec redis redis-cli lrange sc:events 0 9

# 最近告警
docker compose -p smartcity exec redis redis-cli lrange sc:alerts 0 9
```

### 5.3 告警规则热重载

修改 `configs/rules.yaml` 后**无需重启**，后端自动检测文件变更并热重载：

```bash
vi configs/rules.yaml     # 修改阈值/级别/消息内容
# 保存即生效
```

### 5.4 配置文件修改后生效方式

| 文件 | 生效方式 |
|------|---------|
| `.env` | 需重启对应服务 |
| `configs/rules.yaml` | 热重载，保存即生效 |
| `configs/bytetrack.yaml` | 需重启 AI 服务 |
| `configs/business_rules.yaml` | 热重载，保存即生效 |

---

## 六、生产环境优化建议

### 6.1 资源限制

在 `docker-compose.yml` 中为各服务添加资源限制，防止单服务抢占全部资源：

```yaml
services:
  ai:
    deploy:
      resources:
        limits:
          cpus: '4'           # AI 服务最多使用 4 核
          memory: 4G          # 最多使用 4GB 内存
        reservations:
          cpus: '2'           # 预留 2 核
          memory: 2G          # 预留 2GB 内存

  backend:
    deploy:
      resources:
        limits:
          cpus: '2'
          memory: 2G

  redis:
    deploy:
      resources:
        limits:
          memory: 1G
```

### 6.2 日志轮转

Docker 容器日志默认无限增长，建议限制：

```bash
# 全局限制（对所有容器生效）
# 编辑 /etc/docker/daemon.json
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  }
}
# 重启 Docker 使配置生效
sudo systemctl restart docker
```

### 6.3 数据持久化备份

```bash
# Redis 数据（AOF 文件）
# 默认位于 docker volume redis-data，可通过 docker volume inspect 查看实际路径
docker volume inspect dt_redis-data

# MySQL 数据（如需）
docker volume inspect dt_mysql-data

# 建议定期备份：
# tar -czf /backup/redis-data-$(date +%Y%m%d).tar.gz /var/lib/docker/volumes/dt_redis-data/_data
```

### 6.4 安全加固

```bash
# 1. CORS 限定前端域名
# 在 .env 中设置：
CORS_ORIGINS=https://screen.example.com

# 2. 关闭不必要的端口映射
# 如内网访问无需映射 Redis（16379）和 MySQL（3307），可删除 ports 配置
# 仅保留前端需要的端口（如 8000、5173）

# 3. 使用非 root 用户运行容器（需修改 Dockerfile）
```

---

## 七、一键部署脚本

> 将以下脚本保存为 `offline-pack/deploy.sh`，内网服务器上执行即可完成部署。

```bash
#!/bin/bash
# ============================================================
# 离线部署脚本 — 导入镜像 → 准备目录 → 配置 → 启动
# 用法: bash deploy.sh [--no-mysql]
# ============================================================
set -e

PROJECT_DIR="/opt/dt"
MYSQL_ENABLED=true

# 解析参数
for arg in "$@"; do
  case "$arg" in
    --no-mysql) MYSQL_ENABLED=false ;;
  esac
done

echo "[1/5] 导入 Docker 镜像..."
for tar in images/*.tar; do
  [ -f "$tar" ] && docker load -i "$tar" && echo "  已导入: $tar"
done
for gz in images/*.tar.gz; do
  [ -f "$gz" ] && gunzip -k "$gz" && docker load -i "${gz%.gz}" && echo "  已导入: $gz"
done

echo "[2/5] 准备项目目录..."
mkdir -p "$PROJECT_DIR"
# 此处假设脚本在 offline-pack/ 目录下执行
cp -r project-code/* "$PROJECT_DIR/"

echo "[3/5] 配置环境变量..."
cd "$PROJECT_DIR"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "  已创建 .env，请根据现场情况修改:"
  echo "    - WVP_API_URL"
  echo "    - WVP_USERNAME / WVP_PASSWORD"
  echo "    - MYSQL_ENABLED=$MYSQL_ENABLED"
fi

# 设置 MySQL 开关
if [ "$MYSQL_ENABLED" = false ]; then
  sed -i 's/MYSQL_ENABLED=true/MYSQL_ENABLED=false/' .env
fi

echo "[4/5] 启动服务..."
if [ "$MYSQL_ENABLED" = true ]; then
  docker compose -p smartcity up -d
else
  docker compose -p smartcity up -d ai backend redis
fi

echo "[5/5] 验证部署..."
sleep 5
echo ""
echo "===== 服务状态 ====="
docker compose -p smartcity ps

echo ""
echo "===== 健康检查 ====="
curl -s http://localhost:8000/health
echo ""
curl -s http://localhost:8001/health
echo ""

echo ""
echo "========================================"
echo " 部署完成！"
echo "========================================"
echo ""
echo "  后端 API:   http://localhost:8000"
echo "  AI 服务:    http://localhost:8001"
echo "  运维工具:   http://localhost:8000/static/device-config.html"
echo ""
echo "  查看日志:   docker compose -p smartcity logs -f backend"
echo "  重启服务:   docker compose -p smartcity restart backend"
echo "  停止服务:   docker compose -p smartcity down"
echo ""
echo "  ⚠️ 请确认 .env 中 WVP 配置正确后重启后端："
echo "     docker compose -p smartcity restart backend"
```

---

## 八、常见问题

| 现象 | 原因 | 处理 |
|------|------|------|
| `backend` 启动后反复重启 | 缺少模型文件或 Redis 连接失败 | `docker compose -p smartcity logs backend` 查看日志 |
| AI 服务 `active_devices` 始终为 0 | 设备未启流或拉流失败 | 确认设备已在运维工具中配置计数线并启流 |
| WVP 同步返回 `skipped: wvp_disabled` | `.env` 中 `WVP_ENABLED` 未设为 `true` | 修改后 `restart backend` |
| 设备同步 `added` 始终为 0 | WVP 网络不通或账号密码错误 | 检查 WVP 联通性，核对账号密码 |
| 拉流报错/截帧超时 | 摄像头离线或 ZLM 流未就绪 | 确认摄像头在 WVP 后台在线 |
| 预测结果 `degraded: true` | Chronos-2 模型未部署 | 属正常降级，线性外推仍可用；如需完整预测，将模型放入 `models/` 目录 |
| 内网无 Chronos-2 模型 | 模型文件较大（~2GB），未包含在代码仓库中 | 预测自动降级为线性外推，不影响核心计数功能 |

---

## 九、部署清单（验收用）

| 检查项 | 预期 | 状态 |
|--------|------|------|
| `docker images` 包含 `smart-city-platform` | 镜像已导入 | ☐ |
| `docker compose -p smartcity ps` 全部 Up | 核心服务运行中 | ☐ |
| `curl localhost:8000/health` | `{"status":"ok"}` | ☐ |
| `curl localhost:8001/health` | `{"status":"ok"}` | ☐ |
| 后端日志输出调度器启动信息 | 3 个调度器已启动 | ☐ |
| WVP 设备同步 | `added > 0` | ☐ |
| 运维工具页面可访问 | `http://IP:8000/static/device-config.html` | ☐ |
| 设备可启流计数 | `active_devices >= 1` | ☐ |
| 实时统计有数据 | `GET /api/stats/realtime` 返回非空 | ☐ |
| 事件列表有记录 | `GET /api/events` 返回事件 | ☐ |