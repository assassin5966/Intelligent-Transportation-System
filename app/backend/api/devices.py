"""设备管理 API (注册/列表/删除, 含越线计数线配置).

注册仅保存配置到 Redis (不启流); 启流/删除时转发到 AI 分析服务启停视频处理管道.
AI 服务不可达时仅告警, 不阻塞配置落库.
"""
import asyncio
import json
from pathlib import Path
from typing import Literal, Optional

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel

from ...common import device_info
from ...common.config import settings
from ...common.counting_config import effective_line_intervals, parse_counting_config
from ...common.logger import logger
from ...common.redis_client import get_redis
from ..core.stream_urls import to_browser, to_internal

router = APIRouter(prefix="/api/devices", tags=["devices"])

_DEVICE_KEY_PREFIX = f"{settings.redis_prefix}:device:"

# 模块级共享 httpx 连接池 (设备 API 调用频率低, 复用避免反复建连)
_shared_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None:
        _shared_client = httpx.AsyncClient(timeout=10.0)
    return _shared_client


async def _resolve_device_key(redis, device_id: str) -> str:
    """按名称解析设备 Redis 键 (名称即身份, 兼容长名/短名别名).

    device_info 白名单 name 常带通道号尾缀 (如 '...路西以南106米(枪-南)031208'),
    而 WVP 同步注册进 Redis 的键是 WVP 通道名 (同串去尾缀). 同步侧
    device_info.is_registered 有 _resolve_key 短名前缀兜底, 这里对齐做反向兼容:
    精确未命中时按归一化前缀匹配现存设备键 (取最长匹配), 使 /play /snapshot
    /enable 等接口对 device_info 长名与 WVP 短名都能命中.
    最长匹配有多个同名别名时不猜测，返回原键 (由调用方抛 404).
    """
    key = _DEVICE_KEY_PREFIX + device_id
    if await redis.exists(key):
        return key
    norm = device_info.normalize(device_id)
    if not norm:
        return key
    best_ids, best_len = [], 0
    async for k in redis.scan_iter(match=_DEVICE_KEY_PREFIX + "*", count=100):
        rid = k[len(_DEVICE_KEY_PREFIX):]
        rnorm = device_info.normalize(rid)
        if not rnorm or not norm.startswith(rnorm) or len(rnorm) < best_len:
            continue
        if len(rnorm) > best_len:
            best_ids, best_len = [rid], len(rnorm)
        else:
            best_ids.append(rid)
    if len(best_ids) == 1:
        return _DEVICE_KEY_PREFIX + best_ids[0]
    if best_ids:
        logger.warning(f"设备别名 {device_id!r} 匹配多个设备，不自动选择")
    return key


class DeviceIn(BaseModel):
    id: Optional[str] = None  # 设备唯一标识; 缺省取 name (名称即身份, 与 WVP 同步一致)
    name: str
    stream_url: str
    line_coords: Optional[str] = None  # "x1,y1,x2,y2" 归一化 0-1
    anchor_coords: Optional[str] = None  # "x,y" 归一化 0-1, 内侧锚点
    count_only: Optional[Literal["enter", "exit"]] = None  # None=双向, "enter"=只计Enter, "exit"=只计Exit
    camera_type: Optional[Literal["vehicle", "person"]] = None  # None=全部检测, vehicle/person
    roi_coords: Optional[str] = None  # "x1,y1,x2,y2,..." 归一化 0-1, >=3 顶点
    max_vehicles: Optional[int] = None  # 拥挤判断: ROI 内最大车辆数阈值 (>0 开启车辆拥挤判断)
    max_persons: Optional[int] = None  # 拥挤判断: ROI 内最大人数阈值 (>0 开启人流拥挤判断)
    gb_device_id: Optional[str] = None  # 国标设备ID (WVP 同步设备填写, 手动注册留空)
    gb_channel_id: Optional[str] = None  # 国标通道ID (WVP 同步设备填写, 手动注册留空)


