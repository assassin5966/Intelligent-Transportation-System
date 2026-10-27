# 项目 IP / 网络配置索引

> 工作目录: `D:\散活\smart-transport`
> 用途: 排查 IP / 主机相关问题、迁移服务器、定位某个 IP 在哪里被引用

---

## 1. 涉及 IP 的机器

| IP | 角色 | 出现位置 |
|---|---|---|
| `23.45.1.112` | 历史 CPU 服务器 / 旧 WVP 服务器 / simulator 演示 | `wvp_zlm.yml`、`setting-server-bak/`、`simulator/start_simulator.sh` |
| `23.45.1.115` | **当前主用 GPU 服务器** | `.env`、`.env.gpu`、`setting-server/application.yml`、`setting-server-gpu/application.yml` |
| `23.45.1.255` | 历史 WVP_SIP_PUBLIC_IP | `wvp_zlm.yml` (UDP 信令公网痕迹) |
| `192.168.1.28` | 开发本机局域网 IP | `.env.local` |
| `127.0.0.1` | 本机回环 | `.env.mock`、`simulator/start_simulator.sh` |
| `0.0.0.0` | 监听所有网卡 | `sip.ip` (WVP 监听设置) |

## 2. 容器内网服务名(永远不写 IP)

- `redis:6379`、`mysql:3306`、`backend:8000`、`ai:8001`
- `wvp:18080`、`zlmediakit:80`
- `rtsp-server:8554` (mock 版测试流)
- `host.docker.internal` — 容器访问宿主机的特殊域名(傻瓜教程文档中提到)
- 共享网络: `wvp-shared` (external, 必须显式 `name:` 防止拼接)

## 3. 暴露端口清单

| 服务 | 端口 | 用途 |
|---|---|---|
| dt-ai | `8001` | AI 推理入口 |
| dt-backend | `8000` | FastAPI 业务后端 |
| dt-mysql | `3307→3306` | 长期归档 `dt_stats` |
| dt-redis | `16379→6379` | 实时状态(避开本机 6379) |
| dt-frontend | `5173→80` | 大屏前端(niginx) |
| WVP 上游 | `18080` | WVP Web/API |
| WVP SIP | `5060/udp+tcp` | GB28181 信令 |
| ZLM HTTP | `12081` | ZLM REST API |
| ZLM 播放 | `80`(HTTP-FLV/WS-FLV) |
| ZLM RTSP | `554` |
| ZLM RTP | `30000-30100/udp+tcp` |

## 4. 改 IP 要动的 5 个位置

迁移服务器 `OLD_IP → NEW_IP` 时:

1. `.env`(或 `.env.gpu` / `.env.local`)的 `ZLM_PUBLIC_BASE=http://NEW_IP:12081`
2. `setting-server/application.yml` 的 `media.sdp-ip`、`media.stream-ip` → `NEW_IP`
3. `setting-server-gpu/application.yml`(如启用)同步改
4. 老配置文件 `wvp_zlm.yml`、`setting-server-bak/` 也同步改(避免再启用时踩坑)
5. 若换了网络段,确认防火墙放行 `5060/tcp+udp`、`30000-30100/udp+tcp`

## 5. 配置文件清单(按相关度排序)

| 优先级 | 文件 | 关键字段 |
|---|---|---|
| ★★★ | `.env` | `ZLM_PUBLIC_BASE` |
| ★★★ | `.env.gpu` | `ZLM_PUBLIC_BASE` |
| ★★★ | `.env.local` | `ZLM_PUBLIC_BASE`(本机) |
| ★★★ | `setting-server/application.yml` | `media.sdp-ip`、`media.stream-ip` |
| ★★★ | `setting-server-gpu/application.yml` | 同上 |
| ★★ | `docker-compose.yml` / `*.gpu.yml` | 端口映射、共享网 `wvp-shared` |
| ★★ | `setting-server/docker-compose.yml` / `setting-server-gpu/...` | 端口映射、网络名 |
| ★ | `wvp_zlm.yml` | 老备份,仅参考 |
| ★ | `simulator/start_simulator.sh` | `SERVER_IP` 默认值 |
| ★ | `map-marking-system-vue/vite.config.js` | dev proxy(仅本地) |
| ★ | `map-marking-system-vue/nginx.conf` | Docker DNS resolver `127.0.0.11` |
| ☆ | `static/*.html` | 运维页默认 `http://localhost:8000` |
| ☆ | `setting-server-bak/`、`setting/` | 历史版本备份 |
| ☆ | `setting-server/conf/config.ini` | ZLM 主配置(媒体参数) |
