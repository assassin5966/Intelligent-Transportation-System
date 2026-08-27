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
    vehicle_person_min: int = 2  # 每车最少人数 (车流转人流随机采样下界)
    vehicle_person_max: int = 5  # 每车最多人数 (车流转人流随机采样上界)

    # ---- 车流速度与拥挤判断 ----
    congestion_min_flow: float = 5.0  # 拥挤判定: 每分钟跨线车辆数低于此值视为车流速度过低 (辆/分钟)
    person_congestion_min_flow: float = 10.0  # 人流拥挤判定: 每分钟跨线人数低于此值视为人流速度过低 (人/分钟)
    congestion_vehicle_weight: float = 0.5  # 人车混合区域: 车辆拥挤度权重
    congestion_person_weight: float = 0.5  # 人车混合区域: 人流拥挤度权重
    congestion_threshold: float = 0.5  # 人车混合区域: 加权拥挤度达到此值判定为拥挤 (0-1)
    roi_report_interval: float = 2.0  # AI 每 N 秒上报一次 ROI 内车辆数 (供拥挤判断)

    # ---- 小时级车流/人流量 (记录每天每小时) ----
    hourly_retention_days: int = 30  # 小时级数据保留天数 (Redis 自动过期)

    # ---- MySQL 长期归档 (小时级车流/人流量落库, 长期报表) ----
    mysql_enabled: bool = False  # 总开关; False 时跳过归档调度与落库
    mysql_host: str = "mysql"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: str = "root123456"
    mysql_database: str = "dt_stats"
    archive_interval_seconds: int = 300  # 归档调度周期 (秒), 仅归档已完成小时
    mysql_retention_days: int = 365  # MySQL 长期保留天数 (定期清理)

    # ---- 告警 ----
    rules_file: str = "configs/rules.yaml"

    # ---- 业务规则集中配置 (热重载) ----
    # 计数/告警/拥挤/警力/预测/异常/跟踪参数可集中在 configs/business_rules.yaml,
    # 修改后基于 mtime 热重载 (无需重启). 未配置项回落下方各字段默认值.
    business_rules_file: str = "configs/business_rules.yaml"

    # ---- Redis 实时状态 key 前缀 ----
    redis_prefix: str = "sc"

    # ---- 警力分配 ----
    police_demand_weight_current: float = 0.3  # 需求计算: 当前人数权重 α
    police_demand_weight_predict: float = 0.7  # 需求计算: 预测人数权重 β
    police_movement_ratio: float = 0.5  # 每轮最大移动比例 (占总警力)
    police_min_per_region: int = 1  # 每区域最少警力
    police_config_file: str = "configs/police.yaml"  # 警力初始配置 (区域+总警力), 启动时写入 Redis

    # ---- 视频异常检测 ----
    anomaly_check_interval: int = 30  # 每 N 帧检测一次 (≈1s@30fps)
    anomaly_confirm_frames: int = 2  # 连续确认帧数 (去抖, 避免单帧误报)
    anomaly_cooldown_seconds: int = 60  # 同设备同异常告警冷却 (后端去重, 秒)
    anomaly_analysis_width: int = 480  # 分析帧宽度 (等比缩放, 阈值稳定)
    black_screen_brightness: int = 20  # 灰度均值 < 此值 (黑屏条件1)
    black_screen_ratio: float = 0.95  # 近黑像素占比 > 此值 (黑屏条件2)
    black_pixel_value: int = 20  # 近黑像素亮度上限
    flower_block_grid: int = 8  # 花屏分析块网格 (NxN)
    flower_noise_std: float = 35.0  # 块均标准差 > 此值 (花屏: 高噪)
    flower_uniformity: float = 0.6  # 噪声均匀度 1-CV > 此值 (花屏: 均匀)
    flower_channel_corr: float = 0.5  # 通道相关性 < 此值 (花屏: 去相关)
    flower_temporal_diff: float = 25.0  # 时域差分 > 此值 (花屏: 时域高噪, 有前帧时)

    # ---- 安全 ----
    cors_origins: str = "*"  # 允许的跨域来源, 逗号分隔; 生产环境应配置具体前端域名

    # ---- WVP-GB28181 对接 ----
    wvp_enabled: bool = False  # 总开关; False 时跳过自动同步与流地址刷新
    wvp_api_url: str = "http://wvp:18080"  # WVP 管理后台地址 (REST API)
    wvp_username: str = "admin"
    wvp_password: str = "admin"
    wvp_sync_interval: int = 30  # 设备轮询同步间隔 (秒)
    wvp_play_protocol: str = "flv"  # AI 拉流协议: flv | rtsp
    wvp_stream_sub: bool = True  # 拉子码流降低推理压力 (False=主码流)

    # ---- 前端播放地址翻译 (/api/devices/glm-5.3_common/play) ----
    # ZLM 对外地址前缀 (如 http://172.16.168.9:80); 空则 WVP 返回的 flv 地址原样返回
    # (WVP 返回地址的 host 取决于 ZLM stream-ip 配置, 配了容器名时浏览器不可达, 需此处重写)
    zlm_public_base: str = ""
    # MediaMTX 对外地址 (如 http://172.16.168.9:8888); 空则按前端请求的 Host 自动推导 (:8888)
    # 用于把 RTSP 测试流 (rtsp://...:8554/{path}) 翻译成浏览器可播的 HLS 地址
    mediamtx_public_base: str = ""


settings = Settings()