class DeviceOut(BaseModel):
    id: str
    name: str
    stream_url: str
    line_coords: Optional[str] = None
    anchor_coords: Optional[str] = None
    count_only: Optional[str] = None
    camera_type: Optional[str] = None
    roi_coords: Optional[str] = None
    max_vehicles: Optional[int] = None
    max_persons: Optional[int] = None
    gb_device_id: Optional[str] = None
    gb_channel_id: Optional[str] = None
    status: str = "registered"
    ai_status: Optional[str] = None  # 与设备/通道在线状态分开
    wvp_sync_status: Optional[str] = None  # 查询是否完整，不与 AI 心跳状态混用
    last_heartbeat: Optional[str] = None
    longitude: Optional[float] = None  # 设备经纬度 (来自 data/device_geo.json, 按名称匹配)
    latitude: Optional[float] = None
    category: Optional[str] = None  # 点位分类 (来自 data/device_category.json, 按名称匹配)
    region: Optional[str] = None  # 所属景区 (来自 device_info.region, 前端全局景区筛选依据)
    intersection_id: Optional[int] = None  # 所属路口 (来自 device_info.intersection_id, 路口←设备关联)
    show_on_map: Optional[bool] = True  # 是否在大屏显示点位 (device_info.show_on_map; 未登记设备不受控)


# 设备经纬度映射缓存: 设备名称 -> {"longitude", "latitude"} (来自 data/device_geo.json)
_geo_cache: Optional[dict] = None
_GEO_FILE = Path(__file__).resolve().parents[3] / "data" / "device_geo.json"


def _load_geo() -> dict:
    """加载设备经纬度映射 (带模块级缓存, 首次读取后复用)."""
    global _geo_cache
    if _geo_cache is None:
        try:
            if _GEO_FILE.exists():
                _geo_cache = json.loads(_GEO_FILE.read_text(encoding="utf-8")).get("devices", {})
            else:
                _geo_cache = {}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"读取 device_geo.json 失败: {e}")
            _geo_cache = {}
    return _geo_cache


def geo_by_name(name: str) -> tuple[Optional[float], Optional[float]]:
    """按设备名称查经纬度, 未匹配到返回 (None, None).

    优先读 MySQL 设备信息表 (已迁入, 运维页面可增删改查); 未启用/未加载时
    回落旧 device_geo.json (兼容本地开发, 只读不写).

    匹配时忽略空格: device_geo.json 的设备名为 'GAJK-2648和阳南门出口路南以东85米'
    (无空格), 而注册名可能带空格, 与 allocator 的去空格匹配保持一致.

    供设备 API 与统计/WebSocket 推送共用 (前端地图打点).
    """
    row = device_info.get(name)
    if row is not None:
        return row["longitude"], row["latitude"]
    geo = _load_geo().get((name or "").replace(" ", ""))
    if not geo:
        return None, None
    return geo.get("longitude"), geo.get("latitude")


# 设备点位分类映射缓存: 设备名称 -> 点位分类 (来自 data/device_category.json,
# 由 data/设备信息汇总表.xlsx 提取生成)
_category_cache: Optional[dict] = None
_CATEGORY_FILE = Path(__file__).resolve().parents[3] / "data" / "device_category.json"


def _load_categories() -> dict:
    """加载设备点位分类映射 (带模块级缓存, 首次读取后复用).

    值兼容两种形态 (归一化为 {名称: 分类字符串}):
      - 旧版字符串: "设备名": "分类"
      - 新版对象:   "设备名": {"category": "分类", "point_id": "GAJK-2648"}
    """
    global _category_cache
    if _category_cache is None:
        raw: dict = {}
        try:
            if _CATEGORY_FILE.exists():
                raw = json.loads(_CATEGORY_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"读取 device_category.json 失败: {e}")
        _category_cache = {}
        for name, val in raw.items():
            if name.startswith("_"):
                continue  # 元信息键 (如 _meta)
            _category_cache[name] = val.get("category") if isinstance(val, dict) else val
    return _category_cache


