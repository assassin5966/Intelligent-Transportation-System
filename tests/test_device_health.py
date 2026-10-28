"""设备业务状态、失败关闭及已配置设备的自动恢复。"""
import unittest
from unittest.mock import AsyncMock, patch

from app.ai.model_pool import ModelPool
from app.backend.api.devices import DeviceHeartbeat, heartbeat
from app.backend.core import wvp_sync


class MemoryRedis:
    def __init__(self, values):
        self.values = values

    async def scan_iter(self, pattern):
        for key in list(self.values):
            yield key

    async def hgetall(self, key):
        return dict(self.values.get(key, {}))

    async def exists(self, key):
        return key in self.values

    async def hset(self, key, name=None, value=None, mapping=None):
        self.values.setdefault(key, {}).update(mapping or {name: value})


class FakeWvp:
    enabled = True

    async def list_devices(self):
        return [{"deviceId": "gb", "online": True}]

    async def list_channels(self, device_id):
        return [{"channelId": "ch", "status": True, "name": "cam"}]

    async def start_play(self, device_id, channel_id):
        return {"flv": "http://example/stream"}

    def select_stream_url(self, result):
        return result["flv"]


class DeviceHealthTest(unittest.IsolatedAsyncioTestCase):
    async def test_heartbeat_has_separate_processing_state_and_old_body_compatible(self):
        from app.backend.api import devices
        key = devices._DEVICE_KEY_PREFIX + "cam"
        redis = MemoryRedis({key: {"id": "cam"}})
        with patch.object(devices, "get_redis", return_value=redis):
            await heartbeat("cam", DeviceHeartbeat(ai_status="no_recent_inference"))
            self.assertEqual(redis.values[key]["status"], "online")
            self.assertEqual(redis.values[key]["ai_status"], "no_recent_inference")
            await heartbeat("cam")
            self.assertEqual(redis.values[key]["ai_status"], "no_recent_inference")

    async def test_synced_device_with_line_retries_start_and_marks_readiness(self):
        key = wvp_sync._DEVICE_KEY_PREFIX + "cam"
        redis = MemoryRedis({key: {"id": "cam", "name": "cam", "gb_device_id": "gb",
                                   "gb_channel_id": "ch", "line_coords": "0,0,1,1", "status": "synced"}})
        with patch.object(wvp_sync, "get_redis", return_value=redis), \
             patch.object(wvp_sync, "get_wvp_client", return_value=FakeWvp()), \
             patch.object(wvp_sync.device_info, "cache_size", return_value=0), \
             patch.object(wvp_sync, "_get_ai_running_devices", AsyncMock(return_value=set())), \
             patch.object(wvp_sync, "_stop_ai_pipeline", AsyncMock()), \
             patch.object(wvp_sync, "_start_ai_pipeline", AsyncMock(return_value=True)) as start, \
             patch.object(wvp_sync, "to_internal", side_effect=lambda value: value):
            result = await wvp_sync.sync_once()
        start.assert_awaited_once()
        self.assertEqual(result["recovered"], 1)
        self.assertEqual(redis.values[key]["ai_status"], "waiting_first_frame")

    async def test_explicit_gpu_failure_does_not_enable_legacy(self):
        pool = ModelPool()
        with patch("app.ai.model_pool.settings.infer_mode", "gpu_batch"), \
             patch.object(pool, "_probe_cuda", return_value=(False, "no cuda")):
            await pool.initialize()
        self.assertEqual(pool.mode, "unavailable")
        self.assertIsNone(pool.acquire("cam"))
