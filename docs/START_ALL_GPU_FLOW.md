# scripts/start_all_gpu.sh 启动流程拆解

> 工作目录: `D:\散活\smart-transport`
> 适配服务器: `23.45.1.115`(GPU 服务器)
> 镜像源: `docker.xuanyuan.run`(内网,已拉取)
> 关键镜像: `smart-city-platform:gpu`(业务套) + WVP/ZLM 套镜像

---

## 1. 脚本速览

`scripts/start_all_gpu.sh` 是 GPU 服务器部署版的一键启动器,把下面这套组合拉起:

```
上游 WVP 套(由 setting-server-gpu/ 提供)        下游 业务套 + 前端(本项目)
├── wvp-upper-mysql  (MySQL 8.0)               ├── dt-backend  (FastAPI :8000)
├── wvp-upper-redis  (Redis)                   ├── dt-ai        (YOLO 推理 :8001)
├── wvp-upper-zlm    (ZLMediaKit)              ├── dt-mysql / dt-redis(项目自有,数据隔离)
└── wvp-upper-wvp    (WVP-GB28181 管理)        └── dt-frontend  (nginx + Vue :5173)
                                  │
                                  └─ 共用 'wvp-shared' 共享网络,backend/ai 按服务名访问 wvp / zlmediakit
```

---

## 2. 脚本自带的硬编码参数

| 变量 | 值 | 备注 |
|---|---|---|
| `ROOT_DIR` | 项目根目录 | 通过 `BASH_SOURCE` 自动定位 |
| `SETTING_DIR` | `setting-server-gpu/` | WVP 套所在目录 |
| `COMPOSE_SETTING` | `setting-server-gpu/docker-compose.yml` | 上游编排 |
| `COMPOSE_ROOT` | `docker-compose.gpu.yml` | 下游编排(GPU 版) |
| `ENV_FILE` | `.env.gpu` | **必须显式 --env-file**(compose 不会自动读) |
| `MYSQL_CONTAINER` | `wvp-upper-mysql` | 与 setting 编排固定名一致 |
| `WVP_CONTAINER` | `wvp-upper-wvp` | 同上 |
| `DB_NAME / DB_USER` | `wvp` / `wvp` | 同上 |
| `DB_PASS` | `Wvp@123456` | ⚠️ 与 setting 编排的 MYSQL_PASSWORD 一致 |
| `WVP_WEB` | `http://127.0.0.1:18080/` | 健康检查 URL |
| `ZLM_SECRET` | `zlm_secret_7890` | **三处一致密钥 #1**(application.yml + ZLM conf + 这里) |
| `SERVER_IP` | `23.45.1.115` | 打印访问地址时的回退值 |
| `GPU_IMAGE` | `smart-city-platform:gpu` | 业务套 backend/ai 用这个镜像 |

---

## 3. 6 阶段执行流程

### 阶段 1/6 — 语法自检
```bash
docker compose -f "$COMPOSE_SETTING" config --quiet
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_ROOT" config --quiet
docker image inspect "$GPU_IMAGE"
docker info | grep -qi nvidia
```
4 项任一失败 → 直接退出(脚本设了 `set -euo pipefail`)。

### 阶段 2/6 — 启动 WVP 套基础服务
```bash
docker compose -f "$COMPOSE_SETTING" up -d mysql redis
```
随后 `wait_mysql` 循环 60×2s,在 `wvp-upper-mysql` 内 `SELECT 1;` 通过即可。

### 阶段 3/6 — 检查并自动导入建表脚本
- 查 `information_schema.TABLES` 看 `wvp_media_server` 是否存在
- **不存在** → `import_schema`:
  1. 临时容器 `docker create --name wvp-sql-helper docker.xuanyuan.run/wangwuli/wvp:2.7.1-2024110702`
  2. `docker cp wvp-sql-helper:/opt/wvp/mysql.sql setting-server-gpu/`
  3. `docker exec -i wvp-upper-mysql mysql ... < mysql.sql`
  4. 删除临时容器和 SQL 文件
  5. **`docker restart wvp-upper-wvp`**(加载新表)
- **已存在** → 跳过(脚本幂等)