def category_by_name(name: str) -> Optional[str]:
    """按设备名称查点位分类, 未匹配到或分类为空返回 None.

    优先读 MySQL 设备信息表 (已迁入, 运维页面可增删改查); 未启用/未加载时
    回落旧 device_category.json (兼容本地开发, 只读不写).

    匹配时忽略空格 (同 geo_by_name).

    供设备 API 与统计/WebSocket 推送共用 (前端设备卡片分类展示).
    """
    row = device_info.get(name)
    if row is not None:
        return row["category"]
    cat = _load_categories().get((name or "").replace(" ", ""))
    return cat or None


def _line_from_coords(line_coords: Optional[str]) -> list[list[float]]:
    """兼容旧调用名；无效配置抛错，绝不默默替换默认线。"""
    return parse_counting_config(line_coords, None, None)[0]


def _anchor_from_coords(anchor_coords: Optional[str]) -> Optional[list[float]]:
    """兼容旧调用名；空值表示使用默认锚点，格式无效则抛错。"""
    return parse_counting_config(None, anchor_coords, None, require_line=False)[1]


def _roi_from_coords(roi_coords: Optional[str]) -> Optional[list[list[float]]]:
    """兼容旧调用名；空值关闭 ROI，格式无效则抛错。"""
    return parse_counting_config(None, None, roi_coords, require_line=False)[2]


async def _forward_to_ai(method: str, path: str, json_body: Optional[dict] = None) -> bool:
    """转发到 AI 分析服务; 返回是否成功 (失败仅告警, 不阻塞设备配置落库)."""
    url = f"{settings.ai_service_url}{path}"
    try:
        client = _get_client()
        if method == "POST":
            resp = await client.post(url, json=json_body)
        else:
            resp = await client.request(method, url)
        if resp.status_code >= 400:
            logger.warning(f"AI 服务转发失败 {method} {url}: HTTP {resp.status_code}")
            return False
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"AI 服务不可达 {method} {url}: {e}")
        return False


def _capture_frame_sync(url: str, max_frames: int = 30) -> Optional[bytes]:
    """同步拉流截一帧, 返回 JPEG bytes. 失败返回 None.

    优先用 ffmpeg 截帧 (低延迟参数, 对低帧率流如 1fps mock 稳定快速),
    失败再回退 cv2 (兼容无 ffmpeg 环境).
    """
    try:
        import subprocess

        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-rtsp_transport", "tcp",
                "-fflags", "nobuffer", "-flags", "low_delay",
                "-probesize", "32", "-analyzeduration", "0",
                "-i", url,
                "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg",
                "-q:v", "3", "-",
            ],
            capture_output=True,
            timeout=20,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout
    except Exception:  # noqa: BLE001
        pass

    import cv2  # 后端与 AI 共用镜像含 opencv; 惰性导入避免启动依赖

    cap = cv2.VideoCapture(url)
    if not cap.isOpened():
        return None
    try:
        for _ in range(max_frames):
            ret, frame = cap.read()
            if ret and frame is not None and frame.size > 0:
                ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                if ok:
                    return buf.tobytes()
        return None
    finally:
        cap.release()


