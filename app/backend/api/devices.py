"""设备管理 API (注册/列表/删除, 含越线计数线配置).

注册仅保存配置到 Redis (不启流); 启流/删除时转发到 AI 分析服务启停视频处理管道.
AI 服务不可达时仅告警, 不阻塞配置落库.
"""
import asyncio
import json
from pathlib import Path
from typing import Literal, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from ...common.config import settings
from ...common.logger import logger
from ...common.redis_client import get_redis

router = APIRouter(prefix="/api/devices", tags=["devices"])

_DEVICE_KEY_PREFIX = f"{settings.redis_prefix}:device:"

# 模块级共享 httpx 连接池 (设备 API 调用频率低, 复用避免反复建连)
_shared_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None:
        _shared_client = httpx.AsyncClient(timeout=10.0)
    return _shared_client


def _warn_if_out_of_range(coords: list[list[float]], name: str) -> None:
    """归一化坐标应在 [0,1]; 超出范围告警 (计数线会画到画面外)."""
    for pt in coords:
        for v in pt:
            if v < 0 or v > 1:
                logger.warning(f"{name} 存在超出 [0,1] 的值 {v}, 计数线可能偏离画面")
                return


class DeviceIn(BaseModel):
    id: str
    name: str
    stream_url: str
    line_coords: Optional[str] = None  # "x1,y1,x2,y2" 归一化 0-1
    anchor_coords: Optional[str] = None  # "x,y" 归一化 0-1, 内侧锚点
    count_only: Optional[Literal["enter", "exit"]] = None  # None=双向, "enter"=只计Enter, "exit"=只计Exit
    camera_type: Optional[Literal["vehicle", "person"]] = None  # None=全部检测, vehicle/person
    roi_coords: Optional[str] = None  # "x1,y1,x2,y2,..." 归一化 0-1, >=3 顶点
    max_vehicles: Optional[int] = None  # 拥挤判断: ROI 内最大车辆数阈值 (>0 开启拥挤判断)
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
    gb_device_id: Optional[str] = None
    gb_channel_id: Optional[str] = None
    status: str = "registered"
    last_heartbeat: Optional[str] = None
    longitude: Optional[float] = None  # 设备经纬度 (来自 data/device_geo.json, 按名称匹配)
    latitude: Optional[float] = None


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


def _geo_by_name(name: str) -> tuple[Optional[float], Optional[float]]:
    """按设备名称查经纬度, 未匹配到返回 (None, None)."""
    geo = _load_geo().get(name or "")
    if not geo:
        return None, None
    return geo.get("longitude"), geo.get("latitude")


def _line_from_coords(line_coords: Optional[str]) -> list[list[float]]:
    """解析 line_coords -> [[x1,y1],[x2,y2]]; 无法解析时返回默认线并告警."""
    if line_coords:
        try:
            parts = [float(x) for x in line_coords.split(",")]
            if len(parts) == 4:
                coords = [[parts[0], parts[1]], [parts[2], parts[3]]]
                _warn_if_out_of_range(coords, "line_coords")
                return coords
        except ValueError:
            logger.warning(f"line_coords 格式非法, 使用默认线: {line_coords!r}")
    return [[0.5, 0.1], [0.5, 0.9]]


def _anchor_from_coords(anchor_coords: Optional[str]) -> Optional[list[float]]:
    """解析 anchor_coords -> [x,y]; 无法解析时返回 None (由 counter 用默认锚点) 并告警."""
    if anchor_coords:
        try:
            parts = [float(x) for x in anchor_coords.split(",")]
            if len(parts) == 2:
                _warn_if_out_of_range([parts], "anchor_coords")
                return [parts[0], parts[1]]
        except ValueError:
            logger.warning(f"anchor_coords 格式非法, 使用默认锚点: {anchor_coords!r}")
    return None


def _roi_from_coords(roi_coords: Optional[str]) -> Optional[list[list[float]]]:
    """解析 roi_coords -> [[x,y],...]; 需偶数个值且 >=3 顶点, 否则返回 None 并告警."""
    if not roi_coords:
        return None
    try:
        parts = [float(x) for x in roi_coords.split(",")]
    except ValueError:
        logger.warning(f"roi_coords 格式非法, 忽略 ROI: {roi_coords!r}")
        return None
    if len(parts) % 2 != 0 or len(parts) < 6:
        logger.warning(f"roi_coords 需偶数个值且至少3个顶点, 忽略 ROI: {roi_coords!r}")
        return None
    polygon = [[parts[i], parts[i + 1]] for i in range(0, len(parts), 2)]
    _warn_if_out_of_range(polygon, "roi_coords")
    return polygon


