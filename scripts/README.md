# Mock 环境操作手册

## 启动 / 停止 Mock 环境

```bash
# 首次启动（构建镜像）
bash scripts/mock_start.sh --build

# 快速启动（已有镜像，代码改动后无需 build，自动挂载 volume）
bash scripts/mock_start.sh

# 停止所有服务
bash scripts/mock_start.sh --down
```

### 启动流程

1. 启动 Docker 容器：Redis → RTSP Server → 推流服务 → 后端 → AI
2. 等待服务就绪（health 检查）
3. 注册 Mock 设备（`mock-cam-vehicle`、`mock-cam-person`）
4. 验证管道状态

### 注册的 Mock 设备

| 设备 ID | 名称 | 类型 | 流地址 | 计数方向 |
|---------|------|------|--------|---------|
| `mock-cam-vehicle` | 北门摄像头-车辆 | vehicle | `rtsp://rtsp-server:8554/vehicle` | 单向 Enter |
| `mock-cam-person` | 南门摄像头-人流 | person | `rtsp://rtsp-server:8554/person` | 双向 |

---

## 全接口测试

```bash
# 默认 localhost
bash scripts/interface_test.sh

# 指定 IP
bash scripts/interface_test.sh 192.168.1.41

# 自定义 host 和端口
bash scripts/interface_test.sh 192.168.1.41 8000 8001
```

测试覆盖 48 个端点：系统 & 统计、事件、告警、设备管理、警力分配、时序预测、AI 服务、WebSocket、数据一致性。

---

## 服务地址

| 服务 | 地址 |
|------|------|
| 后端 API | `http://localhost:8000` |
| 健康检查 | `http://localhost:8000/health` |
| 设备列表 | `http://localhost:8000/api/devices` |
| 实时统计 | `http://localhost:8000/api/stats/realtime` |
| 事件列表 | `http://localhost:8000/api/events` |
| 告警列表 | `http://localhost:8000/api/alerts` |
| 警力分配 | `http://localhost:8000/api/police` |
| 运维工具 | `http://localhost:8000/static/device-config.html` |
| WebSocket | `ws://localhost:8000/ws` |
| AI 服务 | `http://localhost:8001` |
| RTSP 车辆流 | `rtsp://localhost:8554/vehicle` |
| RTSP 人流流 | `rtsp://localhost:8554/person` |

---

## 运维工具使用

> `http://localhost:8000/static/device-config.html`

1. 点击设备按钮（支持 `synced`/`online`/`running`/`registered` 状态设备）
2. 自动截取视频帧画面
3. 在画面上点击 3 个点：前 2 个为计数线两端，第 3 个为锚点（锚点所在侧 = 内侧 = Enter 方向）
4. 选择检测类型和计数方向
5. 点击「启流计数」提交配置

---

## 查看日志

```bash
# 查看所有服务日志
docker compose -f docker-compose.yml -f docker-compose.mock.yml logs -f

# 查看单个服务日志
docker compose -f docker-compose.yml -f docker-compose.mock.yml logs -f backend
docker compose -f docker-compose.yml -f docker-compose.mock.yml logs -f ai
```

---

## 代码改动后快速重启

```bash
# 代码通过 volume 挂载，无需 rebuild
docker compose -f docker-compose.yml -f docker-compose.mock.yml restart backend
docker compose -f docker-compose.yml -f docker-compose.mock.yml restart ai
```