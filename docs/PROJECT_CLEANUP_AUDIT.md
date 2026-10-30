# 项目核查报告 — 无用文件清理

> 扫描日期: 2026-10-14
> 工作目录: `D:\散活\smart-transport`
> 扫描范围: 全文件树(排除 `node_modules/`、`.git/` 内部、`__pycache__/`)

---

## 摘要

| 类别 | 数量 | 动作 |
|---|---|---|
| 🗑️ 显而易见的垃圾 | 13 | 直接删 |
| ⚠️ 历史冗余备份 | 1 个目录 + 4 个文件 | 评估后删 |
| 🔍 重复 / 临时备份 | 4 | 评估后删 |
| 🧪 孤立脚本 | 4 个 `zbny-*` + 4 个手动脚本 | 文档化或删 |
| 🧩 可疑但不建议直接删 | 5 处 | 治理提示 |

---

## A. 🗑️ 显而易见的垃圾(直接删, 风险 0)

### A1. macOS .DS_Store(11 处)

Windows / Linux 上完全无用,建议加入 `.gitignore` 并清理:

```
.DS_Store
.git/.DS_Store
.trae/.DS_Store
.trae/documents/.DS_Store
app/.DS_Store
app/backend/.DS_Store
data/.DS_Store
logs/.DS_Store
map-marking-system-vue/.DS_Store
output/.DS_Store
setting-server/.DS_Store
```

### A2. 临时扫描脚本(本次会话遗留)

```
__scan_orphans.py
```

### A3. 完全空目录(只有 .DS_Store 占位)

```
logs/   # 空目录,实际日志走容器内 /app/logs (bind mount ./logs)
output/ # 空目录
```

> 若 `docker-compose.yml` 是 `./logs:/app/logs` bind mount,删本地 logs/ 不会破坏容器内,但生产环境通常建议保留这个目录占位以提醒运维。

---

## B. ⚠️ 历史冗余(已被取代)

### B1. 整个 `setting/` 目录 — 弃用

```
setting/
├── application.yml            # sdp-ip: 192.168.1.28 — 老本机部署 IP
├── application.yml.bak        # application.yml 的更老版本
├── docker-compose.yml         # 老部署编排
├── wvp-zlm-deploy-guide.md    # 老教程,已被 setting-server/WVP-ZLM-*.md 取代
└── conf/                      # ZLM 老配置
```

**当前生效**: `setting-server/`(CPU 服务器) + `setting-server-gpu/`(GPU 服务器)

**唯一潜在引用**: `scripts/start_all.sh` 写死 `SETTING_DIR="$ROOT_DIR/setting-server"` —— 是指向 setting-server/ 没指向 setting/, 没问题。

**对比确认**: setting/application.yml 用的是 `192.168.1.28`(本机), 而 setting-server/ 用的是 `23.45.1.115`(GPU 服务器)。后者已被 start_all_gpu.sh 接管。

### B2. 整个 `setting-server-bak/` 目录 — 纯备份

```
setting-server-bak/
├── application.yml           # 23.45.1.112 (老 CPU 服务器, 已被 .115 取代)
├── application.yml-black     # -black 备份, 与正本几乎一致
├── docker-compose.yml        # 同上
├── docker-compose.yml-black  # 同步备份
├── conf/config.ini           # 旧 ZLM
├── conf/config.ini-black     # 同步备份
├── wvp_bak_0910.sql          # ⚠️ 数据 SQL 备份 (0910 时间戳)
├── WVP-ZLM-涓氬姟鍚姩鏁欑▼.docx   # 47875 字节 (与 setting-server/ 同名同大小 docx)
└── WVP-ZLM-涓氬姟鍚姩鏁欑▼.md    # 19004 字节 (与 setting-server/ 的 md 内容一致)
```

**重复验证**: bak 目录的 docx + md 与 setting-server/ 同目录下的同名文件几乎一模一样 (差异可能是字符编码)，删除 bak 完全无损。

### B3. 根目录 `wvp_zlm.yml`

最老的 "裸用 peizhi" 部署模板, IP 全是 `.112`,已被 `setting-server/` 完全取代。

---

## C. 🔍 重复 / 临时备份

### C1. `.bak` 文件

| 路径 | 备注 |
|------|------|
| `setting/application.yml.bak` | 已含在 B1 整体删除 |

### C2. `*-black` 文件(开发者手动 cp 备份)

| 路径 | 用途 | 备注 |
|------|------|------|
| `setting-server-bak/application.yml-black` | 同正本 | 删 |
| `setting-server-bak/docker-compose.yml-black` | 同正本 | 删 |
| `setting-server-bak/conf/config.ini-black` | ZLM 配置老版本 | 删 |

> `-black` 不是 "blacklist" 而是开发者习惯性 "黑色存档",功能性等同正本,纯冗余。

---

## D. 🧪 scripts/ 下孤立脚本

### D1. `zbny-*` 白盒测试脚本(4 个)

```
scripts/zbny-bench-gpu-infer.py          # GPU 推理压测
scripts/zbny-test-e2e-fast-car.py        # 端到端快速车测
scripts/zbny-test-infer-saturation.py    # 推理饱和度
scripts/zbny-test-track-stitching.py     # 跟踪拼接测试
```

