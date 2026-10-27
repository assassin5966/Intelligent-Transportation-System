#!/bin/bash
# ============================================================
# WVP + ZLMediaKit + 业务套 一键启动 (GPU 服务器部署版)
#
# 与 scripts/start_all.sh 的唯一区别: 业务套改用独立的 GPU 编排文件
#   docker compose -f docker-compose.gpu.yml
#   (ai/backend 用 Dockerfile.gpu 构建的 smart-city-platform:gpu, ai 预留 8 张 4090)
# 启动流程不内置 GPU 探针 (与刚拉起的服务抢占 GPU/CUDA 上下文有冲突);
# 需要验证 GPU 环境时单独执行: bash scripts/check_gpu_env.sh --run
# (独立容器探针, 不影响业务; start_all_gpu_noprobe.sh 与本脚本现已等价, 保留兼容)
#
# 用途: 在 GPU 服务器 (10.10.10.11) 上一键拉起
#   - setting-server-gpu/ 的 WVP 套 (mysql/redis/zlm/wvp)
#   - 主项目的业务套 (backend/ai/mysql/redis)
#   - 大屏前端 (frontend)
# 幂等: 可重复执行; 已建表则跳过导入, 已启动则保持运行。
#       关键: WVP 段采用 detect-and-start —— 容器已存在则直接 docker start,
#             不重复 `up -d` 重建, 彻底绕开 setting-server / setting-server-gpu
#             project 名歧义导致的容器撞名失败 (见 PROJECT_OPTIMIZATION_PLAN.md 2.4)。
#
# 用法:
#   bash scripts/start_all_gpu.sh
#   可选: bash scripts/start_all_gpu.sh --no-backend   # 只启 WVP 套, 不启业务套
#
# 前置: 1) 已确认 setting-server-gpu/ 下 application.yml / conf/config.ini / docker-compose.yml 三处密钥一致
#       2) 根目录 .env.gpu 已写好 WVP_ENABLED / ZLM_PUBLIC_BASE 等业务开关 (本脚本自动 --env-file 指向它)
#       3) GPU 镜像已就绪: docker load -i smart-city-platform_gpu.tar
#          (或 docker compose -f docker-compose.gpu.yml build ai)
#       4) 宿主机已装 NVIDIA 驱动 + nvidia-container-toolkit (docker info 能看到 nvidia runtime)
# 服务器信息: 内网 IP 10.10.10.11 / 镜像源 docker.xuanyuan.run (镜像已拉取)
# ============================================================
set -euo pipefail

START_BACKEND=1
[ "${1:-}" = "--no-backend" ] && START_BACKEND=0

# ---------- 路径 ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SETTING_DIR="$ROOT_DIR/setting-server-gpu"
COMPOSE_SETTING="$SETTING_DIR/docker-compose.yml"
COMPOSE_ROOT="$ROOT_DIR/docker-compose.gpu.yml"
ENV_FILE="$ROOT_DIR/.env.gpu"   # GPU 侧环境变量 (compose 不会自动读, 必须显式 --env-file)

# ---------- 关键参数（与配置文件保持一致） ----------
MYSQL_CONTAINER="wvp-upper-mysql"
WVP_CONTAINER="wvp-upper-wvp"
DB_NAME="wvp"
DB_USER="wvp"
DB_PASS="Wvp@123456"          # = docker-compose.yml 的 MYSQL_PASSWORD
WVP_WEB="http://127.0.0.1:18080/"
ZLM_SECRET="zlm_secret_7890"  # = 三处一致的密钥 #1
SERVER_IP="10.10.10.11"       # 服务器内网 IP (用于打印访问地址; 自动探测优先, 取不到时用它)
GPU_IMAGE="smart-city-platform:gpu"

log()  { printf '\n\033[1;36m[%s]\033[0m %s\n' "$(date '+%H:%M:%S')" "$*"; }
ok()   { printf '\033[1;32m  ✔\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !\033[0m %s\n' "$*"; }

# 等一个容器内的 mysql 可执行查询
wait_mysql() {
    log "等待 MySQL 就绪 ($MYSQL_CONTAINER) ..."
    for i in $(seq 1 60); do
        if docker exec "$MYSQL_CONTAINER" mysql -u"$DB_USER" -p"$DB_PASS" -e "SELECT 1;" >/dev/null 2>&1; then
            ok "MySQL 就绪"
            return 0
        fi
        sleep 2
    done
    warn "MySQL 未在预期时间内就绪，继续尝试（可稍后手动建表）"
}