@router.get("")
async def list_(scenic: str = Query(None, description="可选: 景区名; 只返回该景区下的设备")):
    redis = get_redis()
    found = []
    async for key in redis.scan_iter(f"{_DEVICE_KEY_PREFIX}*"):
        data = await redis.hgetall(key)
        if data:
            # Redis hash 空值一律是 "", Pydantic v2 对 Optional[int] 无法解析空字符串,
            # 统一清洗为 None (兼容历史设备无 max_vehicles/max_persons 字段).
            clean = dict(data)
            if clean.get("max_vehicles") == "":
                clean["max_vehicles"] = None
            if clean.get("max_persons") == "":
                clean["max_persons"] = None
            # 按设备名称补全经纬度 (来自 data/device_geo.json)
            lng, lat = geo_by_name(clean.get("name", ""))
            clean["longitude"] = lng
            clean["latitude"] = lat
            # 按设备名称补全点位分类 (来自 data/device_category.json)
            clean["category"] = category_by_name(clean.get("name", ""))
            # 按设备名称补全所属景区 (来自 MySQL device_info.region; 未登记为空;
            # 在线设备用门/口简称时按门级关键字兜底匹配, 见 device_info.region_for)
            clean["region"] = device_info.region_for(clean.get("name", ""))
            # 按设备名称补全所属路口 id (来自 MySQL device_info.intersection_id; 未关联为空)
            clean["intersection_id"] = device_info.intersection_id_for(clean.get("name", ""))
            # 按设备名称补全「是否大屏显示」开关 (来自 MySQL device_info.show_on_map)
            clean["show_on_map"] = device_info.show_on_map_for(clean.get("name", ""))
            found.append(DeviceOut(**clean))
    if scenic and scenic.strip():
        target = scenic.strip()
        found = [d for d in found if (d.region or "") == target]
    return found


@router.post("", status_code=201)
async def register(dev: DeviceIn):
    """注册设备 (仅保存配置到 Redis, 不启流).

    启流统一由 POST /api/devices/{device_id}/enable 负责: 手动注册设备注册后
    status=registered, 配置计数线并 enable 后才启动 AI 管道.
    """
    try:
        parse_counting_config(dev.line_coords, dev.anchor_coords, dev.roi_coords, require_line=False)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    redis = get_redis()
    # 名称即身份: id 缺省取 name; '/' 会被 FastAPI 路径参数截断, 注册时直接拒绝
    dev_id = (dev.id or "").strip() or dev.name.strip()
    if not dev_id:
        raise HTTPException(400, "id 与 name 不能同时为空")
    if "/" in dev_id:
        raise HTTPException(400, f"设备标识含非法字符 '/': {dev_id}")
    key = f"{_DEVICE_KEY_PREFIX}{dev_id}"
    await redis.hset(
        key,
        mapping={
            "id": dev_id,
            "name": dev.name,
            "stream_url": dev.stream_url,
            "line_coords": dev.line_coords or "",
            "anchor_coords": dev.anchor_coords or "",
            "count_only": dev.count_only or "",
            "camera_type": dev.camera_type or "",
            "roi_coords": dev.roi_coords or "",
            "max_vehicles": str(dev.max_vehicles) if dev.max_vehicles is not None else "",
            "max_persons": str(dev.max_persons) if dev.max_persons is not None else "",
            "gb_device_id": dev.gb_device_id or "",
            "gb_channel_id": dev.gb_channel_id or "",
            "status": "registered",
        },
    )
    return {"id": dev_id, "status": "registered"}


@router.delete("/{device_id}")
async def remove(device_id: str):
    redis = get_redis()
    key = await _resolve_device_key(redis, device_id)
    exists = await redis.exists(key)
    if not exists:
        raise HTTPException(404, "device not found")
    real_id = key[len(_DEVICE_KEY_PREFIX):]
    await redis.delete(key)
    await _forward_to_ai("DELETE", f"/devices/{real_id}")
    return {"status": "deleted", "id": device_id}


class DeviceHeartbeat(BaseModel):
    ai_status: Optional[Literal["stopped", "waiting_first_frame", "no_frames", "no_recent_frame",
                                "no_recent_inference", "low_fps", "processing", "event_persist_blocked"]] = None


@router.post("/{device_id}/heartbeat")
async def heartbeat(device_id: str, body: Optional[DeviceHeartbeat] = None):
    """AI 服务心跳: 更新设备最后心跳时间, 标记设备在线."""
    from datetime import datetime, timezone
    redis = get_redis()
    key = await _resolve_device_key(redis, device_id)
    exists = await redis.exists(key)
    if not exists:
        raise HTTPException(404, "device not found")
    now_iso = datetime.now(timezone.utc).isoformat()
    fields = {
        "last_heartbeat": now_iso,
        "status": "online",
    }
    if body and body.ai_status:
        fields["ai_status"] = body.ai_status
    await redis.hset(key, mapping=fields)
    return {"device_id": device_id, "heartbeat": now_iso}


