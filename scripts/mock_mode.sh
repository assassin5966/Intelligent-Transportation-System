#!/bin/bash
# ============================================================
# Mock 模拟模式一键开关
#
# 用途: 开启/关闭 Mock 数据模拟 (不拉流不推理, 由模拟器按真实车流/人流
#       规律生成 32 设备的事件+拥挤数据, 走正常链路入 Redis/MySQL/前端).
#       开启时后端自动禁用 WVP 同步 (互斥保护), 无需手动改 WVP_ENABLED.
#
# 用法:
#   bash scripts/mock_mode.sh start           # 开启 mock (读 .env, CPU 版)
#   bash scripts/mock_mode.sh stop            # 停止 mock (改回 false 并停掉 backend/ai)
#   bash scripts/mock_mode.sh status          # 查看当前状态
#   bash scripts/mock_mode.sh start --gpu     # GPU 服务器版 (读 .env.gpu +
#                                             #   docker-compose.gpu.yml)
#   bash scripts/mock_mode.sh stop --gpu
#
# 说明:
#   - 只操作 backend/ai (+拉起 frontend), 不负责真实拉流模式;
#     真实拉流一律用 scripts/start_all.sh / start_all_gpu.sh 启动
#   - 关闭后恢复 .env 原值, 正式部署务必确认 MOCK_ENABLED=false
# ============================================================
set -euo pipefail

ACTION="${1:-status}"
[ "${2:-}" = "--gpu" ] && IS_GPU=1 || IS_GPU=0

# ---------- 路径 ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ "$IS_GPU" = "1" ]; then
    ENV_FILE="$ROOT_DIR/.env.gpu"
    COMPOSE_ARGS=(--env-file "$ENV_FILE" -f "$ROOT_DIR/docker-compose.gpu.yml")
else
    ENV_FILE="$ROOT_DIR/.env"
    COMPOSE_ARGS=(-f "$ROOT_DIR/docker-compose.yml")
fi

log()  { printf '\n\033[1;36m[%s]\033[0m %s\n' "$(date '+%H:%M:%S')" "$*"; }
ok()   { printf '\033[1;32m  ✔\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !\033[0m %s\n' "$*"; }

# 兼容 macOS/BSD 与 GNU sed 的原地编辑
sed_edit() {  # sed_edit <表达式> <文件>
    sed -i.bak "$1" "$2" && rm -f "$2.bak"
}

# 等待 AI 服务就绪并返回 mock 状态 (native curl)
wait_ai_ready() {
    for _ in $(seq 1 30); do
        body="$(curl -s -m 3 http://localhost:8001/health 2>/dev/null || true)"
        if echo "$body" | grep -q '"status":"ok"'; then
            echo "$body"
            return 0
        fi
        sleep 2
    done
    echo ""
    return 1
}

set_mock() {  # set_mock <true|false>
    local value="$1"
    if grep -Eq "^MOCK_ENABLED=" "$ENV_FILE"; then
        sed_edit "s/^MOCK_ENABLED=.*/MOCK_ENABLED=$value/" "$ENV_FILE"
    else
        echo "错误: $ENV_FILE 中没有 MOCK_ENABLED 配置行"; exit 1
    fi
}

# 从 /health 响应提取 active_devices 数量 (兼容 BSD/GNU, 不用 sed 反向引用)
dev_count() {  # dev_count <health响应体>
    echo "$1" | grep -o '"active_devices":[0-9]*' | grep -o '[0-9]*' || echo "?"
}

cd "$ROOT_DIR"

case "$ACTION" in
    start)
        log "开启 Mock 模拟模式 ($( [ "$IS_GPU" = "1" ] && echo "GPU 版 / .env.gpu" || echo "CPU 版 / .env" )) ..."
        set_mock true
        docker compose "${COMPOSE_ARGS[@]}" up -d --force-recreate backend ai
        # 前端若未运行则拉起 (up 幂等: 已运行则不动, 不会重建)
        docker compose "${COMPOSE_ARGS[@]}" up -d frontend >/dev/null
        log "等待 AI 服务就绪 (backend 健康检查通过后 ai 才启动, 约 30~60s) ..."
        body="$(wait_ai_ready || true)"
        if echo "$body" | grep -q '"mock":true'; then
            ok "Mock 模式已开启: 模拟设备 $(dev_count "$body") 台"
            ok "WVP 同步已被互斥保护自动禁用"
            warn "正式部署前记得: bash scripts/mock_mode.sh stop (停止模拟)"
        else
            warn "AI 服务未在预期时间内就绪或 mock 未生效, 请查看: docker logs dt-ai-1"
            exit 1
        fi
        echo ""
        echo "  前端大屏    http://localhost:5173"
        echo "  后端 API    http://localhost:8000/api/stats/realtime"
        ;;
    stop)
        log "停止 Mock 模拟服务 ..."
        set_mock false
        docker compose "${COMPOSE_ARGS[@]}" stop backend ai
        ok "Mock 模拟已停止 (MOCK_ENABLED=false, backend/ai 已停止)"
        echo ""
        echo "  如需真实拉流计数, 请运行: bash scripts/start_all$( [ "$IS_GPU" = "1" ] && echo "_gpu" ).sh"
        ;;
    status)
        mock_env="$(grep -E "^MOCK_ENABLED=" "$ENV_FILE" | cut -d= -f2 || echo '?')"
        body="$(curl -s -m 3 http://localhost:8001/health 2>/dev/null || echo '')"
        echo "  配置文件 $(basename "$ENV_FILE"): MOCK_ENABLED=$mock_env"
        if echo "$body" | grep -q '"mock":true'; then
            ok "运行状态: Mock 模拟中 ($(dev_count "$body") 台模拟设备)"
        elif echo "$body" | grep -q '"mock":false'; then
            ok "运行状态: 真实拉流计数 ($(dev_count "$body") 台设备)"
        else
            warn "运行状态: AI 服务未启动或不可达"
        fi
        ;;
    *)
        echo "用法: bash scripts/mock_mode.sh {start|stop|status} [--gpu]"
        exit 1
        ;;
esac
