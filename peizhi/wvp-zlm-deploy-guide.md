# WVP + ZLMediaKit Docker 部署方案

本方案基于 `peizhi/` 目录下的三个文件部署一套完整的 GB28181 视频接入平台：

- `docker-compose.yml` — 4 个服务的编排（mysql / redis / zlmediakit / wvp）
- `application.yml` — WVP 的完整配置（**挂载进容器**，覆盖镜像内置配置）
- `conf/config.ini` — ZLMediaKit 的配置文件（**挂载进容器**，覆盖镜像内置的出厂 secret 与 mediaServerId）

> 部署方案已在本地 Mac（本机局域网 IP `<局域网IP>`）完整跑通：WVP Web（18080）正常登录、ZLM 在 WVP 系统信息中显示在线。本文档记录了完整的部署步骤、每个配置项的解析，以及踩过的坑。

---

## 一、整体架构与对接逻辑

```
                        下级/摄像头（GB28181）
                              │ SIP 注册、推流
                              ▼
              ┌──────────────────────────────┐
              │ WVP (wvp-upper-wvp)          │
              │  18080 Web/API   5060 SIP    │
              └───────┬──────────────┬───────┘
                      │ 操作ZLM(API)  │ 媒体流
                      ▼              ▼
              ┌───────────────┐  ┌──────────────────┐
              │ mysql         │  │ ZLMediaKit (zlm) │
              │ redis         │  │  12081 HTTP API  │
              └───────────────┘  │  30000-30100 UDP │
                                 └──────────────────┘
```

**核心对接链路：**

1. **下级 → WVP（信令）**：下级设备通过 SIP（5060）向 WVP 注册，WVP 作为国标平台与设备交互。
2. **下级 → ZLM（媒体）**：设备根据 WVP 下发的 SDP 信息（`sdp-ip` + RTP 端口）向 ZLM 推流。
3. **WVP → ZLM（控制）**：WVP 通过 ZLM 的 HTTP API（`media.ip` + `media.http-port` + `media.secret` 鉴权）控制推拉流、取流地址。
4. **WVP → 前端（播放）**：前端通过 `stream-ip` 拼出播放地址，从 ZLM 拉流播放。

**三组必须一致的"密钥/标识"**（本方案已统一为下列值）：

| 含义 | 配置位置 | 值 |
|---|---|---|
| ZLM 鉴权密钥 | compose `API_SECRET` = application.yml `media.secret` | `<ZLM密钥>` |
| ZLM 服务器标识 | compose `MEDIASERVER_ID` = application.yml `media.id` | `zlm-upper-02` |
| 端口范围 | compose 映射 30000-30100 = application.yml `port-range` | `30000,30100` |

---

## 二、前置准备

### 1. 镜像

```bash
# 登录内网源后拉取（WVP / ZLM 需手动拉，凭据在钥匙串中，沙箱无法读取）
docker pull docker.xuanyuan.run/zlmediakit/zlmediakit:master
docker pull docker.xuanyuan.run/wangwuli/wvp:2.7.1-2024110702

# mysql / redis 用本机已有镜像即可，无需拉取
```

### 2. 本机局域网 IP

`application.yml` 中 `sdp-ip` 与 `stream-ip` 必须填**下级设备能访问到本机**的局域网 IP（本机当前为 `<局域网IP>`）。如果换网络环境，需同步修改。

### 3. 端口占用检查

启动前确认以下端口未被占用：

| 端口 | 用途 | 协议 |
|---|---|---|
| 3306 | MySQL | tcp |
| 6379 | Redis | tcp |
| 18080 | WVP Web/API | tcp |
| 5060 | GB28181 SIP | udp/tcp |
| 554 | RTSP 播放（可选） | tcp |
| 12081 | ZLM HTTP API | tcp |
| 30000-30100 | RTP 收流 | udp |

```bash
lsof -iTCP:5060 -sTCP:LISTEN 2>/dev/null   # 检查 5060 是否被占
```

