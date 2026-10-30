# 智慧古城车辆人流监管平台 · 整体优化方案

> 文档性质：**方案总纲（先规划，不落地代码）**。基于 2026-10-16 全仓库代码体检结果，结合此前已完成的 sim 推流修复、划线流程离线/模板化方案、启动脚本重启自愈根因分析，给出分期优化路线图。
> 核心约束：**最终部署目标为无外网服务器**；**宿主机重启后项目应自动恢复**，无需人工干预。

---

## 0. 现状总览

### 0.1 亮点（应保留）
- **部署形态方向正确**：预构建镜像 + bind 挂载（前端 `dist`、瓦片、配置、模型权重均走挂载），离线友好。
- **后端容错到位**：`app/backend/main.py` 的 `lifespan` 对 6 个后台任务和预测路由均做 `try/except` 降级，单点失败不影响整体启动。
- **AI 资源释放闭环良好**：`stream.py`/`model_pool.py` 的 `finally`/`shutdown` 路径完整，无显存泄漏。
- **代码注释质量高**：关键函数均有中文 docstring，解释"为什么"而非"是什么"（如 `stream.py`、`model_pool.py` 头注释）。
- **sim 推流已修复**：GB28181 RTP/PS 打包层经字节级实弹验证（48 项单测 + ffmpeg 解码验证），WVP 登录 token 字段已兼容多版本。
- **重启自愈根因已定位**：WVP 套最初以 `setting-server`（非 GPU）目录部署，而 `start_all_gpu.sh` 用 `setting-server-gpu` project 管理，容器名相同（显式 `container_name`）→ 脚本 `up` 撞名；容器虽配 `restart: always/unless-stopped` 本应自启，但脚本不幂等导致手动重跑时失败。

### 0.2 主要风险（按严重度）
1. **【高】离线可复现性无保障**：`requirements.txt` 版本仅用区间未锁；构建期强依赖外网（pytorch 官方源、npm、apt 清华源）。在无外网服务器上**无法重 build**，且跨时间 build 可能拉到不兼容新版。
2. **【高】重启后无自愈闭环**：无 systemd 单元；跨 compose（业务套 ↔ WVP 套）无启动顺序保证，reboot 后若 `wvp-shared` 未就绪，业务容器 `up` 失败且 `unless-stopped` 不会在依赖缺失后自愈。
3. **【高】双份等价 WVP 配置漂移**：`setting-server/` 与 `setting-server-gpu/` 的 compose 与 `application.yml` 经 `diff` 仅注释不同，实际等价，长期必漂移。
4. **【中】配置散落与孤儿配置**：服务器 IP 散落 5+ 处；`.env.local`/`.env.mock` 未被任何脚本消费（改了不生效）；数据库/WVP 密码明文硬编码。
5. **【中】冗余未闭环**：`setting-server-bak/`（含 0 字节空 sql）、`start_all_gpu_noprobe.sh`、镜像内多余的 `static`/`tool` 副本仍在。
6. **【中】零自动化测试**：全仓库无单测/CI；AI 计数正确性（反复出现的"漏计/误计"主题）无回归保护。
7. **【低】代码级健壮性**：个别裸 `except` 静默吞异常；AI 共享状态无锁（当前设计安全但脆弱）；历史清理残留乱码文档文件。

---

## 1. 优化分期路线图

| 阶段 | 目标 | 关键项 | 优先级 | 工作量 | 风险 |
|---|---|---|---|---|---|
| **P0 离线闭环** | 无外网可复现部署 + 重启自愈 | 锁依赖、离线 tar、systemd 自启、脚本幂等闭环 | 最高 | 中 | 低 |
| **P1 配置与冗余治理** | 单一事实来源、去重 | 合并 WVP 双配置、清理冗余、IP/密码收口、删孤儿 env | 高 | 小 | 中（需留意 WVP 数据卷） |
| **P2 功能增强（既有方向）** | 划线模板化、可观测 | 模板式自动划线落地、计数 metrics、截帧下沉 | 中 | 中 | 低 |
| **P3 质量基建** | 防回归 | 最小 pytest 套件 + CI、拆胖文件、命名消歧 | 中 | 中 | 低 |

> 建议执行顺序：**P0 → P1 → P2 → P3**。P0/P1 是"能稳定跑在无外网服务器"的硬门槛，优先于功能增强。

