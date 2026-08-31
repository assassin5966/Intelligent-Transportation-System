#!/bin/bash
# ============================================================
# WVP + ZLMediaKit + 业务套 一键启动 (服务器部署版)
#
# 用途: 在服务器上一键拉起
#   - setting-server/ 的 WVP 套 (mysql/redis/zlm/wvp)
#   - 主项目的业务套 (backend/ai)
# 幂等: 可重复执行; 已建表则跳过导入, 已启动则保持运行
#
# 用法:
#   bash scripts/start_all.sh
#   可选: bash scripts/start_all.sh --no-backend   # 只启 WVP 套, 不启业务套
#
# 前置: 1) 已按 setting-server/WVP-ZLM-傻瓜式启动教程.md 配置三个文件一致
#       2) 根目录 .env 已写好 WVP_ENABLED 等业务开关
#       3) 根目录 .env 已配置本脚本所需凭据(见 .env.example):
#          WVP_DB_PASSWORD / ZLM_API_SECRET / SERVER_IP
# 服务器信息: 内网 IP / 镜像源 docker.xuanyuan.run (镜像已拉取)
# ============================================================
set -euo pipefail

log()  { printf '\n\033[1;36m[%s]\033[0m %s\n' "$(date '+%H:%M:%S')" "$*"; }
ok()   { printf '\033[1;32m  ✔\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !\033[0m %s\n' "$*"; }

START_BACKEND=1
[ "${1:-}" = "--no-backend" ] && START_BACKEND=0

# ---------- 路径 ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SETTING_DIR="$ROOT_DIR/setting-server"
COMPOSE_SETTING="$SETTING_DIR/docker-compose.yml"
COMPOSE_ROOT="$ROOT_DIR/docker-compose.yml"

# ---------- 关键参数（从根目录 .env 读取, 不硬编码明文凭据） ----------
ENV_FILE="$ROOT_DIR/.env"
if [ -f "$ENV_FILE" ]; then
    set -a; # shellcheck disable=SC1090
    source "$ENV_FILE"; set +a
else
    warn "未找到 .env, 将从环境变量读取 (需已 export WVP_DB_PASSWORD/ZLM_API_SECRET/SERVER_IP)"
fi

MYSQL_CONTAINER="wvp-upper-mysql"
WVP_CONTAINER="wvp-upper-wvp"
DB_NAME="${WVP_DB_NAME:-wvp}"
DB_USER="${WVP_DB_USER:-wvp}"
DB_PASS="${WVP_DB_PASSWORD:-}"          # 必填: .env 的 WVP_DB_PASSWORD (需与 setting-server compose 的 MYSQL_PASSWORD 一致)
WVP_WEB="http://127.0.0.1:18080/"
ZLM_SECRET="${ZLM_API_SECRET:-}"        # 必填: .env 的 ZLM_API_SECRET (三处一致的密钥 #1)
SERVER_IP="${SERVER_IP:-}"              # 服务器内网 IP (打印访问地址用; 留空则自动探测)

# 校验必填凭据, 缺失即退出并提示
if [ -z "$DB_PASS" ] || [ -z "$ZLM_SECRET" ]; then
    echo "错误: 缺少凭据配置。请在根目录 .env 中填写:"
    echo "  WVP_DB_PASSWORD=你的WVP数据库密码"
    echo "  ZLM_API_SECRET=你的ZLM密钥"
    echo "  SERVER_IP=服务器内网IP (可选, 留空自动探测)"
    echo "参考 .env.example 的说明。"
    exit 1
fi

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

# 检查是否需要导建表脚本
need_schema() {
    docker exec "$MYSQL_CONTAINER" mysql -u"$DB_USER" -p"$DB_PASS" "$DB_NAME" \
        -e "SHOW TABLES LIKE 'wvp_media_server';" >/dev/null 2>&1 \
      && return 1 || return 0
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

log "======== 1/6 语法自检 (setting) ========"
docker compose -f "$COMPOSE_SETTING" config --quiet && ok "compose 语法 OK"

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
    log "======== 6/6 启动业务套 (backend/ai, 走共享网 wvp-shared) ========"
    docker compose -f "$COMPOSE_ROOT" up -d --force-recreate backend ai
else
    log "======== 6/6 已跳过业务套 (--no-backend) ========"
fi

# ---------- 输出访问地址 ----------
LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')"
[ -z "$LAN_IP" ] && LAN_IP="$SERVER_IP"

log "======== 启动完成 (服务器: $LAN_IP) ========"
echo ""
echo "  WVP 平台         http://$LAN_IP:18080/          (admin / admin)"
echo "  ZLM webassist    http://$LAN_IP:12081/webassist/index.html"
echo "  ZLM API          http://$LAN_IP:12081/index/api/getMediaList?secret=$ZLM_SECRET"
if [ "$START_BACKEND" = "1" ]; then
    echo "  后端 API         http://$LAN_IP:8000/api/health"
    echo "  后端设备列表     http://$LAN_IP:8000/api/devices"
fi
echo ""