---

## 三、配置文件详解

### 3.1 docker-compose.yml

| 服务 | 说明 |
|---|---|
| `mysql` | 业务数据库，建库 `wvp`，用户 `wvp`/`<数据库密码>`。**建表需手动导入**（首次启动不会自动建表，见 §四 步骤 3） |
| `redis` | 缓存 |
| `zlmediakit` | 流媒体服务器。**关键改动是挂载了 `conf/config.ini`**：`./conf:/opt/media/conf`。镜像内置的 `secret` 是本方案不对的出厂值，必须用自定义 conf 覆盖。同时 `API_SECRET` 和 `MEDIASERVER_ID` 要与 WVP 对齐 |
| `wvp` | WVP 平台，**关键改动是挂载了 application.yml**：`./application.yml:/opt/wvp/config/application.yml:ro`，否则容器用镜像内置默认配置（前端会显示出厂默认的 44010200492000000001 / admin） |

端口映射要点：

- ZLM 的 `12081:80`：容器内固定 80（WVP 走内网访问 `http://zlmediakit:80`），12081 只是给外部调试 ZLM API 用。
- `30000-30100:30000-30100/udp`：**必须与 application.yml 的 `port-range` 一致**。摄像头往 ZLM 推流走这批 UDP 端口，映射保持一致（宿主机和容器端口相同），SDP 里填 `sdp-ip` + 这个端口段即可直达。
- WVP 的 `5060:5060`（udp+tcp）：SIP 信令端口。
- `554:554`：RTSP 播放端口，可选，用于 `ffplay rtsp://<局域网IP>:554/...` 本地验证。

**Zlmedia 容器卷挂载说明**：

```yaml
volumes:
  - ./conf:/opt/media/conf     # 不打 :ro,必须是可写挂载
  - zlm_log:/opt/ZLMediaKit/log
  - zlm_record:/opt/ZLMediaKit/record
```

> 为什么 `./conf` 不能只读：WVP 连上 ZLM 后，会通过 API 把 hook 回调地址写进 `config.ini`，再让 ZLM 重启加载。若挂载是只读的，写入失败会导致 ZLM 反复"重启应用配置"而无法稳定在线。

### 3.2 application.yml 关键项

#### 数据源（容器内网直连，服务名即主机名）

```yaml
spring:
  redis:
    host: redis        # compose 服务名
    database: 0
  datasource:
    dynamic:
      datasource:
        master:
          url: jdbc:mysql://mysql:3306/wvp?...   # mysql 服务名
          username: wvp
          password: <数据库密码>
```

> ① 原文件连的是 `127.0.0.1:3306/wvp2` + `root/root123`，那是**单机直接运行**的写法；容器化后必须改为 compose 服务名 `mysql` / `redis`。
> ② URL 中必须带 `allowPublicKeyRetrieval=true`，否则 MySQL 8 的 caching_sha2_password 认证会报 "Public Key Retrieval is not allowed"。

#### SIP 国标服务（前端"平台信息"显示的内容）

```yaml
sip:
  ip: 0.0.0.0                # 监听所有网卡（不要用 127.0.0.1）
  port: 5060
  domain: 1402000000         # 国标域编码 = ID 前 10 位
  id: 14020000002090000005   # 国标 ID（20 位）
  password: <国标密码>        # 设备接入密码
```

> 前端显示的"编号/域/密码"就是这里。之前显示 `44010200492000000001 / 4401020049 / admin123`，是因为 **application.yml 没有挂载进容器**，读的是镜像内置默认值。

#### ZLM 对接（WVP 操作 ZLM 的关键）