# ---- WVP-GB28181 对接 (设备同步 / 流地址刷新 / 启用) ----


@router.post("/sync", status_code=200)
async def sync_wvp():
    """手动触发一次 WVP 设备同步 (与后台 wvp_sync 同一逻辑)."""
    if not settings.wvp_enabled:
        raise HTTPException(503, "WVP 同步未启用 (wvp_enabled=false)")
    from ..core.wvp_sync import sync_once

    return await sync_once()


@router.get("/{device_id}/snapshot")
async def snapshot(device_id: str):
    """截取一帧画面供前端绘制计数线/锚点.

    支持两种模式:
      1. WVP 同步设备: play/start 拿 FLV → cv2 拉一帧 → 返回 JPEG.
      2. 手动注册设备: 直接用 stream_url 拉流截帧.
    截完自动释放资源.
    """
    redis = get_redis()
    key = await _resolve_device_key(redis, device_id)
    data = await redis.hgetall(key)
    if not data:
        raise HTTPException(404, "device not found")

    gb_dev = data.get("gb_device_id", "")
    gb_ch = data.get("gb_channel_id", "")
    is_wvp = bool(settings.wvp_enabled and gb_dev and gb_ch)

    if is_wvp:
        # 模式 1: WVP 同步设备
        from ..core.wvp_client import get_wvp_client
        wvp = get_wvp_client()
        play = await wvp.start_play(gb_dev, gb_ch)
        try:
            stream_url = to_internal(wvp.select_stream_url(play))
            if not stream_url:
                raise HTTPException(502, "WVP 点播失败, 无法获取流地址")
            jpeg = await asyncio.wait_for(
                asyncio.to_thread(_capture_frame_sync, stream_url),
                timeout=25.0,
            )
        except asyncio.TimeoutError:
            raise HTTPException(504, "截帧超时 (流可能未就绪, 请重试)")
        finally:
            # 设备已启流 (online/running) 时 AI 正在消费同一路 WVP 点播会话,
            # 此处停播会连带打断 AI 拉流; 仅在该流无其他消费者时清理点播会话.
            if data.get("status") not in ("online", "running"):
                await wvp.stop_play(gb_dev, gb_ch)
    else:
        # 模式 2: 手动注册设备, 直接拉流截帧
        stream_url = data.get("stream_url", "")
        if not stream_url:
            raise HTTPException(400, "设备未配置 stream_url, 无法截帧")
        jpeg = await asyncio.wait_for(
            asyncio.to_thread(_capture_frame_sync, stream_url),
            timeout=25.0,
        )

    if jpeg is None:
        raise HTTPException(502, "截帧失败 (无法读取视频流, 检查设备是否在线)")

    return Response(content=jpeg, media_type="image/jpeg")


@router.get("/{device_id}/stream")
async def refresh_stream(device_id: str):
    """刷新并返回设备流地址. AI 服务断流重连时调用此端点拿新地址."""
    if not settings.wvp_enabled:
        raise HTTPException(503, "WVP 未启用 (wvp_enabled=false)")
    redis = get_redis()
    data = await redis.hgetall(await _resolve_device_key(redis, device_id))
    if not data:
        raise HTTPException(404, "device not found")
    gb_dev = data.get("gb_device_id", "")
    gb_ch = data.get("gb_channel_id", "")
    if not gb_dev or not gb_ch:
        raise HTTPException(400, "非 WVP 同步设备, 无可刷新流地址")
    from ..core.wvp_client import get_wvp_client

    wvp = get_wvp_client()
    play = await wvp.start_play(gb_dev, gb_ch)
    stream_url = to_internal(wvp.select_stream_url(play))
    if not stream_url:
        raise HTTPException(502, f"WVP 点播失败, 无法获取流地址 (gb_dev={gb_dev}, gb_ch={gb_ch})")
    lng, lat = geo_by_name(data.get("name", ""))
    return {
        "device_id": device_id,
        "stream_url": stream_url,
        "stream_id": play.get("stream_id") if play else None,
        "longitude": lng,
        "latitude": lat,
    }


