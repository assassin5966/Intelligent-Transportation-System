"""拉流停顿恢复和接收时间回归，不依赖摄像头、FFmpeg 或 GPU。"""
import asyncio
import threading
import time
import shutil
import subprocess
from unittest.mock import patch

import numpy as np
import pytest

from app.ai import stream


def test_ffmpeg_capability_falls_back_to_vsync_and_caches():
    stream._ffmpeg_fps_option_cache.clear()
    with patch.object(stream.subprocess, "run", side_effect=[
        subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 0)
    ]) as run:
        assert stream._ffmpeg_passthrough_option("test-ffmpeg") == ("-vsync", "0")
        assert stream._ffmpeg_passthrough_option("test-ffmpeg") == ("-vsync", "0")
    assert run.call_count == 2
    assert "-fps_mode" in run.call_args_list[0].args[0]
    assert "-vsync" in run.call_args_list[1].args[0]


def test_ffmpeg_capability_failure_falls_back_to_opencv():
    stream._ffmpeg_fps_option_cache.clear()
    with patch.object(stream.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)):
        with pytest.raises(stream.StreamError, match="帧同步参数探测失败"):
            stream._ffmpeg_passthrough_option("test-ffmpeg")


def test_ffmpeg_failed_capability_probe_is_retried_after_cooldown():
    stream._ffmpeg_fps_option_cache.clear()
    with patch.object(stream.time, 'monotonic', return_value=1000), \
         patch.object(stream.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)):
        with pytest.raises(stream.StreamError):
            stream._ffmpeg_passthrough_option('test-retry-ffmpeg')
    with patch.object(stream.time, 'monotonic', return_value=1400), \
         patch.object(stream.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
        assert stream._ffmpeg_passthrough_option('test-retry-ffmpeg') == ('-fps_mode', 'passthrough')


@pytest.mark.parametrize('help_output,expected', [
    (b'  -stimeout <int64> socket timeout\n', '-stimeout'),
    (b'  -timeout <int64> socket timeout\n', '-timeout'),
    (b'  -not_timeout <int64>\n', None),
])
def test_rtsp_timeout_option_detects_old_and_new_ffmpeg(help_output, expected):
    stream._rtsp_timeout_option_cache.clear()
    result = subprocess.CompletedProcess([], 0, stdout=help_output, stderr=b'')
    with patch.object(stream.subprocess, 'run', return_value=result):
        assert stream._rtsp_timeout_option('test-ffmpeg') == expected


def test_reader_snapshot_keeps_frame_and_receive_time_together():
    class Capture:
        def __init__(self):
            self.calls = 0

        def grab(self):
            self.owner = threading.get_ident()
            self.calls += 1
            return self.calls == 1

        def retrieve(self):
            return True, np.zeros((2, 3, 3), dtype=np.uint8)

        def release(self):
            self.released_by = threading.get_ident()

    before = time.monotonic()
    cap = Capture()
    reader = stream._FrameReader(cap)
    reader._thread.join(timeout=1)
    seq, frame, failed, _, grabbed, received_mono, received_wall = reader.snapshot_with_time()
    assert seq == grabbed == 1 and frame.shape == (2, 3, 3) and failed
    assert before <= received_mono <= time.monotonic()
    assert abs(received_wall - time.time()) < 2
    assert len(reader.snapshot()) == 5  # 保持已有调用方兼容
    reader.close()
    assert cap.released_by == cap.owner


def test_no_new_frames_reconnects_and_advances_generation():
    readers = []

    class Reader:
        def __init__(self):
            self.closed = False
            self.mono = time.monotonic()
            self.wall = time.time()
            readers.append(self)

        def snapshot_with_time(self):
            return 1, np.zeros((2, 3, 3), dtype=np.uint8), False, 0, 1, self.mono, self.wall

        def close(self):
            self.closed = True

    async def exercise():
        with patch.object(stream, "_open_reader", side_effect=lambda *args: (Reader(), (3, 2))), \
             patch.object(stream.settings, "stream_stall_timeout_s", 0.04):
            generator = stream.stream_frames("test://stream")
            first = await asyncio.wait_for(anext(generator), 1)
            second = await asyncio.wait_for(anext(generator), 1)
            await generator.aclose()
            assert (first.stream_generation, second.stream_generation) == (0, 1)
            assert first.received_mono == readers[0].mono
            assert second.received_wall == readers[1].wall
            assert all(reader.closed for reader in readers)

    asyncio.run(exercise())


def test_last_complete_frame_is_delivered_before_reconnect():
    class Reader:
        def snapshot_with_time(self):
            return 1, np.zeros((2, 3, 3), dtype=np.uint8), True, 0, 1, 10.0, 100.0

        def close(self):
            pass

    async def exercise():
        with patch.object(stream, "_open_reader", return_value=(Reader(), (3, 2))):
            generator = stream.stream_frames("test://stream")
            packet = await asyncio.wait_for(anext(generator), 1)
            assert packet.sampled == 1 and packet.received_mono == 10.0
            await generator.aclose()

    asyncio.run(exercise())


def test_ffmpeg_sampling_does_not_duplicate_low_rate_frames():
    if not shutil.which("ffmpeg"):
        import pytest
        pytest.skip("ffmpeg unavailable")
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", "color=c=black:s=32x32:r=4:d=1",
           "-vf", stream._fps_select_filter(8), "-fps_mode", "passthrough",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    result = subprocess.run(cmd, capture_output=True, check=True, timeout=10)
    assert len(result.stdout) == 4 * 32 * 32 * 3


def test_1080p_output_canvas_and_unscaled_small_input():
    with patch.object(stream.settings, "decode_max_short_side", 540):
        assert stream._ffmpeg_out_size((1920, 1080)) == (960, 540)
        assert stream._ffmpeg_out_size((960, 540)) == (960, 540)


def test_25fps_source_is_not_mistaken_for_8fps_output():
    if not shutil.which("ffmpeg"):
        import pytest
        pytest.skip("ffmpeg unavailable")
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", "color=c=black:s=32x32:r=25:d=4",
           "-vf", stream._fps_select_filter(8), "-fps_mode", "passthrough",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    result = subprocess.run(cmd, capture_output=True, check=True, timeout=10)
    frames = len(result.stdout) // (32 * 32 * 3)
    assert frames == 25  # 100 帧、每 4 帧一帧：约 6.25 FPS，不是配置的 8 FPS


@pytest.mark.parametrize('source_fps,expected', [(4, 16), (8, 32), (15, 32), (25, 32)])
def test_optional_ffmpeg_grid_sampling_keeps_real_frames(source_fps, expected):
    exe = shutil.which('ffmpeg')
    if not exe:
        pytest.skip('ffmpeg unavailable')
    option = stream._ffmpeg_passthrough_option(exe)
    cmd = [exe, '-nostdin', '-hide_banner', '-loglevel', 'error',
           '-f', 'lavfi', '-i', f'color=c=black:s=32x32:r={source_fps}:d=4',
           '-vf', stream._fps_select_filter(8, 'grid'), *option,
           '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-']
    result = subprocess.run(cmd, capture_output=True, check=True, timeout=10)
    assert len(result.stdout) == expected * 32 * 32 * 3


def test_vsync_fallback_does_not_duplicate_low_rate_frames():
    exe = shutil.which('ffmpeg')
    if not exe:
        pytest.skip('ffmpeg unavailable')
    cmd = [exe, '-nostdin', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
           '-i', 'color=c=black:s=32x32:r=4:d=1', '-vf', stream._fps_select_filter(8),
           '-vsync', '0', '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-']
    result = subprocess.run(cmd, capture_output=True, check=True, timeout=10)
    assert len(result.stdout) == 4 * 32 * 32 * 3
