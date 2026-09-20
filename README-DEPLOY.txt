计数兼容修复包 - 现场执行说明
=====================================

一、这个包是什么
----------------
修复"计数偏少"的兼容补丁:
  1. 轨迹消失落定: 目标跨线后快速出画/被遮挡, 直接计入 (旧版会永久丢弃)
  2. 确认超时落定: 跨线后 3 秒未完成确认, 直接计入 (防低帧率漏计)
  3. 新增存量清零接口: POST /api/stats/reset (校准虚高的"当前在场"数)
  4. 两个新参数支持运维页热重载, 出现多计可即时关闭, 无需重启

前提: 现场代码需已是 main 分支 024f2ff 或更新 (含 ZLM 断流修复)。
验证: 在项目根目录执行 git log --oneline -1, 首行应是 024f2ff。
      若比这更旧, 请先 git pull origin main 再继续。

二、执行步骤 (拷到服务器后, 在项目根目录 = 有 docker-compose.yml 的目录执行)
----------------
# 1. 备份被覆盖的旧文件 (用于回滚)
mkdir -p /tmp/backup-0918
cp app/ai/counter.py app/ai/track_state_manager.py \
   app/backend/api/stats.py app/backend/core/realtime.py \
   app/backend/api/config_rules.py app/backend/core/wvp_client.py \
   configs/business_rules.yaml /tmp/backup-0918/

# 2. 解包覆盖 (tar 内是相对路径, 必须解到项目根目录)
tar -xzf /path/to/counter-fix-20260918.tar.gz -C .
# /path/to 换成你放包的目录, 例如 /tmp

# 3. 重启容器 (app/configs 是 bind mount, 重启即生效, 无需重建镜像)
docker compose restart ai backend

# 4. 确认正常
docker compose ps                                  # ai/backend 应为 Up
docker compose logs ai --tail=30 | grep 管道        # 无 import 报错
curl -s -X POST http://127.0.0.1:8000/api/stats/reset   # 返回 stats JSON = 新接口已生效

三、替换后验证 (约 10 分钟)
----------------
# 1. 浏览器打开运维页 (backend 地址):
#    http://<服务器IP>:8000/static/business-rules.html
#    "越线计数"组应多出两项: "确认超时落定(秒)"=3.0 和 "轨迹消失落定"=true

# 2. 计数对拍: 看着摄像头让人/车过线 10 次, 对比:
curl -s "http://127.0.0.1:8000/api/events?limit=50" | grep -o CAM串 | tail -20

# 3. 存量虚高校准 (此前 Exit 漏检累积的虚数):
curl -s -X POST http://127.0.0.1:8000/api/stats/reset
#    只清"当前在场", 今日累计/小时累计等流量账不动
#    也可只清某台: curl -X POST "http://127.0.0.1:8000/api/stats/reset?device_id=GB-xxx-xxx"

四、调参 (出现多计时的应急开关, 改完即时生效)
----------------
运维页 /static/business-rules.html -> 越线计数:
  - 多计了   -> "轨迹消失落定" 改 false (关掉出画落定)
  - 仍偏少   -> "确认超时落定(秒)" 调大到 5
  - 均无需重启

五、回滚
----------------
cd /path/to/DT
cp /tmp/backup-0918/counter.py app/ai/
cp /tmp/backup-0918/track_state_manager.py app/ai/
cp /tmp/backup-0918/stats.py app/backend/api/
cp /tmp/backup-0918/realtime.py app/backend/core/
cp /tmp/backup-0918/config_rules.py app/backend/api/
cp /tmp/backup-0918/wvp_client.py app/backend/core/
cp /tmp/backup-0918/business_rules.yaml configs/
docker compose restart ai backend