# 检查是否需要导建表脚本 (全新库时库可能已自动创建但空表, 故按"表是否存在"判断, 而非 SHOW TABLES 退出码)
need_schema() {
    local n
    n="$(docker exec "$MYSQL_CONTAINER" mysql -N -u"$DB_USER" -p"$DB_PASS" "$DB_NAME" \
        -e "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='$DB_NAME' AND TABLE_NAME='wvp_media_server';" 2>/dev/null)"
    if [ "$n" = "1" ]; then
        return 1   # 已存在 wvp_media_server -> 不需要导入
    fi
    return 0        # 表不存在 -> 需要导入建表脚本
}

import_schema() {
    log "WVP 表缺失, 从 WVP 镜像提取建表脚本并导入 ..."
    local tmp_name="wvp-sql-helper"
    docker rm -f "$tmp_name" >/dev/null 2>&1 || true
    docker create --name "$tmp_name" "docker.xuanyuan.run/wangwuli/wvp:2.7.1-2024110702" >/dev/null
    docker cp "$tmp_name:/opt/wvp/mysql.sql" "$SETTING_DIR/mysql.sql"
    docker rm -f "$tmp_name" >/dev/null 2>&1 || true
    docker exec -i "$MYSQL_CONTAINER" mysql -u"$DB_USER" -p"$DB_PASS" "$DB_NAME" \
        < "$SETTING_DIR/mysql.sql"
    rm -f "$SETTING_DIR/mysql.sql"
    ok "建表脚本已导入"
    log "重启 WVP 以加载新表 ..."
    docker restart "$WVP_CONTAINER" >/dev/null 2>&1 || true
}

wait_wvp() {
    log "等待 WVP Web 就绪 ($WVP_WEB) ..."
    for i in $(seq 1 60); do
        local code
        code="$(curl -s -m 3 -o /dev/null -w "%{http_code}" "$WVP_WEB" 2>/dev/null || true)"
        if [ "$code" = "200" ]; then
            ok "WVP Web 就绪 (HTTP 200)"
            return 0
        fi
        sleep 2
    done
    warn "WVP 未在预期时间内就绪，请看 docker logs $WVP_CONTAINER"
}

# ---------- 主流程 ----------
cd "$ROOT_DIR"

if [ ! -f "$COMPOSE_SETTING" ]; then
    echo "错误: 找不到 $COMPOSE_SETTING"; exit 1
fi
if [ ! -f "$COMPOSE_ROOT" ]; then
    echo "错误: 找不到 $COMPOSE_ROOT"; exit 1
fi
if [ ! -f "$ENV_FILE" ]; then
    echo "错误: 找不到 $ENV_FILE"; exit 1
fi

log "======== 1/6 语法自检 (setting / GPU 业务套) ========"
docker compose -f "$COMPOSE_SETTING" config --quiet && ok "setting compose 语法 OK"
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_ROOT" config --quiet && ok "GPU 业务套语法 OK (.env.gpu)"

# project 互斥提示: CPU/GPU 两套 compose 同名 `dt`, 同机不可同时运行。
# 若已有 dt-ai 容器但其镜像不是本脚本的 GPU 镜像, 提示可能误混跑另一套。
if [ "$START_BACKEND" = "1" ] && docker inspect --type container dt-ai >/dev/null 2>&1; then
    _img="$(docker inspect -f '{{.Config.Image}}' dt-ai 2>/dev/null || true)"
    if [ "$_img" != "$GPU_IMAGE" ]; then
        warn "检测到 dt-ai 容器已存在, 镜像为 '$_img' 而非 '$GPU_IMAGE'; 同机 CPU/GPU 两套 compose 同名(dt)会冲突"
        warn "如需切换版本, 请先 bash scripts/stop_all.sh / stop_all_gpu.sh 清理旧容器"
    fi
fi

if [ "$START_BACKEND" = "1" ]; then
    if docker image inspect "$GPU_IMAGE" >/dev/null 2>&1; then
        ok "GPU 镜像就绪: $GPU_IMAGE"
    else
        warn "缺少镜像 $GPU_IMAGE, 请先: docker load -i <平台 GPU 镜像 tar>"; exit 1
    fi
    if docker info 2>/dev/null | grep -qi nvidia; then
        ok "nvidia-container-runtime 已就绪"
    else
        warn "未检测到 nvidia runtime, ai 容器将无法启动 (先装 nvidia-container-toolkit)"
    fi
fi

# WVP 套 detect-and-start: 若整套已部署过(wvp-upper-wvp 容器存在),
# 直接 docker start 全部容器, 不重复 `up -d` 重建 -> 彻底绕开
# setting-server / setting-server-gpu 的 project 名歧义导致的容器撞名失败
if docker inspect --type container "$WVP_CONTAINER" >/dev/null 2>&1; then
    log "======== 2/6 WVP 套已存在 -> 直接 docker start (幂等, 跳过重建) ========"
    docker start "$MYSQL_CONTAINER" "$WVP_CONTAINER" >/dev/null 2>&1 || true
    for c in wvp-upper-redis wvp-upper-zlm; do
        docker start "$c" >/dev/null 2>&1 || true
    done
    wait_mysql
    wait_wvp