```yaml
media:
  id: zlm-upper-02        # = ZLM 容器的 MEDIASERVER_ID
  ip: zlmediakit          # 走 docker 内网用服务名；走宿主机则填本机 IP + http-port:12081
  http-port: 80           # 容器内固定 80
  secret: <ZLM密钥> # = ZLM 容器的 API_SECRET，不一致会报 "api secret is invalid"
  rtp:
    enable: true
    port-range: 30000,30100      # 收流端口，与 compose 映射一致
    send-port-range: 20000,20500 # 级联发送端口
  sdp-ip: <局域网IP>    # 告诉设备往哪个 IP 推流（本机局域网 IP）
  stream-ip: <局域网IP> # 播放地址用 IP
```

> `sdp-ip` 填错是"收不到推流"最常见的原因——设备被要求往一个不可达的 IP 推流。

### 3.3 conf/config.ini（ZLM 配置，必须覆盖出厂值）

ZLM 镜像内置的 `config.ini` 里 `secret` 是一串固定的出厂值、`mediaServerId` 是 `your_server_id`，跟 WVP 配置对不上，**必须用本方案的 `conf/config.ini` 覆盖**。其中与 WVP 对接相关的两项：

```ini
[api]
# ZLM HTTP API 鉴权密钥，必须与 application.yml 的 media.secret、compose 的 API_SECRET 一致
secret=<ZLM密钥>
```

> ZLM 的 `mediaServerId` 通过 compose 的 `MEDIASERVER_ID` 环境变量注入（=`zlm-upper-02`），与 WVP 的 `media.id` 一致；`config.ini` 里的 secret 则通过 `API_SECRET` 环境变量注入。两者任一不一致，WVP 都会报 "api secret is invalid" 或无法识别该媒体服务节点。

**config.ini 里其他值得知道的项：**

| 配置 | 值 | 含义 |
|---|---|---|
| `api.apiDebug` | 1 | 打印 HTTP API 请求日志，便于排查 |
| `ffmpeg.bin` | /usr/bin/ffmpeg | 拉流/转码/截图依赖的 FFmpeg |
| `protocol.enable_hls/rtsp/rtmp/flv` | 1 | 转协议开关，默认全开 |
| `general.mediaServerId` | 见上 | 由环境变量注入 |

### 3.4 登录账号密码（不在配置文件里，在数据库里）

WVP 的登录账号密码**不写在 application.yml**，而是由镜像内置的建表脚本 `/opt/wvp/mysql.sql` 初始化进数据库 `wvp_user` 表：

```sql
INSERT INTO wvp_user VALUES (1, 'admin', '21232f297a57a5a743894a0e4a801fc3', ...);
--                用户名 admin     密码 = MD5('admin')
```

所以这个 2.7.1 镜像的出厂登录密码是 **`admin` / `admin`**（不是官方文档常写的 admin123）。改密码直接改库即可：`UPDATE wvp_user SET password=MD5('新密码') WHERE username='admin';`

---

## 四、启动步骤

```bash
cd peizhi

# 1. 校验配置语法
docker compose config --quiet

# 2. 先启动数据库（给 WVP 建表做准备）
docker compose up -d mysql redis

# 3. 手动导入建表脚本（关键步骤！WVP 首次启动不会自动建表）
#    镜像内置了 /opt/wvp/mysql.sql，需要手动导入到 wvp 库
docker exec -i wvp-upper-mysql mysql -uwvp -p<数据库密码> wvp < <(docker exec wvp-upper-wvp cat /opt/wvp/mysql.sql)

# 4. 启动全部服务
docker compose up -d

# 5. 查看启动日志（WVP 成功标志：SIP 注册监听、ZLM 连接成功）
docker compose logs -f wvp
```

启动后：

1. 浏览器打开 `http://<局域网IP>:18080`，默认账号 `admin` / `admin`（注意：**这个 2.7.1 镜像的出厂密码是 admin，不是 admin123**，见 §六）。
2. 页面顶部"平台信息"应显示：编号 `14020000002090000005`、域 `1402000000`、端口 `5060`、密码 `<国标密码>`。
3. 系统信息页应显示 ZLM（`zlm-upper-02`）在线。