---

## 2. P0 — 离线闭环与重启自愈（最高优先级）

### 2.1 锁定依赖版本（离线可复现前提）
- **问题**：`requirements.txt` 用 `>=`/`<` 区间（如 `ultralytics>=8.3.0,<9`、`fastapi>=0.111.0`），无 `pip freeze` 锁定；在无外网服务器重 build 时可能拉到破坏性新版。
- **方案**：
  - 在联网构建机上 `pip freeze > requirements.lock.txt`（精确 `==` + hash），随镜像/`requirements` 一起提交。
  - `Dockerfile`/`Dockerfile.gpu` 安装分两步：先装 `requirements.lock.txt`（锁死核心依赖），再装未锁的 dev 依赖。
  - 记录构建机 Python 版本（3.13/3.14 管理运行时），避免 ABI 漂移。
- **风险**：锁定后需定期更新；建议 CI 里跑 `pip-compile --upgrade` 生成新锁。

### 2.2 离线构建闭环
- **问题**：构建期依赖外网（pytorch 官方源 `download.pytorch.org`、npm、apt 清华源），无外网服务器无法 `docker build`。当前靠"先在联网机 build 再 load"规避，但**流程未文档化、易踩坑**。
- **方案**：
  - 产出 `docs/OFFLINE_BUILD.md`：明确"目标机禁止 build，只能在联网构建机出镜像 tar"；列出离线 tar 应包含的项（镜像 tar、模型权重 `models/`、前端 `dist/`、瓦片 `tiles/`、配置 `conf/`/`configs/`）。
  - pytorch wheel 改走内网源（如 `docker.xuanyuan.run`）或随离线 tar 预置；`models/` 增加哈希校验（构建前 verify）。
  - `Dockerfile` 增加构建前校验：`models/*.pt` 缺失时给出清晰报错（而非含糊失败）。
- **风险**：低；主要是文档化 + 校验脚本。

### 2.3 开机自启 + 重启竞态补偿（治本"重启起不来"）
- **问题**：无 systemd 单元；跨 compose 无顺序保证；`unless-stopped` 不会在依赖缺失后自愈。
- **方案**（组合拳）：
  1. **Docker restart policy 保底**：确认宿主机 `docker.service` 已 `systemctl enable`（容器内 `restart: always/unless-stopped` 已在）。这是第一层——daemon 启动后容器自动拉起。
  2. **systemd 单元 `dt-stack.service`**（unit 文件随仓库提供，落 `/etc/systemd/system/`）：
     - `After=docker.service docker.socket`，`Wants=docker.service`
     - `Type=oneshot`、`RemainAfterExit=yes`、`TimeoutStartSec=600`
     - `ExecStart=/bin/bash /opt/smart-transport/scripts/start_all_gpu.sh`
     - 脚本需先改为**全幂等**（见 2.4）才能安全作为 systemd 命令。
  3. **跨 compose 顺序 + 网络自愈**：脚本在 step4（WVP 套）后、step6（业务套）前，预检 `wvp-shared` 存在且 `wvp`/`zlmediakit` 服务名可解析；缺失则等待/重建。
- **风险**：低；systemd 与 Docker restart policy 双保险。

### 2.4 启动脚本幂等闭环（落地 2.3 的前提，也补全之前的半截修复）
- **问题**：`start_all_gpu.sh` 的 WVP 段（step2/4）用 `docker compose up -d`，遇到"容器已存在但 project 名不符"（如 `setting-server` vs `setting-server-gpu`）即撞名失败；step6 的 `--force-recreate` 会全量重建（含 mysql/redis），造成不必要抖动；CPU 版 `start_all.sh` 缺 `dt_default` 预建，与 GPU 版不一致。
- **方案**（在已做的 `dt_default` 自愈基础上补全）：
  - **WVP 段 detect-and-start**：若 `wvp-upper-*` 容器已存在（无论何种 project），直接 `docker start`，不 `up -d` 重建 → 彻底绕开 project 名歧义（这是今天所有撞名的终点）。
  - **业务套**：保留 `dt_default` 自愈；去掉无条件 `--force-recreate`，改为"有变更才重建"，减少抖动。
  - **`wvp-shared` 预检**：step5/6 前 `docker network inspect wvp-shared`，失败则提示"先启动 WVP 套"。
  - **去除平台专有残留**：`ipconfig getifaddr en0`（macOS 专有）改纯 `hostname -I`/读 `SERVER_IP`。
  - **project 互斥提示**：同机 CPU/GPU 版 `name: dt` 冲突，脚本加检测或使用不同 project 名避免误混跑。
