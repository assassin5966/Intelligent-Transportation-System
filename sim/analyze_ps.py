# -*- coding: utf-8 -*-
"""解剖 ffmpeg 输出的 MPEG-PS 流, 判定 pack 头/PES 头是 MPEG-1 还是 MPEG-2 布局。

用法: python analyze_ps.py <ps文件> [前N个pack]
输出: 各起始码统计 / pack 头版本与长度 / PES 头前 16 字节 / 两种 PTS 解析对照

判据 (ISO/IEC 13818-1 vs 11172-1):
  pack 头 00 00 01 BA 之后第 1 字节高 4 位:
    '0100' (0x4) -> MPEG-2 pack 头 (BA 后共 10 字节, SCR+SCR_ext+mux_rate+stuffing)
    '0010' (0x2) -> MPEG-1 pack 头 (BA 后共 6 字节)
  PES 头 00 00 01 Ex 之后:
    MPEG-2: [len(2)] [flags1='10xx..'] [flags2] [hdrlen] [PTS...]
    MPEG-1: [len(2)] [stuffing/PTS 直接开始, '0010'=PTS]
"""
import sys

def mpeg2_pts(b):
    return (((b[0] >> 1) & 0x07) << 30) | \
           ((int.from_bytes(b[1:3], "big") >> 1) << 15) | \
           (int.from_bytes(b[3:5], "big") >> 1)

def mpeg1_pts(b):
    # b[0]: '0010' PTS[32:30] '1'; b[1:3]: PTS[29:15]'1'; b[3:5]: PTS[14:0]'1' —— 编码其实相同
    return mpeg2_pts(b)

data = open(sys.argv[1], "rb").read()
npacks = int(sys.argv[2]) if len(sys.argv) > 2 else 20

# ---- 起始码统计 ----
i = 0
codes = {"BA": 0, "BB": 0, "BC": 0, "E0": 0, "BD": 0, "B9": 0}
positions = []          # (code, offset)
while i + 3 < len(data):
    if data[i:i+3] == b"\x00\x00\x01":
        b = data[i+3]
        tag = "%02X" % b
        if tag in codes:
            codes[tag] += 1
            positions.append((tag, i))
        i += 4
    else:
        i += 1
print("起始码统计: %s  (总 %d 字节)" % (codes, len(data)))
print("PSM(BC): %s  —— 无 PSM 时部分收流端无法识别 H.264 流类型"
      % ("有" if codes["BC"] else "无"))

# ---- pack 头版本判定 ----
pack_v2 = pack_v1 = 0
pes_mpeg2 = pes_mpeg1 = 0
pts_v2_list, pts_v1_list = [], []
shown = 0
for tag, off in positions:
    if tag == "BA":
        hi = data[off+4] >> 4
        if hi == 4:
            pack_v2 += 1
            pack_len = 10
        elif hi == 2:
            pack_v1 += 1
            pack_len = 6
        else:
            print("!! 未知 pack 头版本: 0x%X @%d" % (hi, off))
            continue
        if shown < 3:
            print("pack@%-8d %s 头部: %s" %
                  (off, "MPEG-2" if hi == 4 else "MPEG-1",
                   data[off:off+4+pack_len+4].hex(" ")))
    if tag == "E0" and shown < npacks:
        shown += 1
        hdr = data[off:off+20]
        b6 = hdr[6]
        if (b6 & 0xC0) == 0x80:
            # MPEG-2: flags1 高2位 '10'
            pes_mpeg2 += 1
            pts = mpeg2_pts(hdr[9:14]) if (hdr[7] & 0x80) else None
            if pts is not None:
                pts_v2_list.append(pts)
            if shown <= 5:
                print("PES @%-8d MPEG-2: len=%s flags1=%02X flags2=%02X hlen=%d PTS=%s"
                      % (off, hdr[4:6].hex(), b6, hdr[7], hdr[8], pts))
        else:
            # MPEG-1: stuffing 或 PTS ('0010' = 0x2x)
            pes_mpeg1 += 1
            pts = None
            j = 6
            while j < 20 and (hdr[j] & 0xC0) == 0x80:   # stuffing '1xxxxxxx'
                j += 1
            if (hdr[j] >> 4) == 2:
                pts = mpeg1_pts(hdr[j:j+5])
                pts_v1_list.append(pts)
            if shown <= 5:
                print("PES @%-8d MPEG-1: len=%s 字节[6..]=%s PTS=%s"
                      % (off, hdr[4:6].hex(), hdr[6:14].hex(" "), pts))

print()
print("pack 头: MPEG-2=%d  MPEG-1=%d" % (pack_v2, pack_v1))
print("PES 头 : MPEG-2=%d  MPEG-1=%d" % (pes_mpeg2, pes_mpeg1))

def deltas(lst):
    return [b - a for a, b in zip(lst, lst[1:])][:8]

if pts_v2_list:
    print("MPEG-2 方式解析 PTS 序列(前10): %s" % pts_v2_list[:10])
    print("             帧间增量: %s  (3600=25fps 正确)" % deltas(pts_v2_list))
if pts_v1_list:
    print("MPEG-1 方式解析 PTS 序列(前10): %s" % pts_v1_list[:10])
    print("             帧间增量: %s" % deltas(pts_v1_list))

print()
verdict = "MPEG-2 PS" if pack_v2 and not pack_v1 else \
          ("MPEG-1 系统流" if pack_v1 and not pack_v2 else "混合/未知")
print(">>> 结论: 该流是 %s" % verdict)
if pack_v1:
    print(">>> !! MPEG-1 系统流不是 GB28181 要求的 MPEG-2 PS,")
    print(">>>     pack 头/PES 头布局不同, 收流端(ZLM)可能解不出后续帧时间戳,")
    print(">>>     表现为: 收到流、能出第一帧、之后画面冻结。")
