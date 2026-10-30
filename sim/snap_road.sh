#!/bin/sh
# 从 ZLM 抓取 4 路道路监控流的实时画面
# WC: 承载 ffmpeg 的容器名 (旧部署 wvp-server / 新部署 wvp-upper-wvp)
set -e
WC="${WVP_C:-wvp-upper-wvp}"
DEV=44010200491100000001
for i in 1 2 3 4; do
  docker exec $WC sh -c "ffmpeg -y -loglevel error -rtsp_transport tcp -i rtsp://127.0.0.1:554/rtp/${DEV}_4401020049132000000${i} -frames:v 1 -q:v 2 /tmp/snap${i}.jpg"
  docker cp $WC:/tmp/snap${i}.jpg /home/sim/road_snap${i}.jpg
  echo "road_snap${i}.jpg done"
done
ls -la /home/sim/road_snap*.jpg
