#!/usr/bin/env python3
"""
生成视频异常识别测试视频 (合成).

片段序列: 正常 -> 黑屏 -> 正常 -> 花屏(随机噪声) -> 正常
用于端到端验证 AnomalyMonitor / video_processor 的黑屏与花屏检测.

仅需 numpy + opencv, 可在宿主机直接运行 (无需 Docker / YOLO):
    python3 scripts/gen_anomaly_video.py
    python3 scripts/gen_anomaly_video.py --output data/anomaly_test.mp4 --segment 90

输出默认: data/anomaly_test.mp4 (640x480 @ 25fps)
"""
import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

PROJECT_DIR = Path(__file__).resolve().parent.parent


def _normal_frame(h=480, w=640) -> np.ndarray:
    """合成正常帧: 灰度横向渐变 + 几块稳定色块 (通道高相关, 有结构纹理, 静态)."""
    img = np.zeros((h, w, 3), dtype=np.uint8)
    grad = np.tile(np.linspace(0, 255, w, dtype=np.uint8), (h, 1))
    img[:, :, 0] = grad
    img[:, :, 1] = grad
    img[:, :, 2] = grad
    img[60:140, 80:240] = (60, 120, 180)
    img[200:300, 300:480] = (200, 100, 50)
    img[340:420, 120:300] = (40, 160, 90)
    return img


def _black_frame(h=480, w=640) -> np.ndarray:
    return np.zeros((h, w, 3), dtype=np.uint8)


def _noise_frame(h=480, w=640, seed: int = 0) -> np.ndarray:
    """花屏帧: 每通道独立随机 (通道去相关 + 高空间/时域噪声)."""
    rng = np.random.RandomState(seed)
    return rng.randint(0, 256, size=(h, w, 3), dtype=np.uint8)


def main():
    ap = argparse.ArgumentParser(description="生成视频异常识别测试视频")
    ap.add_argument("--output", default="data/anomaly_test.mp4", help="输出路径")
    ap.add_argument("--segment", type=int, default=90, help="每段帧数 (默认 90)")
    ap.add_argument("--fps", type=int, default=25, help="帧率 (默认 25)")
    ap.add_argument("--width", type=int, default=640, help="宽度")
    ap.add_argument("--height", type=int, default=480, help="高度")
    args = ap.parse_args()

    out_path = Path(args.output)
    if not out_path.is_absolute():
        out_path = PROJECT_DIR / args.output
    out_path.parent.mkdir(parents=True, exist_ok=True)

    w, h = args.width, args.height
    n = args.segment
    fps = args.fps

    # 段序列: 正常 -> 黑屏 -> 正常 -> 花屏 -> 正常
    segments = [
        ("normal", n),
        ("black", n),
        ("normal", n),
        ("noise", n),
        ("normal", n),
    ]

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(out_path), fourcc, fps, (w, h))
    if not writer.isOpened():
        print(f"错误: 无法创建视频写入器 {out_path}", file=sys.stderr)
        sys.exit(1)

    total = 0
    seed = 0
    for seg_type, seg_len in segments:
        for _ in range(seg_len):
            if seg_type == "normal":
                frame = _normal_frame(h, w)
            elif seg_type == "black":
                frame = _black_frame(h, w)
            elif seg_type == "noise":
                frame = _noise_frame(h, w, seed)
                seed += 1
            else:
                frame = _normal_frame(h, w)
            writer.write(frame)
            total += 1

    writer.release()
    print(f"已生成测试视频: {out_path}")
    print(f"  分辨率: {w}x{h} | 帧率: {fps} | 总帧数: {total} | 时长: {total/fps:.1f}s")
    print(f"  片段序列 (每段 {n} 帧): 正常 -> 黑屏 -> 正常 -> 花屏 -> 正常")


if __name__ == "__main__":
    main()
