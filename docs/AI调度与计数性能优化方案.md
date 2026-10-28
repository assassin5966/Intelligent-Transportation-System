# AI 调度与计数性能优化方案

> **审核提示（2026-10-28）：** 本文保留原方案和原实施记录；其中部分根因、性能预测及验收结论尚无充分证据，当前组批实现还存在过载时事件循环卡死的严重风险。部署前请以[审核及补充修复方案](./视频拉流与越线计数审核及修复方案.md)为准，先完成 P0 修复和验证。

> 范围: `app/ai/`（解码 → 组批推理 → 跟踪 → 越线计数 → 上报）
> 依据: `logs/2026-10-27.log` 现场日志 + 代码走查
> 目标: 32 路 4K 摄像头在单卡（15GB 显存）上稳定达到 **每路 ≥5 有效处理帧/秒**，越线计数可复现

---

## 一、现状与证据

### 1.1 现场现象

同一台设备（`GAKK-0018清远北门魏都大道-卡口193011`）在不同时段的表现：

| 时间 | 在线路数 | 处理fps | 推理 | 均批 | 组批等待 | 事件 |
|---|---|---|---|---|---|---|
| 16:56 | ~12 | 7.97 | 44ms/批 | 5.0 | 118ms | 正常计数（car/truck） |
| 17:08 | 28+ | 0.11 | 591ms/批 | 1.0 | 8595ms | 0，画面无框 |
| 17:34 | 28+ | 0.12 | 591ms/批 | 1.0 | 6795ms | 0 |

同时伴随：
- 358 次 `解码端缩放未生效, 回退原始分辨率 4096x2336`（cv2 对网络流无效）
- `预处理=114.7ms/帧`
- 16:59:51 槽位 16/16 占满后，出现 12 次 `加载 YOLO 模型 (跟踪)`（每路一份模型）

### 1.2 根因归类

| 编号 | 问题 | 位置 |
|---|---|---|
| S1 | 组批按 `frame.shape` 分组，但提前派发阈值取"本卡设备数"，两者矛盾 → 永远只能超时派发单帧 | `gpu_engine._collector` |
| S1b | 背压方向错误：asyncio 队列有上限，executor 队列无上限 → 排队延迟涨到 8~9s，而非丢帧 | `gpu_engine.submit/_dispatch` |
| S2 | 解码输出未统一画布 → 多 shape → 组批碎片化 | `stream._ffmpeg_out_size` |
| S3 | 容量模型把"路数"当槽位，超容量降级 legacy（每路一份模型）→ 单卡必然崩 | `model_pool.acquire` |
| S4 | 解码帧率恒定，无负反馈，过载时大量无效解码 | `stream._decode_max_fps` |
| S5 | 每帧热重载 14 次 `stat()` | `counter._apply_business_rules` |
| S6 | ffmpeg 方案残留成本：32 个软解进程 + rawvideo 管道拷贝 | `stream` |
| S7 | WS 轨迹广播按帧推送，32 路约 200 msg/s | `pipeline._broadcast_tracks_ws` |
| C1 | `min_motion` 为 px/帧，计数口径随解码帧率漂移 | `crossing_detector._angle_filter` |
| C2 | `miss_grace` / `stitch_gap_frames` 为帧量纲，低帧率下语义失真 | `traj_state` |
| C3 | `dt` 截断在 2s，低帧率下时间轴失真 | `counter.process_tracks` |
| C4 | 低帧率无告警，设备"在线但不计数" | `pipeline._report_stats` |
| C5 | outbox 满时丢事件但无计数 | `pipeline._outbox_put` |

---

## 二、方案

### P0-1 组批与背压（`gpu_engine.py`）

1. **提前派发阈值按形状分组计算**：维护 `shape -> {device_id: last_seen}`，派发阈值取该形状的活跃设备数（60s 内出现过），而不是本卡全部设备数。
2. **在途批上限**：`_inflight` 计数 + `queue_max_batches` 上限；达上限后组保留在 `pending`，等批完成再派发，避免 executor 队列无界堆积。
3. **参数**：`INFER_BATCH_TIMEOUT_MS` 40 → 80（6fps 下窗口内可聚 ~15 帧）。

预期：`均批` 由 1.0 提升到 8~20，`组批等待` 由 8000ms 降到 <500ms。

