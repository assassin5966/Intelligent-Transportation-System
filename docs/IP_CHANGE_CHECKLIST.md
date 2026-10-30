# IP 设置改动清单 — 按场景分类

> 工作目录: `D:\散活\smart-transport`
> 用途: 任意 IP / 服务器迁移时直接对照动文件

---

## A. 必须改的 3 个核心文件(任何 IP 变更)

| # | 路径 | 字段 | 当前值 | 备注 |
|---|------|------|--------|------|
| 1 | `.env.gpu` | `ZLM_PUBLIC_BASE` | `http://23.45.1.115:12081` | 前端播放地址翻译,**必须 HTTP://:12081** |
| 2 | `setting-server-gpu/application.yml` | `media.sdp-ip` + `media.stream-ip` | `23.45.1.115` | WVP 国标信令 + 拉流目标 |
| 3 | `scripts/start_all_gpu.sh` | `SERVER_IP="..."` | `23.45.1.115` | 启动完成打印用 |

`setting-server-gpu/application.yml` 是 bind mount 进去的,**改完只要重启 wvp 容器即生效**,无需重建镜像。

---

## B. 服务器迁移 / IP 完整改动(全场景)

把 `OLD_IP` 改为 `NEW_IP`,以下文件全部同步:

### 必动(运行时)
1. `.env.gpu` → `ZLM_PUBLIC_BASE`
2. `.env` → `ZLM_PUBLIC_BASE`(若仍用 .env)
3. `setting-server-gpu/application.yml` → `media.sdp-ip` + `media.stream-ip`
4. `setting-server/application.yml` → 同上(CPU 版)
5. `scripts/start_all_gpu.sh` → `SERVER_IP`
6. `scripts/start_all.sh` → `SERVER_IP`(CPU 版)
7. `docs/GPU鐜鎺掓煡鏂规.txt` → 所有 curl 地址
8. `docs/wvp-device-offline-check.txt` → 访问地址
9. `docs/鏈嶅姟閲嶅惎鍛戒護閫熸煡.txt`(如有提到 IP)
10. `README-DEPLOY.txt` → curl 命令

### 备用 / 历史(改作留档)
11. `.env.local` → `ZLM_PUBLIC_BASE`
12. `wvp_zlm.yml` → 5 个 IP 字段
    - `WVP_SIP_IP` / `WVP_SIP_SERVER_IP` / `MEDIA_SDP_IP` / `MEDIA_STREAM_IP`
13. `setting-server-bak/application.yml` → `sdp-ip` + `stream-ip`
14. `setting-server-bak/application.yml-black` → 同上
15. `setting-server-bak/conf/config.ini` → 主要是注释里的 `127.0.0.1`(**不必改**),但若用户场景化使用需调整
16. `setting-server-bak/WVP-ZLM-涓氬姟鍚姩鏁欑▼.md` → 文档内访问地址
17. `setting/application.yml` → `sdp-ip` + `stream-ip`(本机版)
18. `setting/conf/config.ini` → 主要是注释里的 localhost,大多数情况下不动
19. `simulator/start_simulator.sh` → `SERVER_IP="${SERVER_IP:-23.45.1.112}"`(可不动,仅演示用)

---

## C. 本地开发(本机 IP `192.168.1.x`)

1. `.env.local` → `ZLM_PUBLIC_BASE=http://192.168.1.28:12081`
2. `setting/application.yml` → `sdp-ip` + `stream-ip` 都填本机 IP

---

## D. 单机 Mock(`MOCK_ENABLED=true`)

- `.env.mock` 全部 `localhost`,不需要改

---

## E. 前端 / 运维页(运行时可改,不必动文件)

| 文件 | 默认 URL | 改法 |
|------|----------|------|
| `static/business-rules.html` | `http://localhost:8000` | 页面输入框 `baseUrl` |
| `static/device-config.html` | `http://localhost:8000` | 同上 |
| `static/device-info.html` | `http://localhost:8000` | 同上 |
| `map-marking-system-vue/vite.config.js` | `http://localhost:8000` | 仅 `npm run dev` 用,build 后走 nginx.conf |
| `map-marking-system-vue/nginx.conf` | `http://backend:8000` | 容器服务名,**无需改** |

---

## F. 永远不需要改 / 永远用容器服务名

| 文件 | 当前用法 | 为什么不用改 |
|------|----------|--------------|
| `docker-compose.yml` / `docker-compose.gpu.yml` | `redis://redis:6379/0`、`http://backend:8000` | 容器内网,Docker DNS 自动解析 |
| `map-marking-system-vue/nginx.conf` | `http://backend:8000` | nginx 与 backend 同一 docker 网络 |
| `app/common/config.py` | `redis_url` 等 | 容器内 |
| `app/backend/core/wvp_client.py` | `http://wvp:18080` | 共享网络 `wvp-shared` |

---

## G. 改完 IP 后的复启动作清单

```bash
# 1. 重启业务套(读 .env.gpu)
docker compose --env-file .env.gpu -f docker-compose.gpu.yml \
  restart backend ai frontend

# 2. 重启 WVP 套(读 application.yml)
docker compose -f setting-server-gpu/docker-compose.yml restart wvp zlmediakit

# 3. 防火墙放行(关键端口)
#    5060/tcp + 5060/udp    GB28181 信令
#    30000-30100/udp+tcp    RTP 收流

# 4. 同步通知摄像头/下级平台,SIP 注册目标改为 NEW_IP

# 5. 验证
curl http://<NEW_IP>:18080/                # WVP
curl http://<NEW_IP>:12081/index/api/getServerConfig?secret=zlm_secret_7890   # ZLM
curl http://<NEW_IP>:8000/api/health       # 后端
curl http://<NEW_IP>:5173                  # 前端
```

---

## H. 一句话决策树

```
要改吗?
├─ 服务器迁移 ───→ 看 B 段全部必动
├─ 本地开发     ──→ 看 C 段(本机 IP)
├─ 用 Mock     ──→ 不需要改
├─ 浏览器本地访问 ──→ 看 E 段(改 baseUrl 输入框)
└─ 不确定       ──→ 看 A 段必动 3 个文件
```