@router.get("/{device_id}/play")
async def play(device_id: str, request: Request):
    """返回浏览器可直接播放的流地址 (前端大屏/预览用; AI 拉流请走 /stream).

    三分支地址翻译:
      1. WVP 同步设备: play/start 取 FLV; 配置 zlm_public_base 时重写 scheme://host:port
      2. 手动 RTSP 设备 (约定推流源为 MediaMTX): rtsp://host:8554/{path}
         -> http://{宿主}:8888/{path}/index.m3u8 (HLS, 浏览器 hls.js 可播)
      3. stream_url 已是 http(s)://: 原样返回 (如 ZLM FLV 直配)
    """
    redis = get_redis()
    data = await redis.hgetall(await _resolve_device_key(redis, device_id))
    if not data:
        raise HTTPException(404, "device not found")

    gb_dev = data.get("gb_device_id", "")
    gb_ch = data.get("gb_channel_id", "")
    stream_url = data.get("stream_url", "")

    # 分支 1: WVP 同步设备 -> FLV
    if settings.wvp_enabled and gb_dev and gb_ch:
        from ..core.wvp_client import get_wvp_client

        wvp = get_wvp_client()
        play_result = await wvp.start_play(gb_dev, gb_ch)
        flv = (play_result or {}).get("flv")
        if not flv:
            raise HTTPException(502, f"WVP 点播失败, 无法获取播放地址 (gb_dev={gb_dev}, gb_ch={gb_ch})")
        flv = to_browser(flv)
        return {
            "device_id": device_id,
            "play_url": flv,
            "protocol": "flv",
            "source": "wvp",
            "stream_id": (play_result or {}).get("stream_id"),
        }

    # 分支 2: RTSP 设备 -> 约定推流源为 MediaMTX, 翻译为 HLS
    if stream_url.startswith("rtsp://"):
        from urllib.parse import urlsplit

        path = urlsplit(stream_url).path.strip("/")
        if not path:
            raise HTTPException(400, f"RTSP 流地址缺少路径, 无法翻译为 HLS: {stream_url!r}")
        base = settings.mediamtx_public_base.rstrip("/")
        if not base:
            # 从前端请求的 Host 推导宿主 IP (前端能访问后端 8000 即可访问同机 MediaMTX 8888)
            host = request.url.hostname or "127.0.0.1"
            base = f"http://{host}:8888"
        return {
            "device_id": device_id,
            "play_url": f"{base}/{path}/index.m3u8",
            "protocol": "hls",
            "source": "mediamtx",
            "source_stream_url": stream_url,
        }

    # 分支 3: http(s) 流地址原样返回
    if stream_url.startswith(("http://", "https://")):
        return {
            "device_id": device_id,
            "play_url": stream_url,
            "protocol": "flv",
            "source": "direct",
        }

    raise HTTPException(400, f"设备无可播放的流地址 (stream_url={stream_url!r})")


class DeviceEnableIn(BaseModel):
    line_coords: str  # "x1,y1,x2,y2" 归一化 0-1 (必填, 启流必需)
    anchor_coords: Optional[str] = None
    count_only: Optional[Literal["enter", "exit"]] = None
    camera_type: Optional[Literal["vehicle", "person"]] = None
    roi_coords: Optional[str] = None
    allow_multi_roi: bool = False  # 客户端已预览并确认凹 ROI 的多段有效计数线
    max_vehicles: Optional[int] = None  # 拥挤判断: ROI 内最大车辆数阈值 (设置后车辆拥挤判断生效)
    max_persons: Optional[int] = None  # 拥挤判断: ROI 内最大人数阈值 (设置后人流拥挤判断生效)