async def _forward_to_ai(method: str, path: str, json_body: Optional[dict] = None) -> None:
    """转发到 AI 分析服务; 失败仅告警, 不阻塞设备配置落库."""
    url = f"{settings.ai_service_url}{path}"
    try:
        client = _get_client()
        if method == "POST":
            resp = await client.post(url, json=json_body)
        else:
            resp = await client.request(method, url)
        if resp.status_code >= 400:
            logger.warning(f"AI 服务转发失败 {method} {url}: HTTP {resp.status_code}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"AI 服务不可达 {method} {url}: {e}")


def _capture_frame_sync(url: str, max_frames: int = 30) -> Optional[bytes]:
    """同步拉流截一帧, 返回 JPEG bytes. 失败返回 None.

    网络流开头可能有空帧, 最多读 max_frames 帧找有效帧.
    """
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
async def list_():
    redis = get_redis()
    found = []
    async for key in redis.scan_iter(f"{_DEVICE_KEY_PREFIX}*"):
        data = await redis.hgetall(key)
        if data:
            # Redis hash 空值一律是 "", Pydantic v2 对 Optional[int] 无法解析空字符串,
            # 统一清洗为 None (兼容历史设备无 max_vehicles 字段).
            clean = dict(data)
            if clean.get("max_vehicles") == "":
                clean["max_vehicles"] = None
            # 按设备名称补全经纬度 (来自 data/device_geo.json)
            lng, lat = _geo_by_name(clean.get("name", ""))
            clean["longitude"] = lng
            clean["latitude"] = lat
            found.append(DeviceOut(**clean))
    return found


@router.post("", status_code=201)
async def register(dev: DeviceIn):
    """注册设备 (仅保存配置到 Redis, 不启流).

    启流统一由 POST /api/devices/{device_id}/enable 负责: 手动注册设备注册后
    status=registered, 配置计数线并 enable 后才启动 AI 管道.
    """
    redis = get_redis()
    key = f"{_DEVICE_KEY_PREFIX}{dev.id}"
    await redis.hset(
        key,
        mapping={
            "id": dev.id,
            "name": dev.name,
            "stream_url": dev.stream_url,
            "line_coords": dev.line_coords or "",
            "anchor_coords": dev.anchor_coords or "",
            "count_only": dev.count_only or "",
            "camera_type": dev.camera_type or "",
            "roi_coords": dev.roi_coords or "",
            "max_vehicles": str(dev.max_vehicles) if dev.max_vehicles is not None else "",
            "gb_device_id": dev.gb_device_id or "",
            "gb_channel_id": dev.gb_channel_id or "",
            "status": "registered",
        },
    )
    return {"id": dev.id, "status": "registered"}


@router.delete("/{device_id}")
async def remove(device_id: str):
    redis = get_redis()
    key = f"{_DEVICE_KEY_PREFIX}{device_id}"
    exists = await redis.exists(key)
    if not exists:
        raise HTTPException(404, "device not found")
    await redis.delete(key)
    await _forward_to_ai("DELETE", f"/devices/{device_id}")
    return {"status": "deleted", "id": device_id}


@router.post("/{device_id}/heartbeat")
async def heartbeat(device_id: str):
    """AI 服务心跳: 更新设备最后心跳时间, 标记设备在线."""
    from datetime import datetime, timezone
    redis = get_redis()
    key = f"{_DEVICE_KEY_PREFIX}{device_id}"
    exists = await redis.exists(key)
    if not exists:
        raise HTTPException(404, "device not found")
    now_iso = datetime.now(timezone.utc).isoformat()
    await redis.hset(key, mapping={
        "last_heartbeat": now_iso,
        "status": "online",
    })
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
    key = _DEVICE_KEY_PREFIX + device_id
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
            stream_url = wvp.select_stream_url(play)
            if not stream_url:
                raise HTTPException(502, "WVP 点播失败, 无法获取流地址")
            jpeg = await asyncio.wait_for(
                asyncio.to_thread(_capture_frame_sync, stream_url),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            raise HTTPException(504, "截帧超时 (流可能未就绪, 请重试)")
        finally:
            await wvp.stop_play(gb_dev, gb_ch)
    else:
        # 模式 2: 手动注册设备, 直接拉流截帧
        stream_url = data.get("stream_url", "")
        if not stream_url:
            raise HTTPException(400, "设备未配置 stream_url, 无法截帧")
        jpeg = await asyncio.wait_for(
            asyncio.to_thread(_capture_frame_sync, stream_url),
            timeout=15.0,
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
    data = await redis.hgetall(_DEVICE_KEY_PREFIX + device_id)
    if not data:
        raise HTTPException(404, "device not found")
    gb_dev = data.get("gb_device_id", "")
    gb_ch = data.get("gb_channel_id", "")
    if not gb_dev or not gb_ch:
        raise HTTPException(400, "非 WVP 同步设备, 无可刷新流地址")
    from ..core.wvp_client import get_wvp_client

    wvp = get_wvp_client()
    play = await wvp.start_play(gb_dev, gb_ch)
    stream_url = wvp.select_stream_url(play)
    if not stream_url:
        raise HTTPException(502, f"WVP 点播失败, 无法获取流地址 (gb_dev={gb_dev}, gb_ch={gb_ch})")
    lng, lat = _geo_by_name(data.get("name", ""))
    return {
        "device_id": device_id,
        "stream_url": stream_url,
        "stream_id": play.get("stream_id") if play else None,
        "longitude": lng,
        "latitude": lat,
    }


def _rewrite_url_base(url: str, base: str) -> str:
    """把 url 的 scheme://host:port 替换为 base, 保留 path/query (path 含 stream_id 是关键)."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    b = urlsplit(base)
    return urlunsplit((b.scheme, b.netloc, parts.path, parts.query, parts.fragment))


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
    data = await redis.hgetall(_DEVICE_KEY_PREFIX + device_id)
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
        if settings.zlm_public_base:
            flv = _rewrite_url_base(flv, settings.zlm_public_base.rstrip("/"))
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
    max_vehicles: Optional[int] = None  # 拥挤判断: ROI 内最大车辆数阈值 (设置后拥挤判断生效)


@router.post("/{device_id}/enable", status_code=200)
async def enable_device(device_id: str, body: DeviceEnableIn):
    """配置计数线并启流 (支持 WVP 同步设备和手动注册设备).

    两种模式:
      1. WVP 同步设备: 调 WVP 拿流地址 → 启流 (status: synced -> online).
      2. 手动注册设备: 更新配置 → 重启 AI 管道 (status: registered -> running).
    """
    redis = get_redis()
    key = _DEVICE_KEY_PREFIX + device_id
    data = await redis.hgetall(key)
    if not data:
        raise HTTPException(404, "device not found")

    gb_dev = data.get("gb_device_id", "")
    gb_ch = data.get("gb_channel_id", "")
    is_wvp = bool(settings.wvp_enabled and gb_dev and gb_ch)

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
        },
    )
    updated = await redis.hgetall(key)

    if is_wvp:
        # 模式 1: WVP 同步设备
        from ..core.wvp_client import get_wvp_client
        from ..core.wvp_sync import _start_ai_pipeline

        wvp = get_wvp_client()
        play = await wvp.start_play(gb_dev, gb_ch)
        stream_url = wvp.select_stream_url(play)
        if not stream_url:
            raise HTTPException(502, f"WVP 点播失败, 无法获取流地址 (gb_dev={gb_dev}, gb_ch={gb_ch})")

        await _start_ai_pipeline(updated, stream_url)
        await redis.hset(key, "status", "online")
        logger.info(f"[WVP] 设备 {device_id} 已启用并启流")
        return {"device_id": device_id, "status": "online", "stream_url": stream_url}
    else:
        # 模式 2: 手动注册设备 - 重启 AI 管道 (先停旧管道, 再起新管道)
        from ..core.wvp_sync import _start_ai_pipeline

        # 先停止旧管道 (如果存在)
        await _forward_to_ai("DELETE", f"/devices/{device_id}")

        # 再启动新管道
        await _start_ai_pipeline(updated, updated.get("stream_url", ""))
        await redis.hset(key, "status", "online")
        logger.info(f"[手动] 设备 {device_id} 已重新配置并启流")
        return {"device_id": device_id, "status": "online", "stream_url": updated.get("stream_url", "")}
