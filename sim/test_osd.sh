#!/bin/sh
# 验证容器内 ffmpeg 能否用道路监控素材 + 中文 OSD 生成 PS 流
# WC: 承载 ffmpeg 的容器名 (旧部署 wvp-server / 新部署 wvp-upper-wvp)
set -e
WC="${WVP_C:-wvp-upper-wvp}"
FONT=/opt/sim-fonts/wqy-zenhei.ttc
FILT="scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720,fps=25,drawtext=fontfile=${FONT}:text='YTDM1 永泰东门1口':fontcolor=white:fontsize=34:box=1:boxcolor=black@0.45:x=20:y=18,drawtext=fontfile=${FONT}:text='%{localtime}':fontcolor=white:fontsize=30:box=1:boxcolor=black@0.45:x=w-tw-20:y=18"
echo "$FILT" > /home/sim/media/_filter_test.txt
echo "=== 挂载检查 (容器: $WC) ==="
docker exec $WC ls -la /opt/sim-media/ /opt/sim-fonts/
echo "=== 生成测试流 ==="
docker exec $WC sh -c "ffmpeg -hide_banner -loglevel error -re -stream_loop -1 -i /opt/sim-media/cam1.mp4 -t 4 -vf \"\$(cat /opt/sim-media/_filter_test.txt)\" -c:v libx264 -preset ultrafast -tune zerolatency -profile:v baseline -pix_fmt yuv420p -g 50 -b:v 2500k -an -f mpeg /tmp/osd_test.ps && ls -la /tmp/osd_test.ps && echo OSD_OK"
echo "=== 抽一帧看效果 ==="
docker exec $WC ffmpeg -y -hide_banner -loglevel error -i /tmp/osd_test.ps -frames:v 1 -q:v 2 /tmp/osd_frame.jpg
docker cp $WC:/tmp/osd_frame.jpg /home/sim/osd_frame.jpg
ls -la /home/sim/osd_frame.jpg