else
    log "======== 2/6 启动 WVP 套基础服务 (mysql/redis) ========"
    docker compose -f "$COMPOSE_SETTING" up -d mysql redis

    wait_mysql

    log "======== 3/6 检查建表脚本 (全新库必做) ========"
    if need_schema; then
        import_schema
    else
        ok "已有表结构, 跳过导入"
    fi

    log "======== 4/6 启动 WVP 套全部 (mysql/redis/zlm/wvp) ========"
    docker compose -f "$COMPOSE_SETTING" up -d

    wait_wvp
fi

log "======== 5/6 验证 ZLM API ========"
zlm_code="$(curl -s -m 5 "http://127.0.0.1:12081/index/api/getServerConfig?secret=$ZLM_SECRET" | head -c 120 || true)"
echo "  $zlm_code" | sed 's/^/  /'

if [ "$START_BACKEND" = "1" ]; then
    # 共享网预检: 业务套 backend/ai 需通过 wvp-shared 按服务名访问 WVP/ZLM。
    # WVP 套已在本脚本 step2~4 创建该网络; 若缺失 (例如曾被 prune), 提示用户先单独起 WVP 套。
    if ! docker network inspect wvp-shared >/dev/null 2>&1; then
        warn "wvp-shared 网络不存在, 业务套将无法访问 WVP/ZLM; 请先确认 WVP 套已启动"
    fi
    # 确保业务默认网存在 (迁移/prune 后可能丢失, 导致旧容器残留死网络 ID 引用)。
    # 注意: 不要用 `docker network create dt_default` 手工创建 —— 手工网没有
    # com.docker.compose.network 标签, compose 会告警 "was not created by compose"。
    # 缺网时清掉残留容器, 交由 compose up 自己按正确标签重建网络。
    if ! docker network inspect dt_default >/dev/null 2>&1; then
        warn "dt_default 网络缺失, 清理残留业务容器后由 compose 重建网络"
        docker compose --env-file "$ENV_FILE" -f "$COMPOSE_ROOT" down --remove-orphans >/dev/null 2>&1 || true
    fi
    log "======== 6/6 启动业务套与大屏前端 (GPU 镜像 + 8 卡预留, 走共享网 wvp-shared) ========"
    # 去掉无条件 --force-recreate: compose up -d 本身"有变更才重建"(配置/镜像变更时自动重建),
    # 避免每次重启都全量重建 mysql/redis 造成不必要抖动
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_ROOT" up -d

    # 启动流程不做 GPU 探测/推理 (与刚拉起的服务抢占 GPU/CUDA 上下文有冲突);
    # 需要验证 GPU 环境时单独执行: bash scripts/check_gpu_env.sh --run
else
    log "======== 6/6 已跳过业务套与大屏前端 (--no-backend) ========"
fi

# ---------- 输出访问地址 ----------
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -z "$LAN_IP" ] && LAN_IP="$SERVER_IP"

log "======== 启动完成 (服务器: $LAN_IP, GPU 版) ========"
echo ""
echo "  WVP 平台         http://$LAN_IP:18080/          (admin / admin)"
echo "  ZLM webassist    http://$LAN_IP:12081/webassist/index.html"
echo "  ZLM API          http://$LAN_IP:12081/index/api/getMediaList?secret=$ZLM_SECRET"
if [ "$START_BACKEND" = "1" ]; then
    echo "  大屏前端        http://$LAN_IP:5173"
    echo "  后端 API         http://$LAN_IP:8000/api/health"
    echo "  后端设备列表     http://$LAN_IP:8000/api/devices"
fi
echo "  停止             bash scripts/stop_all_gpu.sh"
echo ""

# ---------- 输出服务日志查看命令 ----------
log "======== 服务日志查看命令 ========"
echo "  # WVP 套 (容器名固定: wvp-upper-*)"
echo "  WVP 平台日志     docker logs -f wvp-upper-wvp"
echo "  ZLM 流媒体日志   docker logs -f wvp-upper-zlm"
if [ "$START_BACKEND" = "1" ]; then
    echo "  # 业务套 (容器名固定: dt-*)"
    echo "  AI 推理日志      docker logs -f dt-ai          # 关注: 推理模式/引擎数/各路 fps"
    echo "  后端日志         docker logs -f dt-backend     # 关注: WVP 设备同步/事件上报"
    echo "  前端容器日志     docker logs -f dt-frontend"
    echo "  # 组合跟踪 (Ctrl+C 退出)"
    echo "  AI+后端          docker logs -f dt-ai dt-backend"
fi
echo ""