- **风险**：低；纯脚本逻辑，不碰数据卷。

---

## 3. P1 — 配置与冗余治理

### 3.1 合并双份 WVP 配置
- **问题**：`setting-server/` 与 `setting-server-gpu/` 经 `diff` 仅注释不同，实际等价 → 必漂移。
- **方案**：
  - 合并为单一 `setting-server/`，用同一份 compose；通过 env（镜像 tag `:latest` vs `:gpu`、或 `WVP_UPPER_NET`）区分服务器。
  - 或保留目录但抽出 `application.yml` 模板，由 `start_all_gpu.sh` 按服务器渲染。
  - **设显式 `name:`**（如 `name: setting-server`），避免 project 名随目录漂移（否则 `simulator/start_simulator.sh` 里硬编码的 `setting_server_default` 推断会失配）。
- **风险（中）**：WVP MySQL 数据在命名卷 `setting-server_mysql_data` / `setting-server-gpu_mysql_data`。切换 project 后旧卷不认 → 设备/通道需重注册（本项目用 sim 模拟器影响小，但需先 `docker volume ls` 确认数据重要性再操作）。

### 3.2 清理冗余（闭环历史审计）
- **删除**：`setting-server-bak/`（含 0 字节 `wvp_bak_0910.sql`）、`start_all_gpu_noprobe.sh`（陈旧等价版）、镜像内多余的 `static` 副本（Dockerfile 不再 COPY `static`，仅 bind 挂载即可）、`tool/` 进生产镜像的副本（测试工具移出镜像或仅 CI 用）。
- **重命名消歧**：`sim/`↔`simulator/` 命名易混（设备侧推流 vs 下级国标平台），改 `sim/gb28181-device-sim/` 与 `sim/wvp-lower-sim/`，或 README 明确层级。
- **乱码文档**：`docs/` 下 GBK 乱码文件名重命名为 UTF-8 中文，并收敛散落 `*.txt`。

### 3.3 配置收口（单一事实来源）
- **问题**：IP 散落 5+ 处（`.env`/`.env.gpu`/`.env.local`/`simulator/start_simulator.sh`/静态页默认值）；密码明文；`.env.local`/`.env.mock` 孤儿（文档声称用，但无脚本 `--env-file` 指向）。
- **方案**：
  - 删除孤儿 `.env.local`/`.env.mock`，或改为被 `mock_mode.sh`/本地 dev 显式引用。
  - 服务器 IP 收敛到 `.env` + `setting-server/conf/`，文档仅引用不定义。
  - 密码从 `.env`/compose env 读取，消除脚本字面量（如 `start_all.sh:36` 的 `DB_PASS`）。
  - 在 `app/common/config.py` docstring 写明三层配置（默认值 + `.env` + `business_rules.yaml`）优先级链。
- **风险**：低；注意 `docs/IP_CHANGE_CHECKLIST.md` 需同步更新为"唯一真源在 .env"。

---

## 4. P2 — 功能增强（既有方向，待确认后落地）

### 4.1 模板式自动划线（详见 `docs/DEVICE_CONFIG_OPTIMIZATION_PLAN.md`）
- 已出方案：模板库 `configs/line_templates.json`（随镜像，离线）+ 匹配规则（设备名前缀/分组/相机类型）+ 三种触发（注册后自动套用 / 前端一键套用 / 批量配置）。
- **落地分级**：Level 1（模板库 + 前端手动套用）风险低、收益快；Level 2（规则自动匹配 + 批量）需后端 `/api/line-templates` CRUD + `apply-template` 接口。
- **离线保证**：模板 JSON 随 `COPY configs` 进镜像，无 DB 表、无联网。

### 4.2 截帧能力下沉（去双实现）
- **问题**：`app/backend/api/devices.py:225-266` 用 `subprocess(ffmpeg)+cv2` 独立截帧，与 `app/ai/stream.py` 拉流逻辑两套实现。
- **方案**：截帧下沉到 AI 服务统一提供，backend 经内部接口调用；复用已有 `FrameReader`，行为一致、减少维护。

