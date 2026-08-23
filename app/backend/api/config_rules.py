"""业务规则集中配置 API.

读写 configs/business_rules.yaml, 供运维配置页面 (static/business-rules.html) 使用.
- GET: 返回按分组组织的参数 (值 + 元数据: label/desc/type/default).
- PUT: 校验后按行写回 yaml (保留注释与顺序), 文件 mtime 变化触发业务侧热重载.
"""
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from ...common.business_rules import get_business_rules
from ...common.config import settings
from ...common.logger import logger

router = APIRouter(prefix="/api/config", tags=["config"])

# ---- 参数元数据 (与 configs/business_rules.yaml 注释对齐) ----
# default: 页面展示的"出厂默认值", 与 yaml 初始值一致; 修改后可通过"恢复默认"一键还原.
_RULE_META: dict[str, dict[str, Any]] = {
    "counting": {
        "title": "越线计数",
        "desc": "跨线计数算法参数 (AI 服务, counter.py)",
        "params": {
            "min_distance_ratio": {"label": "最小距离比例", "desc": "距线最小距离占帧短边比例, 防抖", "type": "float", "default": 0.02, "step": 0.001, "min": 0, "max": 1},
            "endpoint_sensitivity": {"label": "端点敏感度", "desc": "端点误判过滤敏感度", "type": "float", "default": 0.05, "step": 0.001, "min": 0, "max": 1},
            "hold_frames": {"label": "滞留确认帧数", "desc": "跨线后需在新侧连续保持的帧数 (方向确认)", "type": "int", "default": 3, "min": 1, "max": 10},
            "min_motion": {"label": "最小位移", "desc": "最小位移(像素), 小于此值视为抖动", "type": "int", "default": 2, "min": 0, "max": 50},
            "hysteresis_ratio": {"label": "滞回防抖比例", "desc": "侧别反转需超过此距离(占帧短边比例), 忽略带内抖动", "type": "float", "default": 0.04, "step": 0.001, "min": 0, "max": 1},
            "reverse_crossing_cooldown": {"label": "反向跨线冷却(秒)", "desc": "同一轨迹反向事件的最小间隔(视频时间)", "type": "float", "default": 3.0, "step": 0.5, "min": 0},
            "id_switch_speed_ratio": {"label": "ID切换速度倍数", "desc": "速度超过历史平均 N 倍视为 ID 切换", "type": "float", "default": 3.0, "step": 0.1, "min": 1},
            "id_switch_min_pixel": {"label": "ID切换最小像素差", "desc": "速度差距至少 N 像素才判切换", "type": "float", "default": 30.0, "step": 1, "min": 0},
            "id_switch_min_avg_speed": {"label": "ID切换最低平均速度", "desc": "历史平均速度低于此值不判切换 (防静止误判)", "type": "float", "default": 5.0, "step": 0.5, "min": 0},
            "id_switch_history_window": {"label": "ID切换历史窗口", "desc": "方向一致性验证的历史窗口", "type": "int", "default": 5, "min": 2, "max": 20},
            "counted_tracks_ttl": {"label": "去重保留时长(秒)", "desc": "已计数轨迹去重保留时长, 防内存泄漏", "type": "int", "default": 300, "min": 60, "step": 60},
        },
    },
    "alerts": {
        "title": "告警",
        "desc": "告警引擎参数 (后端, alerts.py)",
        "params": {
            "anomaly_cooldown_seconds": {"label": "异常告警冷却(秒)", "desc": "同设备同类型视频异常告警去重时长", "type": "int", "default": 60, "min": 0, "step": 5},
        },
    },
    "congestion": {
        "title": "拥挤判断",
        "desc": "车流速度与 ROI 上报参数 (后端 + AI)",
        "params": {
            "congestion_min_flow": {"label": "车流速度下限(辆/分钟)", "desc": "每分钟跨线车辆数低于此值视为车流速度过低, 参与拥挤判定", "type": "float", "default": 5.0, "step": 0.5, "min": 0},
            "roi_report_interval": {"label": "ROI 上报周期(秒)", "desc": "AI 上报 ROI 内车辆数的间隔", "type": "float", "default": 2.0, "step": 0.5, "min": 0.5},
        },
    },
    "police": {
        "title": "警力分配",
        "desc": "三阶段分配算法参数 (后端, allocator.py)",
        "params": {
            "demand_weight_current": {"label": "当前人数权重 α", "desc": "需求 = α×当前人数 + β×预测人数, α+β 建议为 1", "type": "float", "default": 0.3, "step": 0.1, "min": 0, "max": 1},
            "demand_weight_predict": {"label": "预测人数权重 β", "desc": "需求 = α×当前人数 + β×预测人数", "type": "float", "default": 0.7, "step": 0.1, "min": 0, "max": 1},
            "movement_ratio": {"label": "每轮最大移动比例", "desc": "单轮最多可调动的警力占总警力比例", "type": "float", "default": 0.5, "step": 0.1, "min": 0, "max": 1},
            "min_per_region": {"label": "每区域最少警力", "desc": "每个区域保障的最少警力数", "type": "int", "default": 1, "min": 0, "max": 100},
        },
    },
    "prediction": {
        "title": "时序预测",
        "desc": "Chronos-2 预测参数 (后端 prediction 模块), 周期同时驱动告警/警力/WS 调度",
        "params": {
            "interval_minutes": {"label": "预测周期(分钟)", "desc": "每 N 分钟预测一次, 同时驱动预测告警/警力分配/WS 推送调度", "type": "int", "default": 15, "min": 5, "max": 60},
            "series_length": {"label": "历史序列长度", "desc": "喂入模型的历史区间个数", "type": "int", "default": 30, "min": 5, "max": 100},
            "vehicle_person_min": {"label": "每车人数下限", "desc": "车流转人流的每车人数随机采样下界", "type": "int", "default": 2, "min": 1, "max": 100},
            "vehicle_person_max": {"label": "每车人数上限", "desc": "车流转人流的每车人数随机采样上界", "type": "int", "default": 5, "min": 1, "max": 100},
        },
    },
    "anomaly": {
        "title": "视频异常",
        "desc": "黑屏/花屏检测参数 (AI 服务, anomaly.py)",
        "params": {
            "analysis_width": {"label": "分析宽度", "desc": "分析帧降采样宽度, 阈值稳定", "type": "int", "default": 480, "min": 100, "max": 1920},
            "black_screen_brightness": {"label": "黑屏亮度阈值", "desc": "灰度均值低于此值 (黑屏条件1)", "type": "int", "default": 20, "min": 0, "max": 255},
            "black_screen_ratio": {"label": "近黑像素占比阈值", "desc": "近黑像素占比高于此值 (黑屏条件2, 双条件 AND)", "type": "float", "default": 0.95, "step": 0.01, "min": 0, "max": 1},
            "black_pixel_value": {"label": "近黑像素上限", "desc": "像素值低于此值算近黑", "type": "int", "default": 20, "min": 0, "max": 255},
            "flower_block_grid": {"label": "花屏分块网格", "desc": "NxN 分块分析空间噪声", "type": "int", "default": 8, "min": 2, "max": 32},
            "flower_noise_std": {"label": "花屏噪声阈值", "desc": "块均标准差高于此值 (高噪)", "type": "float", "default": 35.0, "step": 0.5, "min": 0},
            "flower_uniformity": {"label": "花屏均匀度阈值", "desc": "噪声均匀度 1-CV 高于此值", "type": "float", "default": 0.6, "step": 0.01, "min": 0, "max": 1},
            "flower_channel_corr": {"label": "花屏通道相关阈值", "desc": "通道相关性低于此值 (去相关)", "type": "float", "default": 0.5, "step": 0.01, "min": 0, "max": 1},
            "flower_temporal_diff": {"label": "花屏时域差分阈值", "desc": "时域差分高于此值 (有前帧时)", "type": "float", "default": 25.0, "step": 0.5, "min": 0},
            "check_interval": {"label": "检测间隔(帧)", "desc": "每 N 帧检测一次异常", "type": "int", "default": 30, "min": 1, "max": 300},
            "confirm_frames": {"label": "确认帧数", "desc": "连续 N 帧确认异常才上报 (去抖)", "type": "int", "default": 2, "min": 1, "max": 10},
        },
    },
    "tracking": {
        "title": "目标跟踪",
        "desc": "YOLO 检测与跟踪参数 (AI 服务, tracker.py), 算法本身由 bytetrack.yaml 决定",
        "params": {
            "yolo_conf": {"label": "YOLO 置信度", "desc": "检测置信度阈值", "type": "float", "default": 0.4, "step": 0.05, "min": 0.05, "max": 1},
            "yolo_iou": {"label": "NMS IoU", "desc": "非极大值抑制 IoU 阈值", "type": "float", "default": 0.5, "step": 0.05, "min": 0.05, "max": 1},
            "track_buffer": {"label": "跟踪历史长度", "desc": "跟踪轨迹保留帧数 (遮挡频繁场景可增大)", "type": "int", "default": 30, "min": 5, "max": 200},
        },
    },
}