### P0-2 固定输出画布（`stream.py`）

按宽高比归类，统一输出尺寸，使 32 路落入同一 shape 组：

| 源宽高比 | 输出画布（短边=imgsz） |
|---|---|
| 16:9 ±3% | 1706x960 |
| 4:3 ±3% | 1280x960 |
| 其他 | 保持比例，短边 960 |

- 轻微非等比拉伸（≤3%）；坐标还原由 `pipeline._scale_to_stream` 逐轴计算，天然兼容。
- 说明：像素总量与"短边 960"一致，**不减少管道带宽**；收益是 shape 统一后组批才可能真正变大。

### P1-1 超容量不降级 legacy（`model_pool.py` / `service.py`）

- 批量模式下槽位耗尽不再返回 `None` 走 legacy，而是 `HTTP 503 推理容量已满`。
- `_forward_to_ai` 返回成功标志，`_start_ai_pipeline` 透传；启动失败时设备不置 `online`，避免"在线但不计数"。

### P1-2 计数参数帧率解耦

| 参数 | 现在 | 改为 |
|---|---|---|
| `min_motion` | px/帧 | `min_speed = min_motion × 25`（px/s），按 `dt` 判定 |
| `_miss_grace` | 60 帧 | 2.4s（= 60/25） |
| `_stitch_gap_frames` | 12 帧 | 0.48s（= 12/25） |
| `dt` 上限 | 2s | 10s |

`TrajectoryState.apply` 增加 `dt` 入参，由 pipeline 按实际帧间隔传入。`configs/bytetrack.yaml` 的 `track_buffer` 仍为帧量纲（ultralytics 内部），保留为已知限制。

### P2-1 过载时减少无效排队（`pipeline.py`）

引擎暴露 `inflight` / 最近批耗时；pipeline 在连续过载时跳过若干帧提交（立即丢帧而非排队），使延迟有上界、帧保持新鲜。**注意：只省推理与后处理，不省解码**；进一步省 CPU 需拉子码流（见第三节，NVDEC 在 T4 上不可行）。

### P2-2 业务规则热重载降频（`business_rules.py`）

规则缓存 1s（`mtime` 检查降频），把每帧 ~14 次 `stat()` 降到 1 次/秒。

### P2-3 可观测性

- `处理fps < 5` 持续 30s 打 WARNING 并在运行统计中标记 `低帧率`。
- WS 轨迹广播按 5Hz 节流（仍按最新帧推），历史点采样 `[::3]`。
- outbox 丢弃计数并入运行统计。

---

## 三、解码加速选型：子码流 vs NVDEC（Tesla T4 实机）

### 3.1 实机硬件（`nvidia-smi`）

| 项目 | 值 |
|---|---|
| 型号 | Tesla T4（Turing TU104，数据中心卡） |
| 显存 | 15360 MiB（16GB） |
| 功耗 / 温度 | 70W 上限；空闲 42W / 74°C |
| 驱动 / CUDA | 615.71.09 / 13.4 |

### 3.2 关键换算

T4 的 NVDEC 官方口径约 **38 路 1080p30**。按像素折算：

```
4K30 ≈ 4 × 1080p30 像素量
38 / 4 ≈ 9~10 路 4K30   ← 单卡 NVDEC 的理论上限
```

现场需要解 **32 路 4K@25**，约为 NVDEC 能力的 **3 倍**。即使换成带 NVDEC 的 ffmpeg，也只能覆盖 1/3，其余仍需回落 CPU 软解，形成“半数硬件、半数软件”的混合链路，复杂度高且收益有限。

### 3.3 三道前置门槛

| 门槛 | 结论 |
|---|---|
| 并发会话数 | T4 属 Tesla 系列，**没有** GeForce 的 5~8 路限制 |
| 吞吐能力 | **不满足**：32 路 4K 超出 NVDEC 约 3 倍 |
| 镜像支持 | 当前镜像 `apt install ffmpeg`，Debian 官方包通常不带 `nvdec/cuvid`，需自编译或换镜像（离线部署需重打包） |

### 3.4 方案对比

