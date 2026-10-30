"""WVP 设备定时同步.

每 wvp_sync_interval 秒拉取 WVP 设备/通道, 与本地 Redis 设备表按 gb_device_id +
gb_channel_id 比对:
- 新通道: 入表 (status=synced), 不启流 (缺计数线, 等用户 POST /enable 配置)
- 已配置且 WVP 在线: AI 侧 pipeline 未运行则重新启流 (覆盖 WVP/ZLM 重启场景)
- WVP 离线: 停 AI pipeline, 标 offline
- WVP 恢复: 重新启流
- 通道在 WVP 侧改名: 跟随迁移本地设备键 (RENAME + 更新 id/name, 在跑的按新 ID 重启)

后台任务模式仿 core/heartbeat.py. 坐标解析/AI 转发复用 api/devices.py 的工具函数.
"""
import asyncio
import time
from typing import Optional

import httpx

from ...common import device_info
from ...common.config import settings
from ...common.counting_config import parse_counting_config
from ...common.logger import logger
from ...common.redis_client import get_redis
from .stream_urls import to_internal
from .wvp_client import get_wvp_client

_task: Optional[asyncio.Task] = None
_sync_lock = asyncio.Lock()
_play_failures: dict[tuple[str, str], tuple[int, float]] = {}

_DEVICE_KEY_PREFIX = f"{settings.redis_prefix}:device:"


def _online_flag(value) -> Optional[bool]:
    if value is True or value == 1 or value == "1":
        return True
    if value is False or value == 0 or value == "0":
        return False
    return None


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


async def _get_ai_running_devices() -> Optional[set[str]]:
    """返回运行集合；查询失败返回 None，不能把未知状态当作空集合。"""
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            resp = await c.get(f"{settings.ai_service_url}/devices")
            if resp.status_code != 200:
                logger.warning(f"查询 AI 设备列表失败: HTTP {resp.status_code}")
                return None
            data = resp.json()
            if not isinstance(data, list) or any(
                not isinstance(d, dict) or not isinstance(d.get("device_id"), str)
                or not isinstance(d.get("running"), bool) for d in data
            ):
                logger.warning("查询 AI 设备列表失败: 响应格式无效")
                return None
            return {d["device_id"] for d in data if d["running"]}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"查询 AI 设备列表失败: {e}")
    return None


async def _start_ai_pipeline(dev_data: dict, stream_url: str) -> bool:
    """构造 payload 并转发到 AI 启动 pipeline. 复用 devices.py 的坐标解析与转发."""
    from ..api.devices import (
        _forward_to_ai,
    )

    try:
        line, anchor, roi = parse_counting_config(
            dev_data.get("line_coords"), dev_data.get("anchor_coords"), dev_data.get("roi_coords"))
    except ValueError as exc:
        logger.error(f"[WVP同步] 设备 {dev_data.get('id', '')} 计数配置无效: {exc}")
        return False
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


async def _stop_ai_pipeline(device_id: str) -> bool:
    from ..api.devices import _forward_to_ai

    return await _forward_to_ai("DELETE", f"/devices/{device_id}")


async def _play_with_backoff(wvp, gb_dev: str, gb_ch: str) -> tuple[Optional[str], bool]:
    """按通道限制点播失败重试；第二个值表示本轮是否实际尝试。"""
    key = (gb_dev, gb_ch)
    failures, retry_at = _play_failures.get(key, (0, 0.0))
    now = time.monotonic()
    if now < retry_at:
        return None, False
    play = await wvp.start_play(gb_dev, gb_ch)
    stream_url = to_internal(wvp.select_stream_url(play)) if play else None
    if stream_url:
        _play_failures.pop(key, None)
        return stream_url, True
    failures += 1
    cap = max(1, int(settings.wvp_play_retry_max_s))
    base = max(1, int(settings.wvp_sync_interval))
    delay = min(cap, base * 2 ** min(failures - 1, 8))
    if len(_play_failures) >= 4096 and key not in _play_failures:
        _play_failures.pop(next(iter(_play_failures)))
    _play_failures[key] = (failures, now + delay)
    logger.warning(f'[WVP同步] {gb_dev}/{gb_ch} 点播失败，连续 {failures} 次，{delay} 秒后重试')
    return None, True


_RENAME_CHANNEL_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 or redis.call('EXISTS', KEYS[2]) ~= 0 then
    return 0
end
local fields = {'id', 'gb_device_id', 'gb_channel_id', 'line_coords',
                'anchor_coords', 'roi_coords', 'count_only', 'camera_type'}
for i, field in ipairs(fields) do
    if (redis.call('HGET', KEYS[1], field) or '') ~= ARGV[i + 1] then
        return 0
    end
