"""Optional integration checks using the installed Ultralytics tracker implementation."""
import importlib.util
import itertools
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

HAS_ULTRALYTICS = importlib.util.find_spec('ultralytics') is not None


@unittest.skipUnless(HAS_ULTRALYTICS, 'Ultralytics is not installed')
class UltralyticsTrackerTests(unittest.TestCase):
    def test_construction_does_not_reset_other_tracker_ids(self):
        from app.ai.gpu_engine import _build_tracker, _load_tracker_args
        from ultralytics.engine.results import Boxes
        from ultralytics.trackers.basetrack import BaseTrack

        cfg = Path(__file__).resolve().parents[1] / 'configs' / 'bytetrack.yaml'
        for kind in ('bytetrack', 'botsort'):
            with self.subTest(kind=kind):
                args = _load_tracker_args(str(cfg))
                args.tracker_type = kind
                frame = np.zeros((100, 100, 3), np.uint8)
                old = Boxes(np.array([[10, 10, 30, 30, .95, 2]], dtype=np.float32), (100, 100))
                both = Boxes(np.array([[10, 10, 30, 30, .95, 2],
                                       [65, 65, 85, 85, .95, 2]], dtype=np.float32), (100, 100))
                first = _build_tracker(args)
                row = first.update(old, frame)
                first_id = int(row[0][4])
                BaseTrack._count = 91
                other = _build_tracker(args)
                self.assertEqual(BaseTrack._count, 91)
                other_id = int(other.update(old, frame)[0][4])
                self.assertNotEqual(first_id, other_id)
                first.update(both, frame)
                rows = first.update(both, frame)
                ids = {int(row[4]) for row in rows}
                self.assertEqual(len(ids), 2)
                self.assertIn(first_id, ids)
                self.assertNotIn(other_id, ids)
                first.reset()
                reset_id = int(first.update(old, frame)[0][4])
                self.assertNotIn(reset_id, ids | {other_id})
                self.assertEqual(BaseTrack._count, 91)
    def test_empty_frames_advance_and_expire_lost_tracks(self):
        from app.ai.gpu_engine import GpuInferEngine, _Req, _build_tracker, _load_tracker_args
        from types import SimpleNamespace
        from ultralytics.engine.results import Boxes

        cfg = Path(__file__).resolve().parents[1] / 'configs' / 'bytetrack.yaml'
        for kind in ('bytetrack', 'botsort'):
            with self.subTest(kind=kind):
                args = _load_tracker_args(str(cfg))
                args.tracker_type = kind
                args.track_buffer = 2
                frame = np.zeros((100, 100, 3), np.uint8)
                det = Boxes(np.array([[10, 10, 30, 30, .95, 2]], dtype=np.float32), (100, 100))
                tracker = _build_tracker(args)
                first_id = int(tracker.update(det, frame)[0][4])
                engine = GpuInferEngine('test', device='cpu', model_path='', slots=1,
                    batch_timeout_s=.04, queue_max_batches=1, tracker_cfg=str(cfg), imgsz=64, half=False)
                engine._det_boxed = True
                req = _Req('a', frame, (1, 1))
                for _ in range(6):
                    self.assertEqual(engine._tracks_for(req, SimpleNamespace(boxes=None), tracker), [])
                self.assertEqual(tracker.frame_id, 7)
                self.assertFalse(tracker.lost_stracks)
                tracker.update(det, frame)
                rows = tracker.update(det, frame)
                self.assertGreater(int(rows[0][4]), first_id)

    def test_large_ids_remain_distinct_after_float32_precision_boundary(self):
        from app.ai.gpu_engine import _build_tracker, _load_tracker_args
        from ultralytics.engine.results import Boxes

        cfg = Path(__file__).resolve().parents[1] / 'configs' / 'bytetrack.yaml'
        frame = np.zeros((100, 100, 3), np.uint8)
        det = Boxes(np.array([[10, 10, 30, 30, .95, 2]], dtype=np.float32), (100, 100))
        for kind in ('bytetrack', 'botsort'):
            with self.subTest(kind=kind), patch(
                    'app.ai.gpu_engine._TRACK_ID_SEQUENCE', itertools.count(2**24)):
                args = _load_tracker_args(str(cfg))
                args.tracker_type = kind
                first, second = _build_tracker(args), _build_tracker(args)
                first_result = first.update(det, frame)
                second_result = second.update(det, frame)
                self.assertEqual(first_result.dtype, np.float64)
                self.assertEqual(int(first_result[0][4]), 2**24)
                self.assertEqual(int(second_result[0][4]), 2**24 + 1)
                first.reset()
                self.assertEqual(int(first.update(det, frame)[0][4]), 2**24 + 2)


if __name__ == '__main__':
    unittest.main()
