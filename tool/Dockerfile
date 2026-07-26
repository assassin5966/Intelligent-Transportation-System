FROM python:3.11-slim

RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        ffmpeg \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

COPY requirements.txt /app/requirements.txt
RUN ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "amd64" ]; then \
        pip install --no-cache-dir torch==2.3.1 torchvision==0.18.1 --index-url https://download.pytorch.org/whl/cpu && \
        pip install --no-cache-dir -r /app/requirements.txt; \
    else \
        pip install --no-cache-dir -r /app/requirements.txt; \
    fi

COPY app /app/app
COPY tool /app/tool
COPY models /app/models

RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

RUN python -c "from ultralytics import YOLO; YOLO('models/yolo11n.pt')"

ENV PYTHONUNBUFFERED=1

CMD ["python", "/app/tool/video_processor.py", "--help"]