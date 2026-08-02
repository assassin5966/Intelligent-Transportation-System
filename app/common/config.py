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
    prediction_interval_minutes: int = 15  # N 分钟预测间隔
    prediction_series_length: int = 30  # 历史序列长度 (30 个 N 分钟区间)
    vehicle_person_min: int = 2  # 每车最少人数 (车流转人流)
    vehicle_person_max: int = 5  # 每车最多人数 (车流转人流)

    # ---- 告警 ----
    rules_file: str = "configs/rules.yaml"

    # ---- Redis 实时状态 key 前缀 ----
    redis_prefix: str = "sc"

    # ---- 警力分配 ----
    police_demand_weight_current: float = 0.3  # 需求计算: 当前人数权重 α
    police_demand_weight_predict: float = 0.7  # 需求计算: 预测人数权重 β
    police_movement_ratio: float = 0.5  # 每轮最大移动比例 (占总警力)
    police_min_per_region: int = 1  # 每区域最少警力

    # ---- 安全 ----
    cors_origins: str = "*"  # 允许的跨域来源, 逗号分隔; 生产环境应配置具体前端域名


settings = Settings()
