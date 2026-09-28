#!/bin/bash
# ============================================================
# GPU 环境六阶段自检 (独立探针, 与服务启动解耦)
#
# 背景: start_all_gpu.sh 内置的首推验证探针会在启动时做 CUDA 初始化
#   和推理, 与刚拉起的服务抢占 GPU 上下文, 有冲突。本脚本把探针单独
#   拆出来, 在服务启动完成、稳定运行后手动执行; 也支持 ai 容器未启动
#   时用临时容器自检 (不占用服务端口, 不影响在跑业务)。
#
# 六阶段: stage1 torch导入/CUDA枚举 → stage2 首次CUDA初始化 →
#         stage3 YOLO构造 → stage4 首次track推理 →
#         stage5 跟踪器API自检 → stage6 多图批量+FP16
#
# 用法:
#   bash scripts/check_gpu_env.sh                # ai 容器在跑: 优先 exec; 不在: 临时容器
#   bash scripts/check_gpu_env.sh --run          # 强制用临时容器 (不进 ai 容器)
#
# 注意: 探针会占 0 号卡显存并短暂打满推理, 建议避开业务高峰; 若
#   ai 服务正在跑, 探针结束后如出现异常可 docker compose restart ai 清理残留上下文。
# 参考: docs/GPU服务器离线排查方案.txt (L0~L6 验收与故障速查)
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_ROOT="$ROOT_DIR/docker-compose.gpu.yml"
ENV_FILE="$ROOT_DIR/.env.gpu"
GPU_IMAGE="smart-city-platform:gpu"
FORCE_RUN=0
[ "${1:-}" = "--run" ] && FORCE_RUN=1

log()  { printf '\n\033[1;36m[%s]\033[0m %s\n' "$(date '+%H:%M:%S')" "$*"; }
ok()   { printf '\033[1;32m  ✔\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !\033[0m %s\n' "$*"; }

# 六阶段探针 (与 start_all_gpu.sh 中的探针逻辑一致, 改动需两处同步)
PROBE_PY='
import time
t0 = time.time(); import torch
print(f"stage1 torch导入={time.time()-t0:.1f}s cuda可用={torch.cuda.is_available()} 卡数={torch.cuda.device_count()}")
t0 = time.time(); x = torch.zeros(8, device="cuda"); torch.cuda.synchronize()
print(f"stage2 首次CUDA初始化={time.time()-t0:.1f}s 设备={torch.cuda.get_device_name(0)}")
t0 = time.time()
import numpy as np
from ultralytics import YOLO
m = YOLO("models/yolo11n.pt")
print(f"stage3 YOLO构造={time.time()-t0:.1f}s")
t0 = time.time()
m.track(np.zeros((1080, 1920, 3), np.uint8), persist=True, tracker="configs/bytetrack.yaml", verbose=False)
print(f"stage4 首次track={time.time()-t0:.1f}s")
# stage5: 新推理引擎依赖的官方跟踪器 API 表面 (列含义/跨帧 ID 稳定)
t0 = time.time()
from app.ai.gpu_engine import _build_tracker, _load_tracker_args
tk = _build_tracker(_load_tracker_args("configs/bytetrack.yaml"))
dummy = np.zeros((544, 960, 3), np.uint8)
det = np.array([[400, 400, 600, 600, 0.9, 2]], dtype=np.float32)
r1 = np.asarray(tk.update(det, dummy)); det2 = det.copy(); det2[0, :4] += 10
r2 = np.asarray(tk.update(det2, dummy))
assert len(r1) and len(r2) and r1.shape[1] >= 7 and int(r1[0][4]) == int(r2[0][4]), "tracker API 不符预期"
print(f"stage5 跟踪器API自检={time.time()-t0:.1f}s 列数={r1.shape[1]} id={int(r1[0][4])}")
# stage6: 多图同批 + FP16 (新引擎的批量路径; 仅 0 号卡, 不动其他卡)
t0 = time.time()
imgs = [np.zeros((544, 960, 3), np.uint8) for _ in range(4)]
out = m.predict(imgs, imgsz=960, half=True, device="cuda:0", verbose=False)
torch.cuda.synchronize()
assert len(out) == len(imgs), "批量推理返回数量不符"
print(f"stage6 批量推理(批4@960/half)={time.time()-t0:.1f}s")
'

# 选择执行方式: ai 容器在跑 → compose exec; 否则/强制 → 临时容器 (挂载 app/models/configs)
AI_RUNNING=0
if [ "$FORCE_RUN" = "0" ] && [ -f "$ENV_FILE" ]; then
    if docker compose --env-file "$ENV_FILE" -f "$COMPOSE_ROOT" ps ai 2>/dev/null \
        | grep -qiE '^(dt-)?ai|_ai_| ai ' && \
       [ "$(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_ROOT" ps -q ai 2>/dev/null)" != "" ] && \
       docker compose --env-file "$ENV_FILE" -f "$COMPOSE_ROOT" ps ai 2>/dev/null | grep -qi "up"; then
        AI_RUNNING=1
    fi
fi

