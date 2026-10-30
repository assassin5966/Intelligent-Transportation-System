# -*- coding: utf-8 -*-
"""RTP/PS 打包层单测 (可在本机和服务器直接跑: python3 test_rtp_pack.py)

覆盖:
  1. _pes_unit_parse —— MPEG-2 / MPEG-1 两种 PES 头布局解析 (PTS + 载荷)
  2. _flush_packs    —— 任意 os.read 切块不丢帧; 重建 GB28181 MPEG-2 PS
  3. 重建输出合规    —— BA(MPEG-2)/BB/BC(PSM 声明 H.264->E0)/E0 PES + PTS
  4. 时间戳回跳      —— 素材 stream_loop 重绕后仍严格递增
  5. 多路并发        —— 两个 RtpPusher 交错推流互不干扰
  6. 真实 ffmpeg 流  —— test_fixture_ps.mpeg (ffmpeg -f mpeg 实际输出,
                        MPEG-1 系统流/E2/无PSM) 端到端解析重建
  7. WvpReplayer     —— 登录 token 提取 / 失败原因上报
"""
import importlib.util
import json as _json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "gb", os.path.join(HERE, "gb28181_device.py"))
gb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gb)

FAILS = []


def check(name, cond, detail=""):
    tag = "OK  " if cond else "FAIL"
    print("  [%s] %s%s" % (tag, name, ("  " + detail) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# ------------------------------------------------------------------ #
# 构造工具
# ------------------------------------------------------------------ #
def ts_encode(v, prefix=0x2):
    """5 字节 PTS/DTS 编码 (与 ffmpeg put_timestamp 一致)。"""
    return (bytes([(prefix << 4) | (((v >> 30) & 0x07) << 1) | 1]) +
            ((((v >> 15) & 0x7FFF) << 1) | 1).to_bytes(2, "big") +
            (((v & 0x7FFF) << 1) | 1).to_bytes(2, "big"))


def pes_mpeg2(pts, payload, dts=None):
    """MPEG-2 PES: E0 | len | 80 | flags2 | hlen | PTS | payload。"""
    if dts is None:
        flags2, opt, hlen = 0x80, ts_encode(pts), 5
    else:
        flags2, opt, hlen = 0xC0, ts_encode(pts, 3) + ts_encode(dts, 1), 10
    plen = 3 + hlen + len(payload)
    return (b"\x00\x00\x01\xe0" + plen.to_bytes(2, "big") +
            bytes([0x80, flags2, hlen]) + opt + payload)


def pes_mpeg1(pts, payload):
    """MPEG-1 PES: E0 | len | [PTS] | payload (无 flags/hdrlen 三字节)。"""
    opt = ts_encode(pts) if pts is not None else b"\x0f"
    plen = len(opt) + len(payload)
    return (b"\x00\x00\x01\xe2" + plen.to_bytes(2, "big") + opt + payload)


def make_pack(pts, payload=b"\x0d" * 1500, first=False):
    """模拟国标设备: BA + [BB] + PES(MPEG-2)。"""
    pack = b"\x00\x00\x01\xba" + bytes(10)
    sys_hdr = (b"\x00\x00\x01\xbb" + (9).to_bytes(2, "big") +
               bytes([0xC3, 0x33, 0x67, 0x00, 0x21, 0xFF, 0xE0, 0xE0, 0xE6]))
    return pack + (sys_hdr if first else b"") + pes_mpeg2(pts, payload)


class FakeSock(object):
    def __init__(self):
        self.sent = []

    def sendto(self, data, dst):
        self.sent.append((data, dst))


def new_pusher(ssrc, seq=0):
    p = gb.RtpPusher.__new__(gb.RtpPusher)
    p.cfg = {"video": {"fps": 25}}
    p.ch = {"id": "44010200491320000001", "name": "测试", "mark": "YTDM1"}
    p.dst = ("10.10.10.11", 30060)
    p.ssrc = ssrc
    p.verbose = False
    p.seq = seq
    p._ps_buf = b""
    p._rtp_ts = None
    p._pts_off = 0
    p._cur_pts = None
    p._cur_es = []
    p.pkts_sent = 0
    p.bytes_sent = 0
    p.sock = FakeSock()
    return p


def parse_rtp(data):
    assert data[0] >> 6 == 2
    return {"pt": data[1] & 0x7F, "m": bool(data[1] & 0x80),
            "seq": int.from_bytes(data[2:4], "big"),
            "ts": int.from_bytes(data[4:8], "big"),
            "ssrc": int.from_bytes(data[8:12], "big"),
            "payload": data[12:]}


def group_frames(pkts):
    frames, cur = [], []
    for pkt in pkts:
        cur.append(pkt)
        if pkt["m"]:
            frames.append(cur)
            cur = []
    assert not cur, "存在未以 M 位收尾的尾部包"
    return frames


def frame_ps(frames):
    """每帧的 RTP 载荷拼回完整 PS 帧。"""
    return [b"".join(q["payload"] for q in f) for f in frames]


# ------------------------------------------------------------------ #
# 1. PES 单元解析 (两种布局)
# ------------------------------------------------------------------ #
print("1) _pes_unit_parse: MPEG-2 / MPEG-1 两种 PES 头布局")
pusher = new_pusher(1)
for pts in (0, 1, 3600, 90000, 93600, 123456789, (1 << 32) + 7):
    u = pes_mpeg2(pts, b"\xab" * 40)
    got_pts, got_pl = pusher._pes_unit_parse(u)
    check("MPEG-2 PTS=%d" % pts, got_pts == pts and got_pl == b"\xab" * 40,
          "got pts=%s" % got_pts)
u = pes_mpeg2(90000, b"\xcd" * 30, dts=86400)
check("MPEG-2 PTS+DTS", pusher._pes_unit_parse(u) == (90000, b"\xcd" * 30))
for pts in (None, 0, 45000, 81000):
    u = pes_mpeg1(pts, b"\x99" * 30)
    check("MPEG-1 PTS=%s" % pts, pusher._pes_unit_parse(u) == (pts, b"\x99" * 30))

# ------------------------------------------------------------------ #
# 2. 任意切块 + 重建 GB28181 MPEG-2 PS
# ------------------------------------------------------------------ #
print("2) _flush_packs: 任意切块不丢帧, 重建输出合规")
p = new_pusher(0x0A000001)
stream = b"".join(make_pack(90000 + i * 3600, b"\xab" * 1500, first=(i == 0))
                  for i in range(6))
pos, chunk_sizes = 0, [7, 4093, 1, 8191, 3, 6007, 11, 5000]
for sz in chunk_sizes:
    p._ps_buf += stream[pos:pos + sz]
    p._flush_packs()
    pos += sz
p._ps_buf += stream[pos:]
p._flush_packs(final=True)

pkts = [parse_rtp(d) for d, _ in p.sock.sent]
frames = group_frames(pkts)
ps_frames = frame_ps(frames)
check("切出 6 帧", len(frames) == 6, "实际 %d 帧" % len(frames))
check("帧内时间戳一致", all(len({q["ts"] for q in f}) == 1 for f in frames))
check("时间戳按 3600/fps 递增",
      [f[0]["ts"] for f in frames] == [90000 + i * 3600 for i in range(6)],
      str([f[0]["ts"] for f in frames]))
check("M 位仅帧尾", all(f[-1]["m"] and not any(q["m"] for q in f[:-1]) for f in frames))
check("PT=96 / SSRC 正确",
      all(q["pt"] == 96 and q["ssrc"] == 0x0A000001 for q in pkts))
check("seq 连续递增",
      [q["seq"] for q in pkts] == [i & 0xFFFF for i in range(len(pkts))])

# 重建输出的结构检查
# 帧布局: PACK(14) + SYS(15) + PSM(20) + PES(E0 4 + len 2 + 80 80 05 3 + PTS 5) + ES
P_PSM, P_PES, P_PTS, P_ES = 29, 49, 58, 63
ok_ba = all(f[:4] == b"\x00\x00\x01\xba" and (f[4] >> 4) == 4 for f in ps_frames)
check("重建 pack 头为 MPEG-2 (BA+'0100')", ok_ba)
ok_sys = all(f[14:18] == b"\x00\x00\x01\xbb" for f in ps_frames)
check("每帧带系统头 BB", ok_sys)
ok_psm = all(f[P_PSM:P_PSM + 4] == b"\x00\x00\x01\xbc" for f in ps_frames)
check("每帧带 PSM BC", ok_psm)
psm_ok = (p._PS_PSM[4:6] == (14).to_bytes(2, "big") and
          p._PS_PSM[12:16] == b"\x1b\xe0\x00\x00")
check("PSM 声明 H.264(0x1B)->E0", psm_ok, p._PS_PSM.hex())
ok_pes = all(f[P_PES:P_PES + 4] == b"\x00\x00\x01\xe0" and
             f[P_PES + 6] == 0x80 and f[P_PES + 7] == 0x80 for f in ps_frames)
check("PES 为 E0 + MPEG-2 头 (80 80)", ok_pes)
ok_plen = all(int.from_bytes(f[P_PES + 4:P_PES + 6], "big") == 8 + 1500
              for f in ps_frames)
check("PES 长度字段 = 8+ES", ok_plen)
pts_ok = all(gb.RtpPusher._decode_ts(f[P_PTS:P_PTS + 5]) == fr[0]["ts"]
             for f, fr in zip(ps_frames, frames))
check("PES PTS 与 RTP 时间戳一致", pts_ok)
es_ok = all(f[P_ES:] == b"\xab" * 1500 for f in ps_frames)
check("ES 载荷完整无损", es_ok)

# ------------------------------------------------------------------ #
# 3. PTS 回跳 (stream_loop 重绕)
# ------------------------------------------------------------------ #
print("3) PTS 回跳 (素材循环重绕) 保持严格递增")
p2 = new_pusher(0x0A000002)
seq_pts = [90000 + i * 3600 for i in range(5)] + [90000, 93600]
for i, pts in enumerate(seq_pts):
    p2._ps_buf += make_pack(pts, b"\xcd" * 500, first=(i == 0))
    p2._flush_packs()
p2._flush_packs(final=True)
ts_list = [f[0]["ts"] for f in group_frames([parse_rtp(d) for d, _ in p2.sock.sent])]
check("7 帧时间戳严格递增",
      all(a < b for a, b in zip(ts_list, ts_list[1:])), str(ts_list))

# ------------------------------------------------------------------ #
# 4. 多路并发独立性
# ------------------------------------------------------------------ #
print("4) 多路并发: 两个推流器交错喂流, 状态互不干扰")
pa, pb = new_pusher(0x0A000001, seq=100), new_pusher(0x0A000002, seq=20000)
for i in range(4):
    pa._ps_buf += make_pack(90000 + i * 3600, b"\x11" * 700, first=(i == 0))
    pa._flush_packs()
    pb._ps_buf += make_pack(50000 + i * 3600, b"\x22" * 700, first=(i == 0))
    pb._flush_packs()
pa._flush_packs(final=True)
pb._flush_packs(final=True)

fa = group_frames([parse_rtp(d) for d, _ in pa.sock.sent])
fb = group_frames([parse_rtp(d) for d, _ in pb.sock.sent])
check("两路各 4 帧", len(fa) == 4 and len(fb) == 4)
check("A 路 ssrc", all(q["ssrc"] == 0x0A000001 for f in fa for q in f))
check("B 路 ssrc", all(q["ssrc"] == 0x0A000002 for f in fb for q in f))
check("A 路时间戳独立", [f[0]["ts"] for f in fa] == [90000 + i * 3600 for i in range(4)])
check("B 路时间戳独立", [f[0]["ts"] for f in fb] == [50000 + i * 3600 for i in range(4)])
check("A/B 序列号各自连续",
      [q["seq"] for q in [parse_rtp(d) for d, _ in pa.sock.sent]] ==
      [(100 + i) & 0xFFFF for i in range(len(pa.sock.sent))])

# ------------------------------------------------------------------ #
# 5. 真实 ffmpeg -f mpeg 输出端到端 (MPEG-1 系统流, stream_id E2, 无 PSM)
# ------------------------------------------------------------------ #
FIXTURE = os.path.join(HERE, "test_fixture_ps.mpeg")
if os.path.exists(FIXTURE):
    print("5) 真实 ffmpeg 流 (test_fixture_ps.mpeg) 端到端解析重建")
    raw = open(FIXTURE, "rb").read()
    # 统计输入里带 PTS 的视频 PES 个数 = 期望帧数 (帧边界)
    n_in_frames, i = 0, 0
    while i + 7 < len(raw):
        if raw[i:i + 3] == b"\x00\x00\x01" and 0xE0 <= raw[i + 3] <= 0xEF:
            q = i + 6
            while q < len(raw) and raw[q] == 0xFF:
                q += 1
            if q < len(raw) and (raw[q] >> 4) in (0x2, 0x3):
                n_in_frames += 1
            i = q
        else:
            i += 1
    p5 = new_pusher(0x0A000003)
    pos = 0
    while pos < len(raw):                       # 按 16KB 模拟 os.read 任意切块
        p5._ps_buf += raw[pos:pos + 16384]
        p5._flush_packs()
        pos += 16384
    p5._flush_packs(final=True)
    f5 = group_frames([parse_rtp(d) for d, _ in p5.sock.sent])
    ps5 = frame_ps(f5)
    check("真实流帧数 = 输入带 PTS 的 PES 数 (%d)" % n_in_frames,
          len(f5) == n_in_frames, "实际 %d" % len(f5))
    check("真实流: 重建为 MPEG-2 pack 头",
          all(f[:4] == b"\x00\x00\x01\xba" and (f[4] >> 4) == 4 for f in ps5))
    check("真实流: 每帧 PSM 声明 H.264->E0",
          all(f[29:33] == b"\x00\x00\x01\xbc" and f[41:45] == b"\x1b\xe0\x00\x00"
              for f in ps5), " " if ps5 else "")
    ts5 = [f[0]["ts"] for f in f5]
    check("真实流: 时间戳严格递增且步长合理",
          all(a < b for a, b in zip(ts5, ts5[1:])) and
          all(0 < b - a <= 18000 for a, b in zip(ts5, ts5[1:])),
          str(ts5[:8]))
    # ES 无损: 重建帧的 PES 载荷总量 == 输入视频 PES 载荷总量
    es_out = sum(len(f[63:]) for f in ps5)
    es_in, i = 0, 0
    while i + 7 < len(raw):
        if raw[i:i + 3] == b"\x00\x00\x01" and 0xE0 <= raw[i + 3] <= 0xEF:
            q = i + 6
            while q < len(raw) and raw[q] == 0xFF:
                q += 1
            if q < len(raw) and (raw[q] >> 4) == 0x2:
                q += 5
            elif q < len(raw) and (raw[q] >> 4) == 0x3:
                q += 10
            else:
                q += 1
            j = raw.find(b"\x00\x00\x01", q)
            while j >= 0 and raw[j + 3:j + 4] < b"\xb9":
                j = raw.find(b"\x00\x00\x01", j + 3)
            es_in += (j if j >= 0 else len(raw)) - q
            i = q
        else:
            i += 1
    check("真实流: ES 字节零丢失 (输入 %d / 输出 %d)" % (es_in, es_out),
          es_in == es_out)
else:
    print("5) (跳过: 无 test_fixture_ps.mpeg)")

# ------------------------------------------------------------------ #
# 6. WvpReplayer: token 提取与失败原因
# ------------------------------------------------------------------ #
print("6) WvpReplayer 登录解析 / 补拉失败原因上报")


def new_replayer():
    r = gb.WvpReplayer.__new__(gb.WvpReplayer)
    r.cfg = {"device_id": "44010200491100000001",
             "auto_replay": {"wvp_base": "http://127.0.0.1:18080",
                             "username": "admin", "password": "admin",
                             "interval": 60, "cooldown": 180}}
    r.base = r.cfg["auto_replay"]["wvp_base"]
    r.user = "admin"
    r.password = "admin"
    r.interval = 60
    r.cooldown = 180
    r.token = None
    r.last_try = {}
    r.running = True
    return r


def mock_http(r, login_body, play_body):
    def fake(path, headers=None):
        if "login" in path:
            return 0, login_body
        return 0, play_body
    r._http_get = fake


r = new_replayer()
mock_http(r, _json.dumps({"code": 0, "data": {"access-token": "T1"}}),
          _json.dumps({"code": 0, "data": {}}))
check("旧版 data.access-token 提取", r._login()[0] is True and r.token == "T1")
check("点播成功", r._start_play("ch1")[0] is True)

r = new_replayer()
mock_http(r, _json.dumps({"code": 0, "data": {"token": "T2"}}),
          _json.dumps({"code": 0}))
check("更旧版 data.token 提取", r._login()[0] is True and r.token == "T2")

r = new_replayer()
mock_http(r, _json.dumps({"code": 0, "data": {"accessToken": "T6", "username": "admin"}}),
          _json.dumps({"code": 0}))
check("新版驼峰 data.accessToken 提取", r._login()[0] is True and r.token == "T6")

r = new_replayer()
mock_http(r, _json.dumps({"code": 0, "data": "T3"}), _json.dumps({"code": 0}))
check("字符串 data 提取", r._login()[0] is True and r.token == "T3")

r = new_replayer()
mock_http(r, _json.dumps({"code": 0, "data": {"access-token": "T4"}}),
          _json.dumps({"code": 400, "msg": "通道不存在"}))
r._login()
ok, why = r._start_play("bad")
check("失败原因含 WVP code/msg",
      ok is False and "code=400" in why and "通道不存在" in why, why)

r = new_replayer()
mock_http(r, _json.dumps({"code": 0, "data": {"access-token": "T5"}}), "not-json")
r._login()
ok, why = r._start_play("ch1")
check("非 JSON 响应上报", ok is False and "非 JSON" in why, why)

r = new_replayer()
mock_http(r, _json.dumps({"code": 0, "data": {"foo": 1}}), "")
check("登录无 token 字段 -> 登录失败", r._login()[0] is False)

r = new_replayer()
mock_http(r, _json.dumps({"code": 400, "msg": "用户名或密码错误"}), "")
ok, why = r._login()
check("密码错误原因上报", ok is False and "用户名或密码错误" in why, why)

r = new_replayer()


def fake_err(path, headers=None):
    return -1, "connection refused"
r._http_get = fake_err
ok, why = r._login()
check("连不上原因上报", ok is False and "连不上 WVP" in why, why)

# ------------------------------------------------------------------ #
N_TOTAL = 37 + (11 if os.path.exists(FIXTURE) else 0)
print("=" * 66)
if FAILS:
    print("失败 %d 项: %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("全部 %d 项检查通过" % N_TOTAL)
