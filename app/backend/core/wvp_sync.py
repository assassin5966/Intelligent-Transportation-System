"""WVP 设备定时同步.

每 wvp_sync_interval 秒拉取 WVP 设备/通道, 与本地 Redis 设备表按 gb_device_id +
gb_channel_id 比对:
- 新通道: 入表 (status=synced), 不启流 (缺计数线, 等用户 POST /enable 配置)
- 已配置且 WVP 在线: AI 侧 pipeline 未运行则重新启流 (覆盖 WVP/ZLM 重启场景)
- WVP 离线: 停 AI pipeline, 标 offline
- WVP 恢复: 重新启流

后台任务模式仿 core/heartbeat.py. 坐标解析/AI 转发复用 api/devices.py 的工具函数.
"""
import asyncio
from typing import Optional

import httpx

from ...common import device_info
from ...common.config import settings
from ...common.logger import logger
from ...common.redis_client import get_redis
from .stream_urls import to_internal
from .wvp_client import get_wvp_client

_task: Optional[asyncio.Task] = None

_DEVICE_KEY_PREFIX = f"{settings.redis_prefix}:device:"


def _infer_camera_type(name: str) -> Optional[str]:
    """从设备/通道名启发式推断 camera_type (含「车」→vehicle, 含「人」→person)."""
    if not name:
        return None
    lower = name.lower()
    if "车" in name or "vehicle" in lower:
        return "vehicle"
    if "人" in name or "person" in lower:
        return "person"
    return None


def _camera_type_from_category(category: Optional[str]) -> Optional[str]:
    """按 device_info 点位分类推断 camera_type (卡口/车→vehicle, 便道/人→person)."""
    cat = category or ""
    if "卡口" in cat or "车" in cat:
        return "vehicle"
    if "便道" in cat or "人" in cat:
        return "person"
    return None


