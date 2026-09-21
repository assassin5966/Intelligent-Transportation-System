#!/bin/bash
# ============================================================
# 离线生产部署 一键停止 (对应 scripts/start_offline.sh)
#
# 用法:
#   bash scripts/stop_offline.sh                # 停止全部容器, 保留数据卷
#   bash scripts/stop_offline.sh --down         # 停止并移除容器 (保留数据卷, 下次 start 全新拉起)
#   bash scripts/stop_offline.sh --no-frontend  # 只停业务套, 大屏前端保持运行
#
# 安全: 不会删除任何卷/镜像, 生产数据不受影响
# ============================================================
set -euo pipefail

MODE="stop"         # stop | down
STOP_FRONTEND=1     # 1=连 frontend 一起停
[ "${1:-}" = "--down" ] && MODE="down"
[ "${1:-}" = "--no-frontend" ] && STOP_FRONTEND=0

# ---------- 路径 ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/docker-compose.offline.yml"
PROJECT="smartcity"

log()  { printf '\n\033[1;36m[%s]\033[0m %s\n' "$(date '+%H:%M:%S')" "$*"; }
ok()   { printf '\033[1;32m  ✔\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !\033[0m %s\n' "$*"; }

cd "$ROOT_DIR"

if [ ! -f "$COMPOSE_FILE" ]; then
    echo "错误: 找不到 $COMPOSE_FILE"; exit 1
fi

[ "$MODE" = "down" ] && ACTION="down" || ACTION="stop"

# 与 start_offline.sh 一致的文件链: gpu 镜像存在则叠加 GPU 覆盖文件
COMPOSE_FILES=(-f "$COMPOSE_FILE")
if docker image inspect smart-city-platform:gpu >/dev/null 2>&1 \
   && [ -f "$ROOT_DIR/docker-compose.offline.gpu.yml" ]; then
    COMPOSE_FILES+=(-f "$ROOT_DIR/docker-compose.offline.gpu.yml")
fi

log "======== 停止离线部署 (compose -p $PROJECT) ========"
if [ "$STOP_FRONTEND" = "1" ]; then
    docker compose "${COMPOSE_FILES[@]}" -p "$PROJECT" "$ACTION" && ok "全部服务已 $ACTION (数据卷保留)"
else
    docker compose "${COMPOSE_FILES[@]}" -p "$PROJECT" "$ACTION" ai backend redis && \
        ok "业务套已 $ACTION, frontend 保持运行"
fi

log "======== 停止完成 ========"
echo ""
docker compose "${COMPOSE_FILES[@]}" -p "$PROJECT" ps --format '  {{.Name}}  {{.State}}' 2>/dev/null || true
echo ""
echo "  数据卷未删除, 重新启动: bash scripts/start_offline.sh"
