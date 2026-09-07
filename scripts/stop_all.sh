#!/bin/bash
# ============================================================
# WVP + ZLMediaKit + 业务套 一键停止 (服务器部署版)
#
# 对应 scripts/start_all.sh 的停止脚本:
#   - 先停业务套 (backend/ai/frontend, 根目录 docker-compose.yml)
#   - 再停 WVP 套 (mysql/redis/zlm/wvp, setting-server/docker-compose.yml)
# 默认保留数据卷, 可重复启动 (start_all.sh 幂等)
#
# 用法:
#   bash scripts/stop_all.sh                # 停止全部容器, 保留数据卷
#   bash scripts/stop_all.sh --down         # 停止并移除容器 (保留数据卷, 下次 start 全新拉起)
#   bash scripts/stop_all.sh --no-backend   # 只停 WVP 套 (业务套未启动时的对称用法)
#
# 安全: 不会删除任何卷/镜像, 生产数据不受影响
# ============================================================
set -euo pipefail

MODE="stop"        # stop | down
STOP_BACKEND=1     # 1=连业务套一起停
[ "${1:-}" = "--down" ] && MODE="down"
[ "${1:-}" = "--no-backend" ] && STOP_BACKEND=0

# ---------- 路径 ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SETTING_DIR="$ROOT_DIR/setting-server"
COMPOSE_SETTING="$SETTING_DIR/docker-compose.yml"
COMPOSE_ROOT="$ROOT_DIR/docker-compose.yml"

log()  { printf '\n\033[1;36m[%s]\033[0m %s\n' "$(date '+%H:%M:%S')" "$*"; }
ok()   { printf '\033[1;32m  ✔\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !\033[0m %s\n' "$*"; }

cd "$ROOT_DIR"

if [ ! -f "$COMPOSE_SETTING" ]; then
    echo "错误: 找不到 $COMPOSE_SETTING"; exit 1
fi

[ "$MODE" = "down" ] && ACTION="down" || ACTION="stop"

log "======== 1/2 停止业务套 (backend/ai/frontend) ========"
if [ "$STOP_BACKEND" = "1" ]; then
    if [ -f "$COMPOSE_ROOT" ]; then
        docker compose -f "$COMPOSE_ROOT" "$ACTION" && ok "业务套已 $ACTION (数据卷保留)"
    else
        warn "未找到 $COMPOSE_ROOT, 跳过业务套"
    fi
else
    warn "已跳过业务套 (--no-backend)"
fi

log "======== 2/2 停止 WVP 套 (mysql/redis/zlm/wvp) ========"
docker compose -f "$COMPOSE_SETTING" "$ACTION" && ok "WVP 套已 $ACTION (数据卷保留)"

log "======== 停止完成 ========"
echo ""
echo "  已$ACTION的容器:"
docker compose -f "$COMPOSE_SETTING" ps --format '  {{.Name}}  {{.State}}' 2>/dev/null || true
[ "$STOP_BACKEND" = "1" ] && [ -f "$COMPOSE_ROOT" ] && \
    docker compose -f "$COMPOSE_ROOT" ps --format '  {{.Name}}  {{.State}}' 2>/dev/null || true
echo ""
echo "  数据卷未删除, 重新启动: bash scripts/start_all.sh"
