import asyncio
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from app.ai.gpu_engine import EngineBusy, GpuInferEngine


class FakeModel:
    def __init__(self):
        self.entered = threading.Event()
        self.gate = threading.Event()
        self.gate.set()
        self.fail = False

    def predict(self, frames, **kwargs):
        self.entered.set()
        self.gate.wait(3)
        if self.fail:
            raise RuntimeError("prediction failed")
        return [SimpleNamespace(boxes=None) for _ in frames]

    def to(self, device):
        pass


class FakeTracker:
    def __init__(self):
        self.updates = []

    def update(self, det, frame):
        self.updates.append(det)
        return []


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.build = patch('app.ai.gpu_engine._build_tracker', side_effect=lambda _: FakeTracker())
        self.build.start()
        self.engine = GpuInferEngine('test', device='cpu', model_path='', slots=4,
            batch_timeout_s=0.01, queue_max_batches=1, tracker_cfg='', imgsz=32, half=False)
        self.engine._free_slots = list(range(4))
        self.engine._model = FakeModel()
        self.engine.attach_loop()
        for device in ('a', 'b', 'c', 'd'):
            self.engine.assign(device)
        self.frame = np.zeros((8, 8, 3), np.uint8)

    async def asyncTearDown(self):
        if self.engine._model is not None:
            self.engine._model.gate.set()
        if not self.engine._stopping:
            await self.engine.stop()
        self.build.stop()

    async def submit(self, device, frame=None):
        return await self.engine.submit(device, self.frame if frame is None else frame, (1, 1))

    async def wait_entered(self):
        self.assertTrue(await asyncio.to_thread(self.engine._model.entered.wait, 1))

    async def test_full_inflight_does_not_starve_loop_and_futures_finish(self):
        self.engine._model.gate.clear()
        a = asyncio.create_task(self.submit('a'))
        await self.wait_entered()
        b = asyncio.create_task(self.submit('b', np.zeros((9, 9, 3), np.uint8)))
        await asyncio.sleep(0.08)
        self.assertFalse(a.done())
        self.assertFalse(b.done())
        self.engine._model.gate.set()
        result = await asyncio.wait_for(asyncio.gather(a, b), 1)
        self.assertEqual(len(result), 2)
        self.assertLess(result[0].wait_ms, 50)  # 模型阻塞时间不计为组批等待
        self.assertEqual(self.engine.snapshot()['outstanding'], 0)
        self.assertEqual(self.engine.snapshot()['inflight'], 0)
        self.assertGreaterEqual(result[1].wait_ms, 50)

    async def test_single_request_per_device_and_cancellation(self):
        self.engine._model.gate.clear()
        a = asyncio.create_task(self.submit('a'))
        await self.wait_entered()
        with self.assertRaises(EngineBusy):
            await self.submit('a')
        a.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await a
        with self.assertRaises(EngineBusy):
            await self.submit('a')
        self.engine._model.gate.set()
        for _ in range(100):
            if not self.engine.snapshot()['outstanding']:
                break
            await asyncio.sleep(.01)
        await asyncio.wait_for(self.submit('a'), 1)

    async def test_pending_expiry_and_reset_generation(self):
        self.engine._max_queue_age_s = .06
        self.engine._model.gate.clear()
        a = asyncio.create_task(self.submit('a'))
        await self.wait_entered()
        b = asyncio.create_task(self.submit('b'))
        with self.assertRaises(EngineBusy):
            await asyncio.wait_for(b, .5)
        self.assertEqual(self.engine.drops_expired, 1)
        await self.engine.reset_device('a')
        self.engine._model.gate.set()
        with self.assertRaises(EngineBusy):
            await asyncio.wait_for(a, 1)
        self.assertEqual(self.engine.snapshot()['outstanding'], 0)

    async def test_expiry_inside_executor_queue(self):
        self.engine._max_inflight = 2
        self.engine._max_queue_age_s = .05
        self.engine._model.gate.clear()
        a = asyncio.create_task(self.submit('a'))
        await self.wait_entered()
        b = asyncio.create_task(self.submit('b', np.zeros((9, 9, 3), np.uint8)))
        await asyncio.sleep(.08)
        self.assertEqual(self.engine.snapshot()['inflight'], 2)
        self.engine._model.gate.set()
        await asyncio.wait_for(a, 1)
        with self.assertRaisesRegex(EngineBusy, 'expired'):
            await asyncio.wait_for(b, 1)
        self.assertEqual(self.engine.drops_expired, 1)

    async def test_reset_during_tracker_update_does_not_block_loop(self):
        started = threading.Event()
        release = threading.Event()
        previous_tracker = self.engine._trackers['a']
        touched = []

        def slow_track(req, res, tracker):
            touched.append(tracker)
            started.set()
            release.wait(1)
            return []

        self.engine._tracks_for = slow_track
        a = asyncio.create_task(self.submit('a'))
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        try:
            await asyncio.wait_for(self.engine.reset_device('a'), .1)
            self.assertIsNot(previous_tracker, self.engine._trackers['a'])
        finally:
            release.set()
        with self.assertRaisesRegex(EngineBusy, 'generation'):
            await asyncio.wait_for(a, 1)
        self.assertEqual(touched, [previous_tracker])

    async def test_unassign_reassign_discards_old_result(self):
        self.engine._model.gate.clear()
        a = asyncio.create_task(self.submit('a'))
        await self.wait_entered()
        self.engine.unassign('a')
        self.engine.assign('a')
        self.engine._model.gate.set()
        with self.assertRaisesRegex(EngineBusy, 'generation'):
            await asyncio.wait_for(a, 1)
        await asyncio.wait_for(self.submit('a'), 1)

    async def test_empty_detection_advances_tracker_with_raw_array(self):
        tracker = self.engine._trackers['a']
        result = await asyncio.wait_for(self.submit('a'), 1)
        self.assertEqual(result.raw_tracks, [])
        self.assertEqual(len(tracker.updates), 1)
        self.assertEqual(tracker.updates[0].shape, (0, 6))
        self.assertEqual(tracker.updates[0].dtype, np.float32)

    async def test_prediction_failure_and_recovery(self):
        self.engine._model.fail = True
        with self.assertRaisesRegex(RuntimeError, 'prediction failed'):
            await asyncio.wait_for(self.submit('a'), 1)
        self.assertEqual(self.engine.snapshot()['inflight'], 0)
        self.engine._model.fail = False
        await asyncio.wait_for(self.submit('a'), 1)

    async def test_stop_drains_pending_and_waits_without_blocking_loop(self):
        self.engine._model.gate.clear()
        a = asyncio.create_task(self.submit('a'))
        await self.wait_entered()
        b = asyncio.create_task(self.submit('b'))
        await asyncio.sleep(.03)
        stop = asyncio.create_task(self.engine.stop())
        with self.assertRaises(EngineBusy):
            await asyncio.wait_for(b, 1)
        self.assertFalse(stop.done())
        self.engine._model.gate.set()
        with self.assertRaises(EngineBusy):
            await asyncio.wait_for(a, 1)
        await asyncio.wait_for(stop, 2)
        self.assertEqual(self.engine.snapshot()['outstanding'], 0)


if __name__ == '__main__':
    unittest.main()