| 方案 | 32 路 4K | 32 路 720p |
|---|---|---|
| CPU 软解（现状） | 需 ~16~32 核，是当前瓶颈 | ~4~6 核，轻松 |
| T4 NVDEC | 上限 ~9~10 路，**不可行** | 完全够用 |
| **子码流 + CPU 软解** | — | **推荐方案** |

32 路 720p@25 的像素总量约 0.7 Gpix/s，是 32 路 4K 的 **1/10**，CPU 解码压力回落到可接受区间；T4 的推理能力（Turing Tensor Core，yolo11n@960 batch 32 约几十毫秒/批）不是瓶颈。

### 3.5 结论

1. **子码流是唯一能真正解开 32 路 4K 的路径**，优先实施；NVDEC 不作为本项目方案。
2. NVDEC 只适合“少数必须保留 4K 主码流的点位”做补充，且需先满足镜像带 cuvid。
3. 现场核查：后端已按 `WVP_STREAM_SUB=true` 请求 `streamType=sub`，但 AI 实际拉到 `4096x2336`，说明**相机/WVP 侧子码流未开启或未绑定**，属现场配置问题，不需改代码。

### 3.6 现场核查命令

```bash
# ① 镜像是否具备 NVDEC 能力
docker exec dt-ai ffmpeg -hide_banner -hwaccels
docker exec dt-ai ffmpeg -hide_banner -decoders | grep -E 'h264_cuvid|hevc_cuvid'

# ② AI 实际拉到的流分辨率（确认是否已走子码流）
docker exec dt-ai ffprobe -v error -select_streams v:0 \
  -show_entries stream=width,height,codec_name,avg_frame_rate \
  -of default=nw=1 "http://zlmediakit/rtp/<stream_id>.live.flv"

# ③ 点播接口是否返回子码流
curl "http://wvp:18080/api/play/start/<gb_dev>/<gb_ch>?streamType=sub" \
  -H "access-token: <token>"
```

---

## 四、验收指标

| 指标 | 目标 |
|---|---|
| 单路处理fps（32 路在线） | ≥ 5 |
| 均批 | ≥ 8 |
| 组批等待 | < 500ms |
| 预处理 | ≤ 3ms/帧（ffmpeg 直出） |
| 日志告警 | 无 `降级为 legacy 独立实例` |
| 计数 | 车辆过线稳定产出 `VehicleEnter/Exit` |

## 五、回滚

- `INFER_MODE=legacy`：完全回到改动前的每路独立模型路径。
- `DECODE_BACKEND=opencv`：解码回到 cv2 旧路径。
- 计数参数保留原键名与默认值，换算在代码内完成，配置无需迁移。

## 六、实施状态

| 项 | 状态 | 落点 |
|---|---|---|
| P0-1 组批与背压 | ✅ 已实施 | `gpu_engine._collector/_flush_ready/_note_shape`；`INFER_BATCH_TIMEOUT_MS` 40→80 |
| P0-2 固定画布 | ✅ 已实施 | `stream._ffmpeg_out_size`（16:9→1706x960，4:3→1280x960） |
| P1-1 超容量不降级 | ✅ 已实施 | `model_pool.acquire` + `service.register` 503 + `_forward_to_ai` 回传成功 |
| P1-2 帧率解耦 | ✅ 已实施 | `counter`(min_speed/dt 上限 10s)、`traj_state`(时间量纲)、`tracker`/`pipeline` 传 dt |
| P2-1 提交节流 | ✅ 已实施 | `pipeline` 等待 EMA > 1s 时跳 1 帧，运行统计输出 `降载丢帧` |
| P2-2 规则缓存 | ✅ 已实施 | `business_rules` 1s 检查一次 mtime |
| P2-3 可观测性 | ✅ 已实施 | 低帧率 WARNING、WS 5Hz 节流、`outbox丢弃` 计数 |

### 未解决 / 未验证

| 项 | 说明 |
|---|---|
| 真实环境验证 | 上述改动仅完成本地编译与合成用例验证，尚未部署；`均批`/`处理fps`/`组批等待` 需上线后按第四节复核 |
| 子码流未生效 | 后端已按 `WVP_STREAM_SUB=true` 请求 `streamType=sub`，AI 实际仍拉到 `4096x2336`，属相机/WVP 侧配置问题（见第三节） |
| 解码 CPU 成本 | P0-P2 未减少解码像素量，32 路 4K 的软解开销仍在（见第七节） |
| bytetrack `track_buffer` | 仍为帧量纲（ultralytics 内部机制），低帧率下窗口语义偏差未完全消除 |

