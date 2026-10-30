#!/bin/sh
# GB28181 模拟器启停控制（PID 文件方式，避免 pkill 误伤）
# DIR 自动取本脚本所在目录 (服务器上通常为 /home/sim)
DIR=$(cd "$(dirname "$0")" && pwd)
PIDF=$DIR/sim.pid
LOG=$DIR/sim.log

start() {
  if [ -f "$PIDF" ] && kill -0 "$(cat $PIDF)" 2>/dev/null; then
    echo "already running pid=$(cat $PIDF)"; return 0
  fi
  cd "$DIR" || exit 1
  if [ -f sim.json ]; then CONF="--config sim.json"; else CONF=""; fi
  nohup python3 gb28181_device.py $CONF --verbose > "$LOG" 2>&1 < /dev/null &
  echo $! > "$PIDF"
  sleep 2
  echo "started pid=$(cat $PIDF)"
}

# 持续推流模式: 通道无流时自动调 WVP 点播接口补拉 (适合长时间测试)
keep() {
  if [ -f "$PIDF" ] && kill -0 "$(cat $PIDF)" 2>/dev/null; then
    echo "already running pid=$(cat $PIDF) (先 stop 再 keep 可切换模式)"; return 0
  fi
  cd "$DIR" || exit 1
  if [ -f sim.json ]; then CONF="--config sim.json"; else CONF=""; fi
  nohup python3 gb28181_device.py $CONF --verbose --keep > "$LOG" 2>&1 < /dev/null &
  echo $! > "$PIDF"
  sleep 2
  echo "started (keep 模式) pid=$(cat $PIDF)"
}

stop() {
  if [ -f "$PIDF" ]; then
    kill "$(cat $PIDF)" 2>/dev/null
    rm -f "$PIDF"
    echo "stopped"
  else
    echo "no pidfile"
  fi
  # 兜底：清理容器内残留的推流 ffmpeg (兼容新旧容器名, 只处理运行中的)
  # 按输入源匹配 (sim-media / testsrc2), 无字体时 drawtext 不存在也能清掉
  # 注意: 设备名里含 ( ) 等字符, 不要把它们拼进 pkill 模式 (pkill -f 走 ERE,
  # 元字符会失配, 历史孤儿进程就是这样泄漏的); 这里只用不含元字符的固定串
  for c in $(docker ps --format '{{.Names}}' 2>/dev/null); do
    case "$c" in
      wvp-server|wvp-upper-wvp|wvp|sim-*)
        docker exec "$c" pkill -f 'stream_loop -1 -i' 2>/dev/null
        docker exec "$c" pkill -f sim-media 2>/dev/null
        docker exec "$c" pkill -f testsrc2 2>/dev/null
        for m in YTDM1 YTDM2 YTDM3 YTDM4; do
          docker exec "$c" pkill -f "drawtext=text='$m'" 2>/dev/null
        done
        ;;
    esac
  done
}

status() {
  if [ -f "$PIDF" ] && kill -0 "$(cat $PIDF)" 2>/dev/null; then
    echo "running pid=$(cat $PIDF)"
  else
    echo "not running"
  fi
}

check() {
  cd "$DIR" || exit 1
  if [ -f sim.json ]; then
    python3 gb28181_device.py --config sim.json --check
  else
    python3 gb28181_device.py --check
  fi
}

case "$1" in
  start) start ;;
  keep) keep ;;
  stop) stop ;;
  restart) stop; sleep 1; start ;;
  rekeep) stop; sleep 1; keep ;;
  status) status ;;
  check) check ;;
  log) tail -n "${2:-40}" "$LOG" ;;
  *) echo "usage: $0 {start|keep|stop|restart|rekeep|status|check|log [n]}"
     echo "  start   普通启动 (平台点播才推流)"
     echo "  keep    持续推流模式 (通道无流时自动调 WVP 点播补拉, 适合测试)" ;;
esac