def _coerce_value(key: str, meta: dict, raw: Any) -> Any:
    """按元数据类型转换并校验."""
    t = meta["type"]
    try:
        if t == "int":
            v = int(raw)
        elif t == "float":
            v = float(raw)
        elif t == "bool":
            v = bool(raw) if not isinstance(raw, str) else raw.strip().lower() in ("true", "1", "yes", "on")
        else:
            v = str(raw)
    except (TypeError, ValueError):
        raise HTTPException(400, f"参数 {key} 的值 {raw!r} 无法转换为 {t}")
    if t in ("int", "float") and "min" in meta and v < meta["min"]:
        raise HTTPException(400, f"参数 {key} 不能小于 {meta['min']}")
    if t in ("int", "float") and "max" in meta and v > meta["max"]:
        raise HTTPException(400, f"参数 {key} 不能大于 {meta['max']}")
    return v


def _format_value(v: Any, t: str) -> str:
    if t == "bool":
        return "true" if v else "false"
    return str(v)


def _update_file(path: Path, updates: dict[str, dict[str, Any]]) -> None:
    """按行写回 yaml, 保留注释与顺序; 缺失参数追加到所属分组末尾."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    cur: str | None = None
    remaining = {s: dict(keys) for s, keys in updates.items()}

    # 第一遍: 定位分组并替换已有参数行
    for i, line in enumerate(lines):
        body = line.rstrip("\n")
        stripped = body.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line[0].isspace() and ":" in stripped and not stripped.startswith("-"):
            # 分组头 (无缩进): counting: / prediction:
            name = stripped.split(":")[0].strip()
            cur = name if name in updates else None
            continue
        if cur in remaining and stripped:
            m = re.match(r"^(\s*)([\w-]+):\s*(\S+)(\s*#.*)?$", body)
            if m and m.group(2) in remaining[cur]:
                indent, key, _val, tail = m.groups()
                t = _RULE_META[cur]["params"][key]["type"]
                lines[i] = f"{indent}{key}: {_format_value(remaining[cur].pop(key), t)}{tail or ''}\n"

    # 第二遍: 追加 yaml 中不存在的参数
    for sec, keys in remaining.items():
        if not keys:
            continue
        sec_idx = None
        for i, line in enumerate(lines):
            s = line.strip()
            if s and not line[0].isspace() and ":" in s and not s.startswith("-") and s.split(":")[0].strip() == sec:
                sec_idx = i
                break
        if sec_idx is None:
            continue
        insert_at = len(lines)
        for j in range(sec_idx + 1, len(lines)):
            s = lines[j].strip()
            if s and not lines[j][0].isspace() and ":" in s and not s.startswith("-"):
                insert_at = j
                break
        new_lines = []
        for key, v in keys.items():
            meta = _RULE_META[sec]["params"][key]
            new_lines.append(f"  {key}: {_format_value(v, meta['type'])}  # {meta['label']}\n")
        lines[insert_at:insert_at] = new_lines

    path.write_text("".join(lines), encoding="utf-8")


def _rules_path() -> Path:
    return Path(settings.business_rules_file)


def _build_groups(values: dict[str, dict] | None = None) -> list[dict]:
    """组装前端渲染数据: 文件值优先, 未配置回落元数据默认值."""
    values = values or {}
    groups = []
    for sec, meta in _RULE_META.items():
        params = []
        for key, p in meta["params"].items():
            file_val = values.get(sec, {}).get(key)
            params.append({
                "key": key,
                "label": p["label"],
                "desc": p["desc"],
                "type": p["type"],
                "step": p.get("step"),
                "min": p.get("min"),
                "max": p.get("max"),
                "default": p["default"],
                "value": file_val if file_val is not None else p["default"],
            })
        groups.append({
            "key": sec,
            "title": meta["title"],
            "desc": meta["desc"],
            "params": params,
        })
    return groups


@router.get("/business-rules")
async def get_business_rules_api():
    """读取业务规则配置 (含元数据), 供配置页面渲染."""
    return {
        "file": settings.business_rules_file,
        "hot_reload": True,
        "groups": _build_groups(get_business_rules()),
    }


@router.put("/business-rules")
async def update_business_rules_api(body: dict):
    """更新业务规则: 校验后写回 yaml, mtime 变化触发热重载, 无需重启."""
    updates: dict[str, dict[str, Any]] = {}
    for sec, meta in _RULE_META.items():
        incoming = body.get(sec)
        if not isinstance(incoming, dict):
            continue
        valid: dict[str, Any] = {}
        for key, pm in meta["params"].items():
            if key in incoming:
                valid[key] = _coerce_value(key, pm, incoming[key])
        if valid:
            updates[sec] = valid
    if not updates:
        raise HTTPException(400, "请求体未包含任何可更新参数")

    path = _rules_path()
    if not path.exists():
        raise HTTPException(500, f"业务规则文件不存在: {path}")
    _update_file(path, updates)
    logger.info(f"[业务规则] 已更新 {len(updates)} 个分组, 触发热重载: {path.name}")

    return {
        "status": "ok",
        "message": "已保存, 热重载已生效",
        "hot_reload": True,
        "file": settings.business_rules_file,
        "groups": _build_groups(get_business_rules()),
    }