end
redis.call('RENAME', KEYS[1], KEYS[2])
redis.call('HSET', KEYS[2], 'id', ARGV[1], 'name', ARGV[1])
return 1
"""


async def _follow_channel_rename(
    redis, wvp, gb_dev: str, gb_ch: str, old_data: dict, new_name: str, running: set
) -> Optional[str]:
    """仅在来源和运行状态可确认时迁移名称，失败时保留旧设备。"""
    old_id = old_data.get("id", "")
    if not old_id or not new_name or old_id == new_name or old_data.get("name") == new_name:
        return None
    if "/" in new_name:
        logger.warning(f"[WVP同步] 通道 {gb_dev}/{gb_ch} 新名含 '/': {new_name!r}, 跳过改名迁移 (请人工处理)")
        return None
    old_key = _DEVICE_KEY_PREFIX + old_id
    new_key = _DEVICE_KEY_PREFIX + new_name
    if not await redis.exists(old_key) or await redis.exists(new_key):
        logger.warning(
            f"[WVP同步] 通道 {gb_dev}/{gb_ch} 改名 {old_id!r} -> {new_name!r}, "
            "但旧键不存在或目标键已被占用, 跳过迁移 (请人工处理)"
        )
        return None

    was_running = old_id in running
    was_streaming = old_data.get("status", "") in ("online", "running") or was_running
    stream_url = None
    if was_streaming:
        try:
            parse_counting_config(old_data.get("line_coords"), old_data.get("anchor_coords"),
                                  old_data.get("roi_coords"))
        except ValueError as exc:
            logger.warning(f"[WVP同步] 改名前计数配置无效，保留设备 {old_id}: {exc}")
            return None
        # 先确认可点播，避免点播失败时停止仍在计数的旧管道。
        stream_url, _attempted = await _play_with_backoff(wvp, gb_dev, gb_ch)
        if not stream_url:
            return None
    if was_running:
        if not await _stop_ai_pipeline(old_id):
            logger.warning(f"[WVP同步] 无法确认旧管道已停止，保留设备 {old_id}")
            return None
        running.discard(old_id)

    fields = ("id", "gb_device_id", "gb_channel_id", "line_coords",
              "anchor_coords", "roi_coords", "count_only", "camera_type")
    # 校验身份和配置后原子迁移，禁止 RENAME 覆盖并发创建的目标键。
    renamed = await redis.eval(_RENAME_CHANNEL_LUA, 2, old_key, new_key, new_name,
                               *(old_data.get(field) or "" for field in fields))
    if not renamed:
        logger.warning(f"[WVP同步] 设备 {old_id} 迁移时身份、配置或目标键已变化")
        if was_running:
            current = await redis.hgetall(old_key)
            if current and await _start_ai_pipeline(current, stream_url):
                running.add(old_id)
            elif current:
                await redis.hset(old_key, mapping={"status": "synced", "ai_status": "start_failed"})
        return None
    logger.info(f"[WVP同步] 通道改名跟随: {old_id!r} -> {new_name!r}")

    if was_streaming:
        new_data = dict(old_data, id=new_name, name=new_name)
        if await _start_ai_pipeline(new_data, stream_url):
            running.add(new_name)
            await redis.hset(new_key, mapping={"status": "online", "ai_status": "waiting_first_frame"})
            logger.info(f"[WVP同步] 设备 {new_name} 改名后已按新 ID 重新启流")
        else:
            await redis.hset(new_key, mapping={"status": "synced", "ai_status": "start_failed"})
            logger.warning(f"[WVP同步] 设备 {new_name} 改名后启流失败, 保持 synced")
    return new_name


async def sync_once() -> dict:
    """执行一次同步, 返回 {added, started, stopped, recovered}."""
    async with _sync_lock:
        return await _sync_once()


async def _sync_once() -> dict:
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
    failed_gb_devices: set[str] = set()
    for dev in devices:
        gb_dev = dev.get("deviceId")
        if not gb_dev:
            continue
        dev_online = _online_flag(dev.get("online"))
        if dev_online is None:
            failed_gb_devices.add(gb_dev)
            logger.warning(f"WVP 设备在线状态未知 {gb_dev}")
            continue
        try:
            channels = await wvp.list_channels(gb_dev)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"拉取通道失败 {gb_dev}: {e}")
            failed_gb_devices.add(gb_dev)
            continue
        for ch in channels:
            gb_ch = ch.get("channelId")
            if not gb_ch:
                failed_gb_devices.add(gb_dev)
                continue
            wvp_channels[(gb_dev, gb_ch)] = ch.get("name") or dev.get("name") or gb_ch
            # channel.status 为通道在线标志 (WVP 2.7); 设备离线时通道必不可点播
            channel_online = _online_flag(ch.get("status"))
            if channel_online is None:
                failed_gb_devices.add(gb_dev)
                logger.warning(f"WVP 通道在线状态未知 {gb_dev}/{gb_ch}")
            if dev_online and channel_online is True:
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
    added = started = stopped = recovered = renamed = 0

    # device_info 已启用时, 启流/入表仅限其注册设备 (其余 WVP 通道不参与拉流/绘制计数)
    filter_registered = device_info.cache_size() > 0

    renamed_channels: set[tuple[str, str]] = set()
    # 3. 新增通道: 仅登记 device_info 已注册设备, 其余跳过 (不拉流/不绘制计数)
    for (gb_dev, gb_ch), name in wvp_channels.items():
        if (gb_dev, gb_ch) in local:
            # 未知状态、不可点播或目标名称未经注册时不可迁移已有管道。
            if (running is None or gb_dev in failed_gb_devices
                    or (gb_dev, gb_ch) not in online_channels
                    or (filter_registered and not device_info.is_registered(name))):
                continue
            old_data = local[(gb_dev, gb_ch)]
            new_id = await _follow_channel_rename(redis, wvp, gb_dev, gb_ch, old_data, name, running)
            if new_id:
                all_ids.discard(old_data.get("id", ""))
                all_ids.add(new_id)
                local[(gb_dev, gb_ch)] = await redis.hgetall(_DEVICE_KEY_PREFIX + new_id)
                renamed_channels.add((gb_dev, gb_ch))
                renamed += 1
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
        if (gb_dev, gb_ch) in renamed_channels:
            continue
        device_id = data.get("id", "")
        status = data.get("status", "")
        if gb_dev in failed_gb_devices:
            await redis.hset(_DEVICE_KEY_PREFIX + device_id, "wvp_sync_status", "channel_unknown")
            logger.warning(f"[WVP同步] 通道状态未知，保留设备 {device_id} 的现有状态")
            continue
        if running is None:
            # AI 列表不可用时既不能判定未运行，也不能安全删除或重启 pipeline。
            await redis.hset(_DEVICE_KEY_PREFIX + device_id, "wvp_sync_status", "ai_list_unknown")
            continue
        # 名称不匹配可能来自重命名或别名歧义；仅凭名称不能判定 GB 通道被移除。
        if filter_registered and not device_info.is_registered(data.get("name", "")):
            await redis.hset(_DEVICE_KEY_PREFIX + device_id, "wvp_sync_status", "registration_unconfirmed")
            logger.warning(f"[WVP同步] 设备 {device_id} 的点位注册关系无法确认，保留原配置和管道供核查")
            continue
        if data.get("wvp_sync_status") != "ok":
            await redis.hset(_DEVICE_KEY_PREFIX + device_id, "wvp_sync_status", "ok")
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

        try:
            parse_counting_config(data.get("line_coords"), data.get("anchor_coords"), data.get("roi_coords"))
        except ValueError as exc:
            if device_id in running:
                await _stop_ai_pipeline(device_id)
                stopped += 1
            await redis.hset(_DEVICE_KEY_PREFIX + device_id, "ai_status", "config_invalid")
            logger.error(f"[WVP同步] 设备 {device_id} 计数配置无效，停止计数: {exc}")
            continue

        is_running = device_id in running
        if status in ("offline", "synced", "online") and not is_running:
            stream_url, attempted = await _play_with_backoff(wvp, gb_dev, gb_ch)
            if not stream_url:
                if attempted:
                    await redis.hset(_DEVICE_KEY_PREFIX + device_id, "ai_status", "play_failed")
                continue
            # 有可用流地址之后再清 AI 的残留注册，避免点播失败时反复 DELETE。
            await _stop_ai_pipeline(device_id)
            if await _start_ai_pipeline(data, stream_url):
                await redis.hset(_DEVICE_KEY_PREFIX + device_id,
                                 mapping={"status": "online", "ai_status": "waiting_first_frame"})
                if status in ("offline", "synced"):
                    recovered += 1
                else:
                    started += 1
                logger.info(f"[WVP同步] 设备 {device_id} 已启流或恢复")
            else:
                await redis.hset(_DEVICE_KEY_PREFIX + device_id, "ai_status", "start_failed")
                logger.warning(f"[WVP同步] 设备 {device_id} 启流失败 (推理容量已满或 AI 服务异常)")

    result = {"added": added, "started": started, "stopped": stopped, "recovered": recovered,
              "renamed": renamed}
    if failed_gb_devices:
        result["failed_gb_devices"] = sorted(failed_gb_devices)
    if running is None:
        result["ai_devices_unknown"] = True
    return result


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
