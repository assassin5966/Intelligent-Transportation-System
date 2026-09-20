# 计数兼容修复 - 现场替换操作说明

> 适用: 场地为昨天之前的旧代码, 同事描述"发文件合并就行, 镜像会自己挂载"
> (docker-compose.yml 中 ./app、./configs 均为 bind mount, 替换宿主机文件后重启容器即生效, 无需重建镜像)

## 一、需要传到现场的文件清单 (共 7 个)

### 代码文件 (6 个, 覆盖旧文件)

| # | 文件 | 作用 |
|---|------|------|
| 1 | `app/ai/counter.py` | 漏计防护核心: 轨迹消失落定 + 确认超时落定 |
| 2 | `app/ai/track_state_manager.py` | 配套状态缓存 (轨迹活跃时间/类别/置信度) |
| 3 | `app/backend/api/stats.py` | 新增存量清零接口 POST /api/stats/reset |
| 4 | `app/backend/core/realtime.py` | 清零逻辑补全 (全局+逐设备, 流量账保留) |
| 5 | `app/backend/api/config_rules.py` | 配置页面支持 2 个新参数的元数据 |
| 6 | `app/backend/core/wvp_client.py` | (可选) 与远程 main 对齐, 仅注释差异 |

### 配置文件 (1 个)

| # | 文件 | 作用 |
|---|------|------|
| 7 | `configs/business_rules.yaml` | 新增 2 个漏计防护参数 (热重载) |

> 注意: 前提是场地代码已是 main 分支 `024f2ff` (含 ZLM 断流修复/onLine 兼容等 23 个提交)。
> 若场地比 `024f2ff` 更旧, 不能只发这 7 个文件, 应先让现场 `git pull` 到 024f2ff。
> 判断方法: 现场 `git log --oneline -1` 是否为 `024f2ff`。

## 二、打包 (本地开发机执行)

```powershell
cd c:\workspace\智慧交通项目
tar -czf counter-fix-20260918.tar.gz `
  app/ai/counter.py `
  app/ai/track_state_manager.py `
  app/backend/api/stats.py `
  app/backend/core/realtime.py `
  app/backend/api/config_rules.py `
  app/backend/core/wvp_client.py `
  configs/business_rules.yaml
# 传到现场 (scp / U盘均可), 例如:
# scp counter-fix-20260918.tar.gz user@23.45.1.112:/tmp/
```

## 三、现场替换操作 (场地服务器执行)

```bash
# 0. 前置检查: 确认代码版本 >= 024f2ff
cd /path/to/DT          # 现场项目目录 (有 docker-compose.yml 的目录)
git log --oneline -1

# 1. 备份旧文件 (出问题可回滚)
mkdir -p /tmp/backup-$(date +%m%d)
cp app/ai/counter.py app/ai/track_state_manager.py \
   app/backend/api/stats.py app/backend/core/realtime.py \
   app/backend/api/config_rules.py app/backend/core/wvp_client.py \
   configs/business_rules.yaml /tmp/backup-$(date +%m%d)/ 2>/dev/null

# 2. 解包覆盖 (tar 包内相对路径与仓库一致, 直接在项目根目录解)
tar -xzf /tmp/counter-fix-20260918.tar.gz -C .

# 3. 重启容器 (bind mount 生效, 无需重建镜像)
docker compose restart ai backend

# 4. 确认起来且无报错
docker compose ps
docker compose logs ai backend --tail=50 | grep -iE "error|启动|漏计|管道"
```

## 四、替换后验证 (3 分钟)

```bash
# 1. 新参数已生效 (AI 日志无 import 报错即代码加载成功)
docker compose logs ai --tail=30 | grep -E "管道启动|计数线"

# 2. 清零接口存在 (返回 stats JSON 而不是 404)
curl -s -X POST http://127.0.0.1:8000/api/stats/reset

# 3. 配置页可看到新参数 (浏览器打开 /static/business-rules.html
#    "越线计数" 组应出现"确认超时落定(秒)"和"轨迹消失落定"两项)

# 4. 观察计数: 让车辆/行人过线 10 次, 对比事件数
curl -s "http://127.0.0.1:8000/api/events?limit=50" | tail -20
```

## 五、回滚 (若出现多计/异常)

```bash
cd /path/to/DT
cp /tmp/backup-$(date +%m%d)/* . 2>/dev/null
# 注意: tar 包内文件分属不同目录, 回滚按原路径拷回:
cp /tmp/backup-$(date +%m%d)/counter.py app/ai/
cp /tmp/backup-$(date +%m%d)/track_state_manager.py app/ai/
cp /tmp/backup-$(date +%m%d)/stats.py app/backend/api/
cp /tmp/backup-$(date +%m%d)/realtime.py app/backend/core/
cp /tmp/backup-$(date +%m%d)/config_rules.py app/backend/api/
cp /tmp/backup-$(date +%m%d)/wvp_client.py app/backend/core/
cp /tmp/backup-$(date +%m%d)/business_rules.yaml configs/
docker compose restart ai backend
```

## 六、行为变化说明 (给现场同事)

- **预期变化**: 画面边缘快速出画的目标现在会计入 (此前跨线后出画=永久丢弃, 是计数偏少主因之一); 跨线后 3 秒未完成确认的轨迹直接计
- **可调开关**: 运维页 `/static/business-rules.html` → 越线计数:
  - 多计了 → "轨迹消失落定" 改为 false (关闭出画落定)
  - 仍觉偏少 → "确认超时落定(秒)" 调大到 5
  - 两个参数改完即时生效, 无需重启
- **存量校准**: 之前 Exit 漏检积累的虚高存量, 用 `POST /api/stats/reset` 校零
  (只清"当前在场", 今日累计/小时累计等流量账不动)