async def _get_ai_running_devices() -> set[str]:
    """一次性拉取 AI 服务在跑的 device_id 集合 (供同步判重, 避免逐设备查询)."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            resp = await c.get(f"{settings.ai_service_url}/devices")
            if resp.status_code == 200:
                return {
                    d["device_id"] for d in resp.json() if d.get("running")
                }
    except Exception as e:  # noqa: BLE001
        logger.warning(f"查询 AI 设备列表失败: {e}")
    return set()


async def _start_ai_pipeline(dev_data: dict, stream_url: str) -> bool:
    """构造 payload 并转发到 AI 启动 pipeline. 复用 devices.py 的坐标解析与转发."""
    from ..api.devices import (
        _forward_to_ai,
        _line_from_coords,
        _anchor_from_coords,
        _roi_from_coords,
    )

    line = _line_from_coords(dev_data.get("line_coords") or "")
    anchor = _anchor_from_coords(dev_data.get("anchor_coords") or "")
    roi = _roi_from_coords(dev_data.get("roi_coords") or "")
    payload: dict = {
        "device_id": dev_data["id"],
        "stream_url": stream_url,
        "line": line,
    }
    if anchor is not None:
        payload["anchor"] = anchor
    count_only = dev_data.get("count_only")
    if count_only:
        payload["count_only"] = count_only
    camera_type = dev_data.get("camera_type")
    if camera_type:
        payload["camera_type"] = camera_type
    if roi is not None:
        payload["roi"] = roi
    gb_dev = dev_data.get("gb_device_id")
    gb_ch = dev_data.get("gb_channel_id")
    if gb_dev:
        payload["gb_device_id"] = gb_dev
    if gb_ch:
        payload["gb_channel_id"] = gb_ch
    return await _forward_to_ai("POST", "/devices", payload)


async def _stop_ai_pipeline(device_id: str) -> None:
    from ..api.devices import _forward_to_ai

    await _forward_to_ai("DELETE", f"/devices/{device_id}")


async def sync_once() -> dict:
    """执行一次同步, 返回 {added, started, stopped, recovered}."""
    wvp = get_wvp_client()
    if not wvp.enabled:
        return {"added": 0, "started": 0, "stopped": 0, "recovered": 0, "skipped": "wvp_disabled"}

    redis = get_redis()

    # 1. 拉 WVP 全量通道 (含离线设备): 离线设备也要入表, 运维页才能提前画线/配置;
    #    online_channels 只收录「设备在线且通道在线」的通道, 用于决定是否拉流/恢复.
    try:
        devices = await wvp.list_devices()
    except Exception as e:  # noqa: BLE001
        logger.error(f"WVP 设备列表拉取失败, 跳过本次同步: {e}")
        return {"added": 0, "started": 0, "stopped": 0, "recovered": 0, "error": str(e)}

    wvp_channels: dict[tuple[str, str], str] = {}  # (gb_dev, gb_ch) -> name
    online_channels: set[tuple[str, str]] = set()  # 设备与通道均在线, 可点播
    for dev in devices:
        gb_dev = dev.get("deviceId")
        if not gb_dev:
            continue
        dev_online = bool(dev.get("online"))
        try:
            channels = await wvp.list_channels(gb_dev)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"拉取通道失败 {gb_dev}: {e}")
            continue
        for ch in channels:
            gb_ch = ch.get("channelId")
            if not gb_ch:
                continue
            wvp_channels[(gb_dev, gb_ch)] = ch.get("name") or dev.get("name") or gb_ch
            # channel.status 为通道在线标志 (WVP 2.7); 设备离线时通道必不可点播
            if dev_online and ch.get("status", True):
                online_channels.add((gb_dev, gb_ch))

    # 2. 读本地设备表 (含 gb 映射); all_ids 收集全部现有设备 id (含手动注册), 供新增时查冲突
    local: dict[tuple[str, str], dict] = {}
    all_ids: set[str] = set()
    async for key in redis.scan_iter(f"{_DEVICE_KEY_PREFIX}*"):
        data = await redis.hgetall(key)
        if not data:
            continue
        if data.get("id"):
            all_ids.add(data["id"])
        gb_dev = data.get("gb_device_id", "")
        gb_ch = data.get("gb_channel_id", "")
        if gb_dev and gb_ch:
            local[(gb_dev, gb_ch)] = data

    running = await _get_ai_running_devices()
    added = started = stopped = recovered = 0

    # device_info 已启用时, 启流/入表仅限其注册设备 (其余 WVP 通道不参与拉流/绘制计数)
    filter_registered = device_info.cache_size() > 0

    # 3. 新增通道: 仅登记 device_info 已注册设备, 其余跳过 (不拉流/不绘制计数)
    for (gb_dev, gb_ch), name in wvp_channels.items():
        if (gb_dev, gb_ch) in local:
            continue
        if filter_registered and not device_info.is_registered(name):
            logger.info(f"[WVP同步] 通道 {name} 未在 device_info 注册, 跳过 (不参与拉流/计数)")
            continue
        # 名称即身份: 生产链路 (WVP拉流 -> device_info 白名单按名称过滤 -> 运维画线启流)
        # 中 ID 全程无需人工编写, 直接用可读点位名作 id (与白名单同键, 运维页/事件/日志可读);
        # 名称含 '/' (破坏 URL 路由) 或与现有设备 id 冲突时回退 GB 编号形式, 防覆盖他路
        device_id = name
        if "/" in device_id or device_id in all_ids:
            device_id = f"GB-{gb_dev}-{gb_ch}"
        camera_type = _infer_camera_type(name)
        if not camera_type:
            pt = device_info.get(name)
            camera_type = _camera_type_from_category(pt.get("category") if pt else None)
        await redis.hset(
            _DEVICE_KEY_PREFIX + device_id,
            mapping={
                "id": device_id,
                "name": name,
                "stream_url": "",
                "line_coords": "",
                "anchor_coords": "",
                "count_only": "",
                "camera_type": camera_type or "",
                "roi_coords": "",
                "gb_device_id": gb_dev,
                "gb_channel_id": gb_ch,
                "status": "synced",
            },
        )
        added += 1
        logger.info(f"[WVP同步] 新增设备 {device_id} ({name}), 待配置计数线")

    # 4. 已有设备: 按在线/离线/恢复处理
    for (gb_dev, gb_ch), data in local.items():
        device_id = data.get("id", "")
        status = data.get("status", "")
        # 4.0 非 device_info 注册设备: 停止拉流并从设备表移除 (不参与绘制计数/拉流)
        if filter_registered and not device_info.is_registered(data.get("name", "")):
            if status in ("online", "synced") or device_id in running:
                await _stop_ai_pipeline(device_id)
            await redis.delete(_DEVICE_KEY_PREFIX + device_id)
            stopped += 1
            logger.warning(f"[WVP同步] 设备 {device_id} ({data.get('name', '')}) 未在 device_info 注册, 已停止拉流并移除")
            continue
        has_line = bool(data.get("line_coords"))
        in_wvp = (gb_dev, gb_ch) in wvp_channels
        playable = (gb_dev, gb_ch) in online_channels

        if not in_wvp:
            # 通道已从 WVP 删除: 在线/synced 状态需停止
            if status in ("online", "synced"):
                await _stop_ai_pipeline(device_id)
                await redis.hset(_DEVICE_KEY_PREFIX + device_id, "status", "offline")
                stopped += 1
                logger.warning(f"[WVP同步] 设备 {device_id} 通道已移除, 已停止 pipeline")
            continue

        if not playable:
            # WVP 侧设备/通道离线: 停止拉流并标 offline, 设备保留在设备表供运维页配置
            if status in ("online", "running") or device_id in running:
                await _stop_ai_pipeline(device_id)
                await redis.hset(_DEVICE_KEY_PREFIX + device_id, "status", "offline")
                stopped += 1
                logger.warning(f"[WVP同步] 设备 {device_id} WVP 侧离线, 已停止 pipeline")
            continue

        # WVP 在线
        if not has_line:
            # WVP 侧可点播但未配置/未启流: 状态应为 synced, 不应停留在 offline
            # (能截帧/预览的设备显示"离线"具有误导性); 有计数线的恢复流程不受影响
            if status == "offline" and device_id not in running:
                await redis.hset(_DEVICE_KEY_PREFIX + device_id, "status", "synced")
            continue  # 未配置计数线, 不启流

        is_running = device_id in running
        if status in ("offline", "synced") and not is_running:
            # 恢复: 先清 AI 侧残留注册 (流断后 pipeline 对象仍留在 _pipelines, 直接 POST 会 409)
            await _stop_ai_pipeline(device_id)
            play = await wvp.start_play(gb_dev, gb_ch)
            stream_url = to_internal(wvp.select_stream_url(play))
            if stream_url:
                if await _start_ai_pipeline(data, stream_url):
                    await redis.hset(_DEVICE_KEY_PREFIX + device_id, mapping={"status": "online", "ai_status": "waiting_first_frame"})
                    recovered += 1
                    logger.info(f"[WVP同步] 设备 {device_id} 恢复在线, 已启流")
                else:
                    await redis.hset(_DEVICE_KEY_PREFIX + device_id, "ai_status", "start_failed")
                    logger.warning(
                        f"[WVP同步] 设备 {device_id} 启流失败 (推理容量已满或 AI 服务异常), 保持 synced"
                    )
        elif status == "online" and not is_running:
            # 在线但 AI 侧没跑 (WVP/ZLM 重启后); 同样先清残留, 避免 409
            await _stop_ai_pipeline(device_id)
            play = await wvp.start_play(gb_dev, gb_ch)
            stream_url = to_internal(wvp.select_stream_url(play))
            if stream_url:
                if await _start_ai_pipeline(data, stream_url):
                    await redis.hset(_DEVICE_KEY_PREFIX + device_id, "ai_status", "waiting_first_frame")
                    started += 1
                    logger.info(f"[WVP同步] 设备 {device_id} AI 侧未运行, 已重新启流")
                else:
                    await redis.hset(_DEVICE_KEY_PREFIX + device_id, "ai_status", "start_failed")
                    logger.warning(
                        f"[WVP同步] 设备 {device_id} 重新启流失败 (推理容量已满或 AI 服务异常)"
                    )

    return {"added": added, "started": started, "stopped": stopped, "recovered": recovered}


async def _loop() -> None:
    while True:
        try:
            await sync_once()
        except Exception as e:  # noqa: BLE001
            logger.error(f"WVP 同步异常: {e}")
        await asyncio.sleep(settings.wvp_sync_interval)


async def start_syncer() -> None:
    global _task
    if not settings.wvp_enabled:
        logger.info("WVP 同步未启用 (wvp_enabled=false)")
        return
    if _task is None:
        _task = asyncio.create_task(_loop())
        logger.info(f"WVP 设备同步已启动 (每 {settings.wvp_sync_interval}s)")


async def stop_syncer() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
        logger.info("WVP 设备同步已停止")
