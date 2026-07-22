# ============================================================
# 智慧古城车辆人流监管平台 - AI 分析 + 业务后端 统一镜像
# 一个镜像, 两个服务 (AI 服务 / 后端服务) 通过不同启动命令区分
#
# 构建 (默认 CPU):
#   docker build -t smart-city-platform:latest .
#
# 构建 (CUDA 12.1, 需主机支持 nvidia-docker):
#   docker build --build-arg TORCH_INDEX=https://download.pytorch.org/whl/cu121 \
#                -t smart-city-platform:latest .
# ============================================================

ARG PYTHON_VERSION=3.11

FROM python:${PYTHON_VERSION}-slim AS base

# ---------- 1. 系统依赖 ----------
# libgl1/libglib2.0-0: OpenCV 运行库
# libgomp1: OpenMP (PyTorch/YOLO 多线程)
# ffmpeg: RTSP 视频流解码
# gcc/g++: 部分 Python 包编译
# 切换 debian 源到清华镜像 (加速国内 apt)
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        ffmpeg \
        gcc \
        g++ \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 国内 PyPI 镜像加速 (torch 仍走 pytorch 官方 CPU 源, 见下方 --index-url)
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ---------- 2. 安装 PyTorch (独立层, 利用缓存; 自动适配 CPU 架构) ----------
# x86_64 (amd64): 用 PyTorch 官方 CPU 源 (轻量约 200MB, 无 CUDA)
# aarch64 (arm64): 官方 CPU 源无对应 wheel, 改用 PyPI (aarch64 版, 含 CUDA 可用于 GPU 推理)
# 如需 amd64 + NVIDIA GPU: 把下方 amd64 分支的 --index-url 改为
#   https://download.pytorch.org/whl/cu121
RUN ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "amd64" ]; then \
        pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu; \
    else \
        pip install --no-cache-dir torch torchvision; \
    fi

# ---------- 3. 安装业务依赖 ----------
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# ---------- 3.5 应用代码 (镜像自包含; compose 挂载会覆盖用于开发热加载) ----------
COPY app /app/app
COPY configs /app/configs
COPY scripts /app/scripts

# ---------- 4. 环境配置 ----------
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LANG=C.UTF-8
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 预下载 YOLO11 模型权重到镜像内, 避免运行时联网下载
# (首次构建会拉取 yolov11n.pt, 离线/内网部署友好)
RUN python -c "from ultralytics import YOLO; YOLO('yolo11n.pt')" || true

# ---------- 5. 端口 ----------
# 8000: 业务后端 (FastAPI REST/WebSocket)
# 8001: AI 分析服务 (接收视频流任务 / 对外推送事件)
EXPOSE 8000 8001

# ---------- 6. 启动命令 ----------
# 默认启动业务后端; AI 服务在 docker-compose 中通过 command 覆盖:
#   AI 服务:   python -m app.ai.service
#   后端服务:  uvicorn app.backend.main:app --host 0.0.0.0 --port 8000
CMD ["uvicorn", "app.backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