### 验证（本地，无需 GPU/ffmpeg）

| 用例 | 结果 |
|---|---|
| 32 路同形状同批提交 | 1 批 31 帧 + 1 批 1 帧，在途峰值 1（上限 2） |
| 同一运动 25/12/8/6/3/2fps 计数 | 均产出 1 条 `VehicleEnter`，方向一致 |
| 慢速目标（41px/s < 50px/s） | 各帧率一致不计数 |
| 画布归一 | 4096x2336 / 1920x1080 / 3840x2160 / 2560x1440 → 均 1706x960；1440x1080 → 1280x960 |
| 解码读帧 | 子进程异常退出 → `failed=True`；`close()` 正常 TERM 收尾 |

### 上线后按第四节指标核对

关键日志：
- `槽位自适应: ceil(32/1) = 32 槽/引擎`，且不再出现 `降级为 legacy 独立实例`
- `[ffmpeg] 解码就绪: 4096x2336 -> 1706x960 @6fps (非等比拉伸 1.753->1.777)`
- 运行统计中 `均批` ≥ 8、`组批等待` < 500ms、`预处理` ≤ 3ms/帧
- 若 `处理fps < 5` 会出现 `有效帧率过低` WARNING

---

## 七、当前性能瓶颈

P0-P2 解决的是「GPU 侧排队与空转」和「计数口径随帧率漂移」，**没有减少解码像素量**。因此瓶颈位置发生了转移。

### 7.1 瓶颈排序

| 优先级 | 环节 | 现状 | 判断依据 |
|---|---|---|---|
| 1 | **CPU 解码** | 32 路 × 4096×2336@25 全软解，需 16~64 核 | 单路 4K H.264 ≈ 0.5~1 核、H.265 ≈ 1~2 核；另有 rawvideo 管道 32×4.9MB×6fps ≈ 940MB/s 拷贝 |
| 2 | GPU 推理 | 不再是瓶颈 | T4 空闲 9% 利用率、337MiB；修完槽位与组批后 yolo11n@960 batch 32 预计 100~300ms/批，等效 3~8fps/路 |
| 3 | WS 广播 / 后端 | 可忽略 | 已节流到 5Hz × 32 路 ≈ 160 msg/s；outbox、心跳、Redis 量级很小 |

```
解码 CPU  ████████████████████  ← 瓶颈
GPU 推理  ████
其他      █
```

### 7.2 结论

- 之前 600ms/批 的根因是 12 份 legacy 模型抢卡，该路径已由 P1-1 关闭，**GPU 不再是主要瓶颈**。
- **在子码流开通前，即使 P0-P2 全部生效，32 路 4K 也可能达不到“每路 ≥5fps”**——瓶颈从 GPU 排队转移到 CPU 解码。这是预期内结果，也是第三节把子码流列为首选的原因。
- 子码流把 32 路 4K 降到 32 路 720p 后，解码像素总量约为原来的 1/10（≈0.7 Gpix/s），CPU 回落到 4~6 核量级，此时 GPU 推理与计数链路才有意义。

### 7.3 部署后的判断方法

```bash
# ① CPU 是否被解码吃满（接近 核数×100% 即解码饱和）
docker stats dt-ai --no-stream
top -H -p $(pgrep -f "uvicorn app.ai") | head

# ② 单路有效处理帧率是否达标
grep "运行统计" logs/<date>.log | tail -20
# 关注 处理fps / 采样帧 / 丢帧 三个口径

# ③ 是否真的走了子码流
docker exec dt-ai ffprobe -v error -select_streams v:0 \
  -show_entries stream=width,height,codec_name,avg_frame_rate \
  -of default=nw=1 "http://zlmediakit/rtp/<stream_id>.live.flv"
```

判定逻辑：

| 现象 | 结论 |
|---|---|
| CPU 打满 + `处理fps < 5` | 纯解码瓶颈，只能通过子码流解决 |
| CPU 未满 + `处理fps < 5` + `组批等待` 高 | 回到 GPU 组批参数继续调 |
| CPU 未满 + `处理fps ≥ 5` | 达到预期，进入计数准确性观察 |
