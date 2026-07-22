"""全局配置 (pydantic-settings, 从环境变量 / .env 读取)."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---- 数据存储 ----
    redis_url: str = "redis://redis:6379/0"
    mysql_url: str = "mysql+aiomysql://smartcity:smartcity123@mysql:3306/smart_city"

    # ---- 服务间通信 ----
    backend_url: str = "http://backend:8000"
    ai_service_url: str = "http://ai:8001"

    # ---- 服务端口 ----
    backend_port: int = 8000
    ai_port: int = 8001

    # ---- AI 视觉分析 ----
    yolo_model: str = "yolo11n.pt"
    yolo_conf: float = 0.4
    yolo_iou: float = 0.5
    # COCO: 0=person 1=bicycle 2=car 3=motorcycle 5=bus 7=truck
    detect_classes: list[int] = [0, 1, 2, 3, 5, 7]
    track_buffer: int = 30  # ByteTrack 跟踪帧缓冲

    # ---- 时序预测 ----
    chronos_model: str = "amazon/chronos-t5-tiny"
    prediction_horizon: int = 60  # 预测分钟数 (15/30/45/60)

    # ---- 告警 ----
    rules_file: str = "configs/rules.yaml"

    # ---- Redis 实时状态 key 前缀 ----
    redis_prefix: str = "sc"


settings = Settings()