if [ "$AI_RUNNING" = "1" ]; then
    log "执行方式: compose exec ai (ai 容器运行中)"
    RUN_CMD=(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_ROOT" exec -T ai)
else
    if ! docker image inspect "$GPU_IMAGE" >/dev/null 2>&1; then
        echo "错误: 找不到镜像 $GPU_IMAGE, 先 docker load -i smart-city-platform_gpu.tar"; exit 1
    fi
    log "执行方式: 临时容器 (ai 容器未运行或 --run)"
    RUN_CMD=(docker run --rm --gpus all -w /app \
        -v "$ROOT_DIR/app:/app/app" \
        -v "$ROOT_DIR/models:/app/models" \
        -v "$ROOT_DIR/configs:/app/configs" \
        "$GPU_IMAGE")
fi

# -k 5: SIGTERM 无效时 (CUDA 卡死在内核模块 ioctl, 进程 D 状态收不到信号)
# 5s 后强制 SIGKILL, 管道关闭脚本才能继续, 否则 timeout 90 形同虚设.
log "GPU 六阶段探针 (torch CUDA 初始化 → YOLO 构造 → 首次推理 → 跟踪器API → 批量推理, 超时 90s) ..."
gpu_check="$(timeout -k 5 90 "${RUN_CMD[@]}" python -c "$PROBE_PY" 2>&1 | tr -d '\r' || true)"

if echo "$gpu_check" | grep -q "stage6"; then
    echo "$gpu_check" | sed 's/^/  /'
    ok "GPU 环境自检通过 (全部 6 阶段完成)"
    exit 0
fi

# ---- 未通过: 按最后完成的阶段给出定位与处置 (同 start_all_gpu.sh 的诊断) ----
echo "$gpu_check" | sed 's/^/  /' >&2
# (nvidia-smi 只走驱动查询路径, 不经过 nvidia_uvm; stage2 的首次显存分配才走 uvm,
#  所以 nvidia-smi 正常不能替代 stage2 验证)
done_stage=0
for n in 1 2 3 4 5; do
    if echo "$gpu_check" | grep -q "stage$n"; then done_stage=$n; fi
done
warn "GPU 环境自检未完成! (共 6 阶段, 最后完成: stage${done_stage}, 卡在 stage$((done_stage + 1)))"
case "$done_stage" in
    0)
        warn "卡点: stage1 (torch 导入/CUDA 枚举) → 容器 GPU 注入问题"
        warn "  排查: docker run --rm --gpus all $GPU_IMAGE nvidia-smi   # 驱动+runtime 注入是否正常"
        warn "        docker info | grep -i nvidia                       # nvidia-container-toolkit 是否装好"
        ;;
    1)
        warn "卡点: stage2 (首次 CUDA 初始化/显存分配) → 驱动 nvidia_uvm 挂起 (进程 D 状态)"
        warn "  解决(按序): 1) docker compose --env-file '$ENV_FILE' -f '$COMPOSE_ROOT' restart ai   # 清残留 GPU 上下文"
        warn "              2) sudo rmmod nvidia_uvm && sudo modprobe nvidia_uvm   # 卸不掉(有 D 进程引用)则 reboot"
        warn "  排查: dmesg -T | grep -iE 'nvrm|xid|uvm' | tail -30   # 出现 Xid=硬件/驱动层错误, 需升级驱动"
        warn "        docker compose --env-file '$ENV_FILE' -f '$COMPOSE_ROOT' exec -T ai env | grep ALLOC_CONF   # 必须无输出"
        ;;
    2)
        warn "卡点: stage3 (YOLO 构造) → 权重/配置文件问题"
        warn "  排查: docker compose --env-file '$ENV_FILE' -f '$COMPOSE_ROOT' exec ai ls -l models/yolo11n.pt configs/bytetrack.yaml"
        warn "        (临时容器模式直接看宿主机: ls -l models/yolo11n.pt configs/bytetrack.yaml)"
        ;;
    3)
        warn "卡点: stage4 (首次 track 推理) → 显存/分配器问题"
        warn "  排查: nvidia-smi   # 看显存占用与残留进程"
        warn "        docker compose --env-file '$ENV_FILE' -f '$COMPOSE_ROOT' exec -T ai env | grep ALLOC_CONF   # expandable_segments 必须未启用"
        warn "  解决: docker compose --env-file '$ENV_FILE' -f '$COMPOSE_ROOT' restart ai 后重试"
        ;;
    4)
        warn "卡点: stage5 (官方跟踪器 API 自检) → ultralytics 版本与预期不符"
        warn "  影响: 批量引擎启动时会自检失败并自动整体降级 legacy (功能不受影响, 但退回每路独立实例)"
        warn "  排查: docker compose --env-file '$ENV_FILE' -f '$COMPOSE_ROOT' exec -T ai python -c 'import ultralytics;print(ultralytics.__version__)'"
        warn "        docker compose --env-file '$ENV_FILE' -f '$COMPOSE_ROOT' exec -T ai python -c 'from app.ai.gpu_engine import _build_tracker,_load_tracker_args;t=_build_tracker(_load_tracker_args(\"configs/bytetrack.yaml\"));import numpy as np;print(np.asarray(t.update(np.array([[400,400,600,600,0.9,2]],np.float32),np.zeros((544,960,3),np.uint8))).shape)'"
        ;;
    5)
        warn "卡点: stage6 (多图批量 + FP16 推理) → 显存不足或该卡不支持半精度"
        warn "  解决: 把 .env.gpu 的 INFER_HALF 改为 false; 仍失败则调小 INFER_IMGSZ 或 INFER_SLOTS_PER_GPU"
        warn "  排查: nvidia-smi   # 看显存余量与残留进程"
        ;;
esac
if [ "$AI_RUNNING" = "1" ]; then
    warn "  超时强杀后容器内可能残留挂起的 exec 进程 (占着 GPU 上下文), 建议执行: docker compose restart ai"
fi
exit 1