@router.post("/{device_id}/enable", status_code=200)
async def enable_device(device_id: str, body: DeviceEnableIn):
    """配置计数线并启流 (支持 WVP 同步设备和手动注册设备).

    两种模式:
      1. WVP 同步设备: 调 WVP 拿流地址 → 启流 (status: synced -> online).
      2. 手动注册设备: 更新配置 → 重启 AI 管道 (status: registered -> running).
    """
    try:
        line, _anchor, roi = parse_counting_config(body.line_coords, body.anchor_coords, body.roi_coords)
        if roi is not None and len(effective_line_intervals(line, roi)) > 1 and not body.allow_multi_roi:
            raise ValueError('ROI 包含多段有效计数线，请使用支持多段预览的配置页面')
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    redis = get_redis()
    key = await _resolve_device_key(redis, device_id)
    data = await redis.hgetall(key)
    if not data:
        raise HTTPException(404, "device not found")

    gb_dev = data.get("gb_device_id", "")
    gb_ch = data.get("gb_channel_id", "")
    is_wvp = bool(settings.wvp_enabled and gb_dev and gb_ch)
    real_id = key[len(_DEVICE_KEY_PREFIX):]

    # 落库计数线配置
    await redis.hset(
        key,
        mapping={
            "line_coords": body.line_coords,
            "anchor_coords": body.anchor_coords or "",
            "count_only": body.count_only or "",
            "camera_type": body.camera_type or "",
            "roi_coords": body.roi_coords or "",
            "max_vehicles": str(body.max_vehicles) if body.max_vehicles is not None else "",
            "max_persons": str(body.max_persons) if body.max_persons is not None else "",
        },
    )
    updated = await redis.hgetall(key)

    if is_wvp:
        # 模式 1: WVP 同步设备
        from ..core.wvp_client import get_wvp_client
        from ..core.wvp_sync import _start_ai_pipeline

        wvp = get_wvp_client()
        play = await wvp.start_play(gb_dev, gb_ch)
        stream_url = to_internal(wvp.select_stream_url(play))
        if not stream_url:
            raise HTTPException(502, f"WVP 点播失败, 无法获取流地址 (gb_dev={gb_dev}, gb_ch={gb_ch})")

        # 先停旧管道: 重复点「启流计数」时类型(只检车/只检人)/方向/计数线/ROI 可能已改,
        # 旧管道不重启会继续用旧配置 (POST /devices 只会返回 409 device already running)
        await _forward_to_ai("DELETE", f"/devices/{real_id}")
        if not await _start_ai_pipeline(updated, stream_url):
            await redis.hset(key, "ai_status", "start_failed")
            logger.error(f"[WVP] 设备 {device_id} 启流失败 (推理容量已满或 AI 服务异常)")
            raise HTTPException(
                503, "AI 启流失败: 推理容量已满或 AI 服务异常, 请查看 AI 服务日志"
            )
        await redis.hset(key, mapping={"status": "online", "ai_status": "waiting_first_frame"})
        logger.info(f"[WVP] 设备 {device_id} 已启用并启流")
        return {"device_id": device_id, "status": "online", "ai_status": "waiting_first_frame", "stream_url": stream_url}
    else:
        # 模式 2: 手动注册设备 - 重启 AI 管道 (先停旧管道, 再起新管道)
        from ..core.wvp_sync import _start_ai_pipeline

        # 先停止旧管道 (如果存在)
        await _forward_to_ai("DELETE", f"/devices/{real_id}")

        # 再启动新管道
        if not await _start_ai_pipeline(updated, updated.get("stream_url", "")):
            await redis.hset(key, "ai_status", "start_failed")
            logger.error(f"[手动] 设备 {device_id} 启流失败 (推理容量已满或 AI 服务异常)")
            raise HTTPException(
                503, "AI 启流失败: 推理容量已满或 AI 服务异常, 请查看 AI 服务日志"
            )
        await redis.hset(key, mapping={"status": "online", "ai_status": "waiting_first_frame"})
        logger.info(f"[手动] 设备 {device_id} 已重新配置并启流")
        return {"device_id": device_id, "status": "online", "ai_status": "waiting_first_frame", "stream_url": updated.get("stream_url", "")}
