#!/usr/bin/env bash
# ============================================================
# 生成精确依赖锁文件 requirements.lock.txt (P0.1 锁依赖)
#
# 原则: 目标部署机无外网, 不能在上面 pip install; 必须在【联网构建机】
#       上冻结当前解析到的版本, 把 requirements.lock.txt 随镜像/离线 tar 一起交付,
#       Dockerfile 改用 `pip install -r requirements.lock.txt` 即可离线可复现。
#
# 用法 (在联网构建机执行, 架构需与部署目标一致):
#   # 1) 建议先用虚拟环境干净安装一遍区间约束的依赖
#   python -m venv .venv && source .venv/bin/activate
#   pip install -U pip
#   pip install -r requirements.txt
#
#   # 2) 生成锁
#   bash scripts/gen_requirements_lock.sh
#   # 或指定已存在的 venv:
#   VENV=.venv bash scripts/gen_requirements_lock.sh
#
# 输出: 项目根目录 requirements.lock.txt (精确 == 版本, 可直接喂给 pip)
#
# 说明:
#   - 锁的是【业务依赖】; torch/torchvision 在 Dockerfile 中按架构单独装
#     (CPU 走 pytorch 官方 CPU 源 / GPU 走 cu121 源), 不在此锁范围内。
#   - 如需带 hash 的更强锁定, 可后续用 `pip-compile --generate-hashes`
#     (pip-tools) 替换本脚本的简单 freeze。
#   - 定期更新: 联网机上 `pip install -U -r requirements.txt` 后重跑本脚本。
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ -n "${VENV:-}" ]; then
    # shellcheck disable=SC1090
    source "$VENV/bin/activate"
fi

echo ">>> 生成 requirements.lock.txt (精确 == 版本)"
pip install --upgrade pip >/dev/null
# 确保环境里含项目依赖 (并集, 不会覆盖已装的精确版本)
pip install -r requirements.txt >/dev/null 2>&1 || true
# 冻结已装依赖 (仅当前环境本地包, 不含 -e 开发包)
pip freeze --local > requirements.lock.txt

echo ">>> 已写入 $ROOT/requirements.lock.txt"
echo ">>> 行数: $(wc -l < requirements.lock.txt)"
echo ">>> 后续构建请将该文件 COPY 进镜像并改用: pip install -r /app/requirements.lock.txt"
echo ">>> (详见 docs/OFFLINE_BUILD.md 的「离线锁依赖」一节)"