### 4.3 计数可观测性
- 已在 `docker-compose.gpu.yml` 暴露 `COUNT_STATS_INTERVAL`/`COUNT_EVENT_LOG`；建议补充 Prometheus/简单 `/metrics` 端点，便于无外网环境下本地排障"漏计/误计"。

---

## 5. P3 — 质量基建（防回归）

### 5.1 最小 pytest 套件 + CI
- **优先覆盖**：`app/ai/counter.py`（越线判定，项目反复出现计数问题）、`app/common/device_info.py`（点位派生/白名单逻辑）。
- 把现有 `sim/test_rtp_pack.py` 纳入 `pytest` 套件（目前是手动脚本）。
- 仓库加 `.github/` 或 `.gitlab-ci.yml`：锁版本校验 + 单测 + `dist`↔`src` 一致性。

### 5.2 代码级健壮性
- 裸 `except Exception`（如 `app/backend/api/ws.py:76,96`、`app/ai/service.py:143`）补 `logger.exception`。
- `app/ai/model_pool.py:58-59` 共享状态加 `asyncio.Lock` 或文档明确"必须在事件循环线程操作"约束。
- 拆胖文件：`app/backend/api/devices.py`（588 行）拆分坐标解析/截帧/设备 CRUD。
- `device_info` 概念在 `app/common` 与 `app/backend` 各有一模块，README 明确"common=存储层、api=接口层"边界。

---

## 6. 实施建议与既有优化衔接

### 6.1 从哪里开始
1. **立刻做（低风险、高收益）**：P0.4（脚本幂等闭环，补全 WVP 段 detect-and-start）+ P0.3（systemd 单元）。这直接解决你今天"宿主机重启起不来"的痛点，且不碰数据。
2. **紧接着**：P0.1（锁依赖）+ P0.2（离线 build 文档），把"无外网可复现"固化。
3. **然后**：P1（合并 WVP 配置 + 清理冗余 + 配置收口）。
4. **功能侧**：P2.1 模板式自动划线（已有详细方案，等确认 Level 1 还是 1+2）。

### 6.2 与已完工作的关系
- **sim 推流修复**：已落地本地 `sim/gb28181_device.py`（哈希 `f51f0c82...`），待服务器 scp 覆盖 + `rekeep` 验证流畅度（卡帧根因已改为 GB28181 PS 重建）。
- **划线流程方案**：见 `docs/DEVICE_CONFIG_OPTIMIZATION_PLAN.md`（离线核查 + 模板式自动划线段）。
- **重启自愈根因**：本文 0.1/2.4 已定位，本方案的 P0.3+P0.4 是落地动作。
- **IP 改动清单**：见 `docs/IP_CHANGE_CHECKLIST.md`（服务器迁移场景）。

### 6.3 待你确认的决策点
- P0.4 是否立即落代码？以及是否一并加 systemd 单元（P0.3）？
- P1.1 合并 WVP 双配置前，是否需要先 `docker volume ls` 评估 WVP 数据卷重要性（决定能否直接合并）？
- P2.1 模板式自动划线做 Level 1 还是 Level 1+2？
- 是否授权执行 P1.2 的实际删除（删 `setting-server-bak/`、`noprobe` 脚本、镜像内冗余副本）？

---

## 7. 优化项 Top 10（优先级速查）

| 序 | 项 | 阶段 | 优先级 |
|---|---|---|---|
| 1 | 锁定依赖版本（requirements.lock） | P0 | 高 |
| 2 | 合并 setting-server 双份配置（消漂移） | P1 | 高 |
| 3 | 离线构建闭环（tar + 文档 + 权重校验） | P0 | 高 |
| 4 | 开机自启 + 重启竞态补偿（systemd + 网络自愈） | P0 | 高 |
| 5 | 启动脚本幂等闭环（WVP 段 detect-and-start） | P0 | 高 |
| 6 | 最小 pytest 套件 + CI | P3 | 中 |
| 7 | 清理冗余（bak/noprobe/镜像内副本/乱码文档） | P1 | 中 |
| 8 | 配置收口（删孤儿 env、IP/密码单一真源） | P1 | 中 |
| 9 | 移除 tile-preview.html 的 CDN 引用 + dist 一致性校验 | P0/P1 | 中 |
| 10 | 代码级健壮性（裸 except 补日志、共享状态加锁、拆胖文件） | P3 | 低 |
