# 离线构建与部署闭环 (P0.2)

> 目标机（23.45.1.112 / 23.45.1.115）**无外网**。本平台采用「预构建镜像 + bind 挂载」
> 的离线友好形态，但构建期仍强依赖外网（PyTorch 官方源、PyPI、apt 镜像）。
> 本文固化「只能在联网构建机出镜像 tar，目标机只 load 不 build」的流程，并补齐
> 权重校验与锁依赖，确保跨时间、跨机器可复现。

---

## 0. 总原则

- **目标机禁止 `docker build`**：任何 `docker compose ... build` 在离线机上都会失败或拉到不可控新版。
- **出镜产物全部来自联网构建机**：镜像 tar、`models/`、`map-marking-system-vue/dist/`、`tiles/`、`conf/`、`configs/`。
- **锁版本 + 校验权重**：保证「这次能跑的，下次也能跑」。

---

## 1. 离线锁依赖 (P0.1)

### 1.1 在联网构建机生成锁文件

```bash
cd smart_transport_git
python -m venv .venv && source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
bash scripts/gen_requirements_lock.sh          # 生成 requirements.lock.txt (精确 ==)
# 或: VENV=.venv bash scripts/gen_requirements_lock.sh
```

`requirements.lock.txt` 是**精确 `==` 版本**，需随仓库/离线 tar 一起交付。

> torch / torchvision 在 Dockerfile 中按架构单独安装（CPU 走 PyTorch 官方 CPU 源、
> GPU 走 cu121 源），不纳入此锁；如需完全锁定，可在构建机装对应 torch 后一并 freeze。

### 1.2 构建时改用锁文件

在 Dockerfile（CPU/GPU）中，将

```dockerfile
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt
```

改为（把锁文件 COPY 进镜像）：

```dockerfile
COPY requirements.txt requirements.lock.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.lock.txt
```

构建机需联网执行此构建；产物是镜像 tar，目标机只 `docker load`。

### 1.3 定期更新

联网机上 `pip install -U -r requirements.txt` 后重跑 1.1，生成新锁并提交。

---

## 2. 构建前的权重校验 (已在 Dockerfile 固化)

`models/` 不入库、不联网下载。Dockerfile 末尾已加显式校验：
`/app/models` 下若无任何 `*.pt`，构建直接以清晰中文报错退出，不再含糊失败。

构建机准备：

```bash
mkdir -p models
cp /path/to/yolo11n.pt models/yolo11n.pt
# 可选: 写入 sha256 供交付时核对
sha256sum models/yolo11n.pt > models/yolo11n.pt.sha256
```

---

## 3. 出镜 tar 清单（目标机需接收的项）

| 项 | 说明 | 目标机落点 |
|---|---|---|
| `smart-city-platform.tar` | 业务镜像（`docker save`） | 任意临时目录，`docker load -i` |
| `smart-city-platform_gpu.tar` | GPU 镜像 | 同上（GPU 服务器） |
| `models/` | YOLO 权重 | `./models/`（构建上下文 / 挂载源） |
| `map-marking-system-vue/dist/` | 前端产物 | `./map-marking-system-vue/dist/`（nginx 挂载） |
| `map-marking-system-vue/public/tiles/` | 离线瓦片 | 同上（nginx 挂载） |
| `setting-server(-gpu)/conf/`、`application.yml` | WVP/ZLM 配置 | 对应目录 |
| `configs/` | 业务配置（含模板 JSON） | 挂载源 |
| `requirements.lock.txt` | 锁依赖 | 项目根（构建用） |

> 前端 `tile-preview.html` 已去除 CDN 引用（改用本地 `vendor/leaflet/`），
> 离线 tar 还需包含 `map-marking-system-vue/public/vendor/leaflet/leaflet.{css,js}`
> （从 leaflet 1.9.4 发布包取）。

---

## 4. 目标机落地步骤

```bash
# 1) 加载镜像
docker load -i smart-city-platform_gpu.tar

# 2) 放好挂载源: models/ dist/ tiles/ conf/ configs/ (见第 3 节清单)

# 3) 准备 .env.gpu (GPU 服务器) / .env (CPU 服务器)
#    WVP_ENABLED / ZLM_PUBLIC_BASE / SERVER_IP 等

# 4) 一键启动 (脚本已幂等 + detect-and-start, 见 PROJECT_OPTIMIZATION_PLAN.md 2.4)
bash scripts/start_all_gpu.sh            # GPU 服务器
# 或
bash scripts/start_all.sh                # CPU 服务器

# 5) 校验
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/api/health
docker logs -f dt-ai
```

---

## 5. 开机自启 / 重启自愈 (P0.3)

目标机 `reboot` 后需自动恢复。组合拳：

1. **Docker restart policy 保底**：各服务已配 `restart: unless-stopped`，
   且 `docker.service` 已 `systemctl enable`（daemon 启动后容器自动拉起）。
2. **systemd 单元**：仓库提供 `scripts/dt-stack.service`，在 docker 就绪后
   调用一键启动脚本（脚本幂等，重复执行安全）。

部署单元：

```bash
cp scripts/dt-stack.service /etc/systemd/system/dt-stack.service
# 若项目部署根不是 /opt/smart-transport, 改 ExecStart 路径
systemctl daemon-reload
systemctl enable --now dt-stack.service
```

> 跨 compose 顺序：脚本 step5/6 前已 `docker network inspect wvp-shared` 预检，
> WVP 套未就绪会提示先启动 WVP 套；WVP 段采用 detect-and-start 直接 `docker start`，
> 不再 `up -d` 重建，彻底绕开 `setting-server` / `setting-server-gpu` 的 project 名歧义撞名。

---

## 6. 排障速查

- **容器撞名 `container name is already in use`**：旧容器来自另一套 compose（CPU/GPU 同名 `dt`）。
  先 `bash scripts/stop_all.sh` 或 `stop_all_gpu.sh` 清理，再启动目标版本。
- **权重缺失报错**：构建时 `models/*.pt` 未放入，按第 2 节补齐。
- **WVP 拉不起**：看 `docker logs wvp-upper-wvp`；多半是 `application.yml` / `conf/config.ini`
  三处密钥不一致或 `wvp-shared` 网络未创建（先启动 WVP 套）。
- **前端白屏**：`dist/` 或 `tiles/` 未挂载 / 路径错误，`docker exec dt-frontend nginx -s reload`。
