#!/bin/bash
# ============================================================
# WVP + ZLMediaKit + 业务套 一键启动 (GPU 服务器部署版 / 无探针)
#
# 与 scripts/start_all_gpu.sh 的唯一区别: 去掉启动流程末尾的
#   "GPU 首推验证" 探针 (stage1-stage6)。探针会在容器内做 CUDA 初始化
#   和推理, 与刚拉起的服务抢占 GPU/CUDA 上下文, 有冲突导致启动卡顿,
#   故本版本启动时完全不做 GPU 探测; 需要验证 GPU 时单独跑原脚本
#   scripts/start_all_gpu.sh (服务已在跑时其探针逻辑同样适用)。
#
# 用途: 在 GPU 服务器 (23.45.1.115) 上一键拉起
#   - setting-server-gpu/ 的 WVP 套 (mysql/redis/zlm/wvp)
#   - 主项目的业务套 (backend/ai/mysql/redis)
#   - 大屏前端 (frontend)
# 幂等: 可重复执行; 已建表则跳过导入, 已启动则保持运行
#
# 用法:
#   bash scripts/start_all_gpu_noprobe.sh
#   可选: bash scripts/start_all_gpu_noprobe.sh --no-backend   # 只启 WVP 套, 不启业务套
#
# 前置: 1) 已确认 setting-server-gpu/ 下 application.yml / conf/config.ini / docker-compose.yml 三处密钥一致
#       2) 根目录 .env.gpu 已写好 WVP_ENABLED / ZLM_PUBLIC_BASE 等业务开关 (本脚本自动 --env-file 指向它)
#       3) GPU 镜像已就绪: docker load -i smart-city-platform_gpu.tar
#          (或 docker compose -f docker-compose.gpu.yml build ai)
#       4) 宿主机已装 NVIDIA 驱动 + nvidia-container-toolkit (docker info 能看到 nvidia runtime)
# 服务器信息: 内网 IP 23.45.1.115 / 镜像源 docker.xuanyuan.run (镜像已拉取)
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
SERVER_IP="23.45.1.115"       # 服务器对外 IP (用于打印访问地址, 统一固定值)
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

log "======== 5/6 验证 ZLM API ========"
zlm_code="$(curl -s -m 5 "http://127.0.0.1:12081/index/api/getServerConfig?secret=$ZLM_SECRET" | head -c 120 || true)"
echo "  $zlm_code" | sed 's/^/  /'

if [ "$START_BACKEND" = "1" ]; then
    log "======== 6/6 启动业务套与大屏前端 (GPU 镜像 + 8 卡预留, 走共享网 wvp-shared) ========"
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_ROOT" up -d --force-recreate backend ai frontend

    # 无探针版: 启动时不做任何 GPU 探测/推理 (与刚拉起的服务抢占 GPU 有冲突),
    # GPU 健康验证需要时单独执行 scripts/start_all_gpu.sh 或容器内手动自检
    if grep -Eq "^MOCK_ENABLED=true" "$ENV_FILE"; then
        warn "MOCK_ENABLED=true: Mock 模拟模式 (不拉流不推理)"
        warn "  WVP 同步已被后端自动禁用 (互斥保护); 正式部署前记得把 .env.gpu 的 MOCK_ENABLED 改回 false"
    fi
else
    log "======== 6/6 已跳过业务套与大屏前端 (--no-backend) ========"
fi

# ---------- 输出访问地址 ----------
LAN_IP="$SERVER_IP"

log "======== 启动完成 (服务器: $LAN_IP, GPU 版 / 无探针) ========"
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