---

## 五、验证

### 1. 验证 WVP ↔ ZLM 已连通（看 WVP 日志）

```bash
docker logs --tail 50 wvp-upper-wvp 2>&1 | grep -iE "ZLM-连接成功|设置成功|心跳超时"
```

本方案实测的连通日志（出现 `[ZLM-连接成功]` + `[媒体服务节点] 设置成功` 即代表 ZLM 已在线）：

```
[ZLM-连接成功] ID：zlm-upper-02, 地址： zlmediakit:80
[媒体服务节点] 设置成功 zlm-upper-02 -> zlmediakit:80
```

> 若出现 `[ZLM-心跳超时]`，多是 ZLM 容器重启瞬间的临时现象，随后会自动重连，不必担心。

### 2. 验证 ZLM API 可达（鉴权用 secret）

```bash
curl "http://<局域网IP>:12081/index/api/getServerConfig?secret=<ZLM密钥>"
```

返回 `code: 0` 且 `api.secret` 为 `<ZLM密钥>` 即正常。

### 3. 验证 WVP Web 可访问

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://<局域网IP>:18080/   # 期望 200
```

浏览器打开 `http://<局域网IP>:18080`，用 `admin` / `admin` 登录后：平台信息显示国标编号 `14020000002090000005`，系统信息中 ZLM（`zlm-upper-02`）在线。

### 4. 用测试源模拟设备推流（无真实摄像头时）

```bash
# 用 ffmpeg 生成测试流推到 ZLM
ffmpeg -re -f lavfi -i testsrc=size=1280x720:rate=25 -f lavfi -i sine=frequency=440 \
  -vcodec libx264 -acodec aac -f rtsp rtsp://<局域网IP>:554/live/test
```

然后在 WVP 页面把该流地址配置为设备拉流，或在 ZLM 的 Web（`http://<局域网IP>:12081`）查看流列表。

---

## 六、常见问题

| 现象 | 原因 | 解决 |
|---|---|---|
| 启动报 `Table 'wvp.wvp_media_server' doesn't exist` | WVP 首次启动不会自动建表 | 手动导入镜像内置脚本：`docker exec -i wvp-upper-mysql mysql -uwvp -p<数据库密码> wvp < <(docker exec wvp-upper-wvp cat /opt/wvp/mysql.sql)` |
| 登录提示用户名密码错误 | 该 2.7.1 镜像的出厂密码是 `admin`（MD5: `21232f297a57a5a743894a0e4a801fc3`），不是 `admin123`。账号密码存在 MySQL `wvp_user` 表，不在 application.yml 里 | 用 `admin` / `admin` 登录；或改库：`UPDATE wvp_user SET password=MD5('新密码') WHERE username='admin';` |
| 前端显示出厂默认 ID / admin | application.yml 未挂载进容器 | 确认 wvp 服务有 `volumes: ./application.yml:/opt/wvp/config/application.yml:ro` 并 `up -d --force-recreate wvp` |
| ZLM 日志报 `api secret is invalid` | `media.secret` 与 ZLM `API_SECRET` / `config.ini` 的 `secret` 不一致 | 三处统一为 `<ZLM密钥>` |
| ZLM 显示离线 / 反复重启 | `./conf` 挂载为只读，WVP 写不进 hook 地址 | 挂载去掉 `:ro`，`up -d --force-recreate zlmediakit` |
| 设备注册成功但收不到推流 | `sdp-ip` 填错 / UDP 30000-30100 未映射或防火墙拦截 | 确认 `sdp-ip` 为下级可达的本机局域网 IP，UDP 端口段与 `port-range` 一致 |
| 改配置不生效 | 只 `restart` 不会重建容器，也不重读挂载文件 | 用 `docker compose up -d --force-recreate wvp` |
| WVP 无法连数据库 | application.yml 里还是 `127.0.0.1` | 改为 compose 服务名 `mysql` / `redis` |
