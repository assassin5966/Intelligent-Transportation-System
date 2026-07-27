"""全局配置 (pydantic-settings, 从环境变量 / .env 读取)."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---- 数据存储 ----
    redis_url: str = "redis://redis:6379/0"

    # ---- 服务间通信 ----
    backend_url: str = "http://backend:8000"
    ai_service_url: str = "http://ai:8001"

    # ---- 服务端口 ----
    backend_port: int = 8000
    ai_port: int = 8001

    # ---- AI 视觉分析 ----
    yolo_model: str = "models/yolo11n.pt"
    yolo_conf: float = 0.4
    yolo_iou: float = 0.5
    track_buffer: int = 30

    # ---- 时序预测 ----
    chronos_model: str = "models"  # 本地 Chronos-2 模型目录
    prediction_horizon: int = 60
    prediction_history_hours: int = 168

    # ---- 告警 ----
    rules_file: str = "configs/rules.yaml"

    # ---- Redis 实时状态 key 前缀 ----
    redis_prefix: str = "sc"

    # ---- 安全 ----
    cors_origins: str = "*"  # 允许的跨域来源, 逗号分隔; 生产环境应配置具体前端域名


settings = Settings()
