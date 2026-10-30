#!/bin/sh
# WVP-PRO 接口调用辅助（登录 / 查设备 / 点播）
# 安全约定：输出里不出现凭据与令牌明文，只回显状态。
BASE=http://127.0.0.1:18080
U=admin
PLAIN=admin
MD5=$(printf '%s' "$PLAIN" | md5sum | awk '{print $1}')
TOKENFILE=/home/sim/token.txt

extract() {   # 从登录响应里稳健地取出 token（兼容 data 为字符串 / 对象两种形态）
  python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
except Exception:
    sys.exit(0)
v = d.get('data')
if isinstance(v, str):
    print(v)
elif isinstance(v, dict):
    for k in ('token', 'accessToken', 'access_token'):
        if isinstance(v.get(k), str):
            print(v[k]); break
"
}

try() {   # $1=待发送值  $2=标签
  R=$(curl -s --max-time 10 "$BASE/api/user/login?username=$U&password=$1")
  CODE=$(echo "$R" | sed -n 's/.*"code":\([-0-9]*\).*/\1/p')
  T=$(printf '%s' "$R" | extract)
  echo "[$2] code=$CODE tokenLen=${#T}"
  if [ -n "$T" ]; then echo "$T" > "$TOKENFILE"; fi
}

login() {
  rm -f "$TOKENFILE"
  try "$MD5" md5
  [ -s "$TOKENFILE" ] || try "$PLAIN" plain
  if [ -s "$TOKENFILE" ]; then echo "LOGIN_OK"; else echo "LOGIN_FAILED"; fi
}

TOKEN=$(cat "$TOKENFILE" 2>/dev/null)

case "$1" in
  login)
    login
    ;;
  devices)
    curl -s --max-time 15 "$BASE/api/device/query/devices?page=1&count=50" -H "access-token: $TOKEN"; echo
    ;;
  channels)
    curl -s --max-time 15 "$BASE/api/device/query/devices/$2/channels?page=1&count=50" -H "access-token: $TOKEN"; echo
    ;;
  sync)
    curl -s --max-time 20 "$BASE/api/device/query/devices/$2/sync" -H "access-token: $TOKEN"; echo
    ;;
  play)
    curl -s --max-time 30 "$BASE/api/play/start/$2/$3" -H "access-token: $TOKEN"; echo
    ;;
  stop)
    curl -s --max-time 30 "$BASE/api/play/stop/$2/$3" -H "access-token: $TOKEN"; echo
    ;;
  allstop)
    for CH in 44010200491320000001 44010200491320000002 44010200491320000003 44010200491320000004; do
      printf '%s -> ' "$CH"
      curl -s --max-time 30 "$BASE/api/play/stop/$2/$CH" -H "access-token: $TOKEN" \
        | sed -n 's/.*"code":\([-0-9]*\),"msg":"\([^"]*\)".*/code=\1 msg=\2/p'
      sleep 1
    done
    ;;
  allplay)
    for CH in 44010200491320000001 44010200491320000002 44010200491320000003 44010200491320000004; do
      printf '%s -> ' "$CH"
      curl -s --max-time 30 "$BASE/api/play/start/$2/$CH" -H "access-token: $TOKEN" \
        | sed -n 's/.*"code":\([-0-9]*\),"msg":"\([^"]*\)".*/code=\1 msg=\2/p'
      sleep 1
    done
    ;;
  probe)
    # 端到端验证：从 ZLM 拉 RTSP 解码，看编码/分辨率
    # WC: 承载 ffmpeg/ffprobe 的容器名 (旧部署 wvp-server / 新部署 wvp-upper-wvp)
    WC="${WVP_C:-wvp-upper-wvp}"
    docker exec $WC timeout 15 ffprobe -v error -rtsp_transport tcp \
      -i "rtsp://127.0.0.1:554/rtp/$2" \
      -show_entries stream=codec_name,width,height,avg_frame_rate \
      -of default=nw=1 2>&1 | head -12
    ;;
  streams)
    # 直接问 ZLM 当前有哪些流以及码率
    curl -s --max-time 10 'http://127.0.0.1:90/index/api/getMediaList?secret=OHvo86N9Ww6V8mHPWMisxNgkb8dvqAV420241107' \
      | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception as e:
    print('parse fail:', e); sys.exit()
for m in d.get('data', []):
    print('%-55s codec=%-5s %sx%s  reader=%s  bytesSpeed=%s  alive=%ss' % (
        m.get('app','')+'/'+m.get('stream',''), m.get('videoCodec'),
        m.get('width'), m.get('height'), m.get('readerCount'),
        m.get('bytesSpeed'), m.get('aliveSecond')))
print('共 %d 路流' % len(d.get('data', [])))
"
    ;;
  *)
    echo "usage: $0 {login|devices|channels <devId>|sync <devId>|play <devId> <chId>|allplay <devId>|stop <devId> <chId>}"
    ;;
esac
