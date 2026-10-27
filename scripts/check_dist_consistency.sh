#!/usr/bin/env bash
# ============================================================
# 前端 dist 一致性校验 (PROJECT_OPTIMIZATION_PLAN.md Top10 #9)
#
# 作用: 离线部署前确认大屏前端产物 dist 与源码一致、且资源完整,
#       避免"改了源码没 build / build 产物缺失"导致白屏或加载失败。
#
# 用法:
#   bash scripts/check_dist_consistency.sh
#   bash scripts/check_dist_consistency.sh --src src --dist map-marking-system-vue/dist
#
# 退出码: 0=通过; 1=存在问题 (ERROR); 2=仅警告 (WARN, 如 dist 可能过期)
# ============================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="$ROOT/map-marking-system-vue/src"
DIST_DIR="$ROOT/map-marking-system-vue/dist"
VUE_DIR="$ROOT/map-marking-system-vue"

while [ $# -gt 0 ]; do
    case "$1" in
        --src)  SRC_DIR="$2"; shift 2 ;;
        --dist) DIST_DIR="$2"; shift 2 ;;
        *) echo "未知参数: $1"; exit 2 ;;
    esac
done

RC=0
err() { echo "  [ERROR] $*"; RC=1; }
warn() { echo "  [WARN]  $*"; [ "$RC" -lt 2 ] && RC=2; }
ok()  { echo "  [OK]    $*"; }

echo ">>> 校验前端 dist: $DIST_DIR"

# 1) dist 是否存在
if [ ! -d "$DIST_DIR" ]; then
    err "dist 目录不存在: $DIST_DIR"
    echo "    请先在 $VUE_DIR 执行: npm install && npm run build"
    exit 1
fi

# 2) dist 非空
if [ -z "$(ls -A "$DIST_DIR" 2>/dev/null)" ]; then
    err "dist 目录为空, 请重新 npm run build"
    exit 1
else
    ok "dist 非空"
fi

# 3) index.html 引用的本地 .js/.css 是否都在 dist 内 (排除 http(s) 外链)
if [ -f "$DIST_DIR/index.html" ]; then
    missing=0
    while IFS= read -r asset; do
        [ -f "$DIST_DIR/$asset" ] || { err "index.html 引用缺失: $asset"; missing=1; }
    done < <(grep -oE '(src|href)="[^"]+"' "$DIST_DIR/index.html" \
                | sed -E 's/.*="([^"]+)".*/\1/' \
                | grep -vE '^(https?:|//|data:|#)' \
                | sed 's#^\./##')
    [ "$missing" -eq 0 ] && ok "index.html 引用的本地资源均存在"
else
    warn "dist/index.html 不存在, 前端可能无法正常加载"
fi

# 4) 过期检查: src 中是否有比 dist 更新的文件 (提示需重新 build)
if [ -d "$SRC_DIR" ]; then
    newest_src=$(find "$SRC_DIR" -type f -printf '%T@\n' 2>/dev/null | sort -n | tail -1)
    if [ -n "${newest_src:-}" ] && [ -d "$DIST_DIR" ]; then
        newest_dist=$(find "$DIST_DIR" -type f -printf '%T@\n' 2>/dev/null | sort -n | tail -1)
        # 允许 60s 构建抖动
        if [ -n "${newest_dist:-}" ] && awk "BEGIN{exit !($newest_src - $newest_dist > 60)}"; then
            warn "源码(src)存在比 dist 更新的文件, dist 可能已过期, 请重新 npm run build"
        else
            ok "dist 未明显落后于源码"
        fi
    fi
fi

# 5) 离线检查: 是否存在外网 CDN 引用 (leaflet 等应已本地化)
if grep -rqE 'https?://(unpkg|jsdelivr|cdnjs|cdn\.)' "$DIST_DIR" 2>/dev/null; then
    warn "dist 中发现外网 CDN 引用, 离线环境可能加载失败 (请本地化依赖)"
fi

echo ">>> 校验结束 (rc=$RC)"
exit $RC