### 阶段 4/6 — 启动 WVP 套全部服务
```bash
docker compose -f "$COMPOSE_SETTING" up -d
```
拉起 `wvp-upper-{mysql, redis, zlm, wvp}` 4 个容器。然后 `wait_wvp` 循环: `curl http://127.0.0.1:18080/` → 200。

### 阶段 5/6 — ZLM API 验证
```bash
curl "http://127.0.0.1:12081/index/api/getServerConfig?secret=$ZLM_SECRET"
```
回显前 120 字节,作为活体检测。

### 阶段 6/6 — 启动业务套 + 前端
```bash
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_ROOT" \
  up -d --force-recreate backend ai frontend
```
- `dt-backend:8000` / `dt-ai:8001` / `dt-frontend:5173→80`

最后探测 LAN_IP(`ipconfig getifaddr en0` → `hostname -I`),打印访问地址 + 日志查看命令。

---

## 4. 文件清单(本脚本会触发读取的)

| 层级 | 文件 | 阶段 |
|------|------|------|
| 核心脚本 | `scripts/start_all_gpu.sh` | 本身 |
| WVP 套编排 | `setting-server-gpu/docker-compose.yml` | 1/2/4 |
| WVP 应用配置 | `setting-server-gpu/application.yml`(挂载) | 4 |
| WVP ZLM 配置 | `setting-server-gpu/conf/config.ini`(挂载) | 5 |
| WVP 业务教程 | `setting-server-gpu/WVP-ZLM-业务启动教程.md` | (人工查阅) |
| 业务套编排 | `docker-compose.gpu.yml` | 1/6 |
| 业务套环境变量 | `.env.gpu` | 1/6 |
| 业务镜像构建 | `Dockerfile.gpu` | (前置 build/load) |
| 应用代码 | `app/`(bind mount:cached) | 业务套运行时 |
| 模型权重 | `models/yolo11n.pt`、`models/`(Chronos) | dt-ai 启动加载 |
| 业务规则 | `configs/{business_rules,rules,bytetrack,police}.yaml` | 业务套运行时 |
| 设备种子 | `data/{device_geo,device_category}.json` | backend 启动 |
| 运维页 | `static/{business-rules,device-config,device-info}.html` | backend 服务 |
| 前端产物 | `map-marking-system-vue/dist/` | frontend 挂载 |
| 前端 nginx | `map-marking-system-vue/nginx.conf` | frontend 容器 |
| 前端瓦片 | `map-marking-system-vue/public/tiles/` | frontend 挂载 |
| 共享网络 | `wvp-shared`(external, `name: wvp-shared`) | 必须存在 |

---

## 5. 共享网络 `wvp-shared` 的关键角色

```
dt-backend ─┐
dt-ai      ─┴─ 共享网络 wvp-shared ──→  wvp:18080(WVP API)
dt-frontend                            zlmediakit:80(ZLM HTTP API)
```

脚本本身**不创建**这个网络,依赖 `setting-server-gpu/docker-compose.yml` 先把它 `external: true` + `name: wvp-shared` 创建出来。

---

## 6. 这个脚本不做的事(避免误解)

- ❌ **不调用 GPU 探针**:与刚拉起服务抢 CUDA 上下文冲突;需要时单独跑 `bash scripts/check_gpu_env.sh --run`
- ❌ 不构建镜像(`smart-city-platform:gpu` 必须先 `docker load` 或独立 build)
- ❌ 不导入 ZLM 设备种子表(由 backend 启动从 `data/*.json` 读)
- ❌ 不写 MEDIA_SDP_IP / MEDIA_STREAM_IP(在 `setting-server-gpu/application.yml` 里手工填)

---

## 7. 配套脚本对照

| 角色 | GPU 版 | CPU 版 |
|------|--------|--------|
| 一键启动 | `start_all_gpu.sh`(本文档) | `start_all.sh`(同一脚本替换 setting→setting-server,.env.gpu→.env,GPU_IMAGE→smart-city-platform) |
| 不带探针启动 | `start_all_gpu_noprobe.sh`(等价于 start_all_gpu.sh,保留兼容) | — |
| 一键停止 | `stop_all_gpu.sh`(`stop` / `--down` / `--no-backend`) | `stop_all.sh` |
| Mock 模式 | `mock_mode.sh` + `docker-compose.mock.yml` | — |
