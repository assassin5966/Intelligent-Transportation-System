"""单路端到端处理：输入帧 -> 轨迹 -> 越线 -> 持久化接口。"""
import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

import numpy as np

from app.ai.gpu_engine import InferOutcome
from app.ai.pipeline import DevicePipeline
from app.ai.stream import FramePacket
from app.ai.traj_state import RawTrack


class FakeEngine:
    def __init__(self):
        self.x = 0

    async def submit(self, device_id, frame, scale):
        x = self.x
        return InferOutcome(raw_tracks=[RawTrack(1, 2, "car", [x - 10, 490, x + 10, 510], .9)],
                            batch_size=1, wait_ms=1, infer_ms=1)


class FakeLease:
    mode = "cpu_batch"
    engine_id = "fake"

    def __init__(self):
        self.engine = FakeEngine()


class PipelineIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_health_does_not_treat_supervisor_as_ready(self):
        p = DevicePipeline("cam", "test://stream", ((.5, .1), (.5, .9)), lease=FakeLease())
        p._task = asyncio.create_task(asyncio.sleep(30))
        p._started_mono = time.monotonic() - 20
        try:
            self.assertEqual(p.health_snapshot()["state"], "no_frames")
            p._last_packet_mono = time.monotonic()
            self.assertEqual(p.health_snapshot()["state"], "no_recent_inference")
            p._last_success_mono = time.monotonic()
            self.assertEqual(p.health_snapshot()["state"], "processing")
            p._event_persist_started = time.monotonic() - 6
            self.assertEqual(p.health_snapshot()["state"], "event_persist_blocked")
        finally:
            p._task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await p._task
            await p._client.aclose()

    async def test_frame_to_count_persist_before_broadcast(self):
        lease = FakeLease()
        p = DevicePipeline("cam", "test://stream", ((.5, .1), (.5, .9)),
                           anchor=(.9, .5), lease=lease)
        p.counter.anti_jitter = False
        p.counter._apply_business_rules = lambda: None
        p._anomaly_monitor.check = lambda frame: None
        p._broadcast_tracks_ws = AsyncMock()
        p._broadcast_ws = AsyncMock()
        calls = []

        async def persist(events):
            calls.append(("persist", events))

        async def broadcast(event):
            calls.append(("broadcast", event))

        p._broadcast_ws.side_effect = broadcast
        with patch("app.ai.pipeline.event_outbox.enqueue_many", side_effect=persist):
            for seq, (x, mono, wall) in enumerate(((480, 10, 3599.8), (520, 10.4, 3600.2)), 1):
                lease.engine.x = x
                packet = FramePacket(np.zeros((8, 8, 3), dtype=np.uint8), seq, seq, seq,
                                     (1000, 1000), received_mono=mono, received_wall=wall)
                await p._process_frame(packet)
        try:
            self.assertEqual([kind for kind, _ in calls], ["persist", "broadcast"])
            payload = calls[0][1][0]
            self.assertEqual(payload["event_type"], "VehicleEnter")
            self.assertTrue(payload["event_id"])
            self.assertEqual(p._stats_frames, 2)
            self.assertEqual(p._stats_events, 1)
            # 事件使用穿线插值时间，跨小时不被确认时刻错误归档。
            from datetime import datetime
            self.assertAlmostEqual(datetime.fromisoformat(payload["occurred_at"]).timestamp(), 3600.0, places=5)
        finally:
            await p._client.aclose()

    async def test_invalid_time_does_not_run_inference(self):
        lease = FakeLease()
        lease.engine.submit = AsyncMock()
        p = DevicePipeline("cam", "test://stream", ((.5, .1), (.5, .9)), lease=lease)
        p._last_frame_mono = 10
        await p._process_frame(FramePacket(np.zeros((8, 8, 3), dtype=np.uint8), 1, 1, 1,
                                           (1000, 1000), received_mono=9))
        lease.engine.submit.assert_not_awaited()
        await p._client.aclose()

    async def test_1080p_mapping_and_windowed_quality_metrics(self):
        p = DevicePipeline("cam", "test://stream", ((.5, .1), (.5, .9)), lease=FakeLease())
        packet = FramePacket(np.zeros((540, 960, 3), dtype=np.uint8), 1, 1, 1,
                             (1920, 1080), decode_backend="ffmpeg", decode_max_fps=8)
        self.assertEqual(p._scale_to_stream(packet), (2.0, 2.0))
        p._source_size = packet.stream_size
        p._output_size = (960, 540)
        p._decode_backend = packet.decode_backend
        p._decode_max_fps = packet.decode_max_fps
        now = time.monotonic()
        for i in range(20):
            p._quality_samples.append((now - 1 + i * .04, now - 1 + i * .04, (i + 1) / 1000))
        h = p.health_snapshot()
        self.assertEqual(h["latency_sample_count_30s"], 20)
        self.assertEqual(h["receive_to_persist_p95_ms_30s"], 19.0)
        self.assertEqual(h["receive_to_persist_p99_ms_30s"], 20.0)
        self.assertTrue(h["source_is_1080p"])
        self.assertEqual(h["decode_max_fps_at_connect"], 8)
        self.assertGreaterEqual(h["max_processed_gap_s_30s"], .2)
        await p._client.aclose()