`grep -r "\bzbny-"` 全仓 **0 命中**。文件名 `zbny` 是项目代号 (智博农?), 没有任何 README 说明这些脚本怎用,优先级最低,可直接归档或删除。

### D2. 其他手动维护脚本(可能有用,需补文档)

```
scripts/regen_device_seed.py    # 用 data/xxx.xlsx 重生成 device_geo.json
scripts/replace_device_info.py  # 把 JSON 全量替换进 MySQL device_info 表
scripts/smoke_cpu_batch.py      # CPU 批量推理 smoke
scripts/cut_video.sh            # 视频切片
```

虽然 grep 也是 0 命中,但从 docstring 看是运维手动工具。**建议**: 把这些写进 `docs/SCRIPTS_INDEX.md`,让团队知道怎么用。

### D3. `scripts/` 治理缺口

整个 `scripts/` 22 个文件,没有 INDEX 文档,新人不知道哪些在用。建议后期补一篇 `docs/SCRIPTS_INDEX.md`。

---

## E. 🧩 可疑但**不建议直接删**

### E1. 双包管理锁文件

```
map-marking-system-vue/package-lock.json    # npm
map-marking-system-vue/pnpm-lock.yaml       # pnpm
```

`package.json` 没声明用哪个,**两套锁文件并存**会误导团队。

**决策建议**:
- 如果团队用 pnpm → `rm package-lock.json` + 容器构建改 pnpm
- 如果用 npm → `rm pnpm-lock.yaml`

### E2. `tool/` 目录(独立工具链)

```
tool/
├── Dockerfile           # 0 引用
├── __pycache__/
└── video_processor.py   # 27 KB 离线视频处理器 (被 scripts/anomaly_test.sh 提及但不被运行)
```

**潜在价值**: `tool/video_processor.py` 是离线算法验证工具 (处理视频文件→输出事件),开发阶段很有用。

**决策建议**: 不删,但写进 `docs/SCRIPTS_INDEX.md` 让团队知道。

### E3. 前端 dist 内的设计文档(可能被公网访问)

```
map-marking-system-vue/dist/
├── README.md                  # 包含项目信息
├── FourLaneRoadPlan.html      # 路网规划图
├── tile-preview.html          # 瓦片预览
└── _preview-datong-z15.png    # 大同 z15 预览图
```

**风险**: nginx 部署时这 4 个文件会被挂在根路径下,公网可访问(数据敏感性需评估)。**当前 nginx.conf 没屏蔽这些 URL**。

**决策建议**: 给这些文件加 nginx 拒绝规则或移出 dist/。

### E4. `models/gitattributes.txt` 命名错误

Git LFS 跟踪规则的文件必须叫 `.gitattributes` 才会被识别。

**决策建议**: 改名 `models/.gitattributes`,验证 LFS 跟踪是否生效。

### E5. 测试文件还是正式文件

`anomaly_test.sh`、`cut_video.sh`、`gen_anomaly_video.py` 看似测试工具,内含 "用法" 注释,可能曾经是开发环节但未文档化。**不删**,补说明即可。

---

## 推荐清理顺序(分批执行)

### 第 1 批:0 风险(可立即执行)

```bash
# 1. 删除 .DS_Store
find . -name ".DS_Store" -not -path "*/node_modules/*" -not -path "*/.git/*" -delete

# 2. 删本次会话临时文件
rm __scan_orphans.py
```

### 第 2 批:轻量冗余(确认 SQL 不再需)

```bash
# 3. 删 setting/ 目录(确认 start_all.sh 不再指向它)
rm -rf setting/

# 4. 删 wvp_zlm.yml(根目录)
rm wvp_zlm.yml

# 5. 删 setting-server-bak/(除 SQL)
rm setting-server-bak/application.yml-black
rm setting-server-bak/docker-compose.yml-black
rm setting-server-bak/conf/config.ini-black
rm setting-server-bak/WVP-ZLM-涓氬姟鍚姩鏁欑▼.*
# 注意: 保留 setting-server-bak/wvp_bak_0910.sql 和 application.yml + docker-compose.yml + conf/config.ini 作冷备份
```

### 第 3 批:治理(不删,补说明)

```bash
# 6. 写 docs/SCRIPTS_INDEX.md 列出 scripts/ 用途
# 7. 评估 package-lock.json 或 pnpm-lock.yaml 单一化
# 8. 给前端 dist 的辅助文件加 nginx 拒绝规则
# 9. 改 models/gitattributes.txt 为 .gitattributes(并验证 LFS)
```

---

## 附录:扫描命令记录

```bash
# 1. 找备份文件
glob **/*.bak
glob **/*-black

# 2. 找空目录
find logs/ output/ -type f

# 3. grep 引用追踪
grep -rE "zbny-|regen_device_seed|replace_device_info|smoke_cpu_batch|cut_video.sh" .

# 4. .DS_Store 扫描
find . -name ".DS_Store" -not -path "*/node_modules/*"
```

---

## 关联文档

- `docs/PROJECT_OVERVIEW.md` — 整体架构
- `docs/IP_CONFIG_INDEX.md` — IP 配置索引
- `docs/IP_CHANGE_CHECKLIST.md` — 改 IP 时的清单
- `docs/START_ALL_GPU_FLOW.md` — 启动流程
- `docs/PROJECT_CLEANUP_AUDIT.md` — **本文档**
