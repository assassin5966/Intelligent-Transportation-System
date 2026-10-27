# device-config.html 划线流程审查与离线优化方案

> 约束：项目最终运行在**无外网服务器**上，所有资源必须留在服务器/内网。
> 范围：`static/device-config.html` 的「划线流程」——选设备 → 截帧底图 → 画计数线+锚点(+可选 ROI) → 启流计数(+可选预览)。

---

## 0. 结论先行

**划线流程当前已具备离线运行条件**，无 CDN / 外网脚本依赖。`Dockerfile` 第 59 行（gpu 版 57 行）`COPY static /app/static` 已将整个前端（含 `vendor/` 播放库）打包进镜像。

真正能优化、且契合「资源全在服务器」目标的有三点：
1. 截帧每次都走 **WVP play/start 回环**（3–8 秒，WVP 不可用时直接失败）→ 用 Redis 缓存截帧结果化解。
2. 页面引用了 `config.js`，但 `static/` 下**没有这个文件**（悬空 404，目前靠 `window.location.origin` 兜底）→ 补齐或移除。
3. **划线全靠手动点选**，古城大量相似机位下工作量巨大且易不一致 → 引入**模板式自动划线**（归一化坐标一套模板套用同场景全部设备，离线存储于服务器）。

---

## 1. 划线流程资源清单与离线状态

| 资源 | 来源 | 离线状态 |
|---|---|---|
| 页面 HTML / CSS | 内联 `<style>`，无外链 CSS/字体/图标库 | ✅ |
| 字体 | `system-ui`，浏览器内置 | ✅ |
| `config.js` | `<script src="config.js">` 引用，但文件缺失 | ⚠️ 404，已兜底 origin（建议处理） |
| `flv.min.js` / `hls.min.js` | `/static/vendor/` 本地文件 | ✅（须随镜像打包，已满足） |
| 截帧底图 | 后端 `ffmpeg`/`cv2` 拉流生成 JPEG | ✅ 全服务端 |
| 预览播放 | 本地 `vendor` 库 + 后端 `play_url`（内网 ZLM） | ✅ |
| 画线坐标 | 归一化相对坐标（×画布宽高） | ✅ 分辨率无关，已做好 |

**外部依赖核查**：对全部 `static/*.html` 做 `http(s)://` 扫描，仅命中 `http://localhost:8000`（输入框默认值，非网络拉取）与本地 `vendor` 路径，**无任何 CDN / googleapis / unpkg / jsdelivr / 字体库引用**。

---

## 2. 已发现的问题

1. **`config.js` 悬空引用**：`device-config.html:103` 引用 `config.js`，仓库 `static/` 下不存在（仅有 business-rules.html、device-config.html、device-info.html、vendor/）。加载时 404，代码 `baseUrl = (window.OPS_CONFIG?.API_BASE || window.location.origin)` 兜底为同源，**功能不受影响但控制台报错、且 `OPS_CONFIG` 永远为空**。
2. **截帧强依赖 WVP 实时点播**（`devices.py:384-403`）：WVP 设备走 `wvp.start_play → select_stream_url → ffmpeg/cv2 截帧 → (未启流时) stop_play`，单台 3–8 秒；WVP 不可用或流未就绪即失败。手动注册设备走 `stream_url` 直拉，同样每次新建拉流。
3. **无截帧缓存**：同一设备反复切换/重选会重复触发拉流，浪费且缓慢。
4. **划线全靠手动点选**：每台设备需人工点 2 点+锚点(+ROI 多边形)，古城大量相似机位下工作量大、易不一致，且无法「上线即计数」。

---

## 3. 优化方案（全部资源服务器内，无新增外网依赖）

### P0 — 静态资源交付确认（必做，低风险）
- **`config.js` 处理**（二选一）：
  - *补齐*：新增 `static/config.js`，写入 `window.OPS_CONFIG = { API_BASE: "" }`（留空即同源），消除 404 并保留扩展位；或
  - *移除*：删掉 `device-config.html:103` 的 `<script src="config.js">` 标签。
- **vendor 库打包确认**：确认镜像构建步骤**不会**把 `flv.min.js`/`hls.min.js` 替换为 CDN 链接（当前为本地文件，已安全）。

### P1 — 截帧解耦与缓存（核心优化）
- **方案 A（推荐，低风险）**：截帧结果按 `device_id` 写入 **Redis**（devices API 已持有 `get_redis()` 句柄，无需新依赖），TTL 60–120 秒。`selectDevice` 先查缓存命中即秒回；未命中再走现有 ffmpeg/cv2 截帧并写回。完全服务端、无新外部依赖，重复选同一设备从 3–8s 降到 <50ms。
- **方案 B（进阶，二期）**：复用 AI 推理管道已缓冲的最新帧（`app/ai/stream.py` 的 `FrameReader.snapshot()` 持有最新 video 帧，但当前 `model_pool.snapshot()` 只暴露推理元数据，未对 devices API 开放）。适用于**已启流**设备，零拉流延迟；未启流设备回退方案 A。需把「最新帧」经 AI service 暴露给 devices API，工作量中等。

### P2 — 模板式自动划线（批量自动配置，核心新增）
> 目标：划线坐标已归一化（相对 0–1），**一台参考摄像机的模板可直接套用到所有同机位/同场景设备**，无需逐台手动点选。契合「无外网 + 古城大量相似机位」的批量部署。

- **模板数据模型**（归一化，分辨率无关）：
  ```json
  {
    "id": "yt-east-gate-vehicle",
    "name": "永泰东门 车辆卡口",
    "match": { "by": "name_prefix", "value": "永泰东门" },
    "camera_type": "vehicle",
    "count_only": "",
    "line_coords":   "0.10,0.62,0.90,0.62",
    "anchor_coords": "0.50,0.82",
    "roi_coords":    "0.05,0.40,0.95,0.40,0.95,0.95,0.05,0.95",
    "max_vehicles": 20, "max_persons": null
  }
  ```
- **存储（离线保证）**：模板存为 `configs/line_templates.json`，随镜像 `COPY configs /app/configs` 打入，无联网、可版本化、无需新增 DB 表。后端启动时加载，支持热重载。
- **匹配规则（由精确到模糊，命中即止）**：
  1. 设备显式绑定 `tpl_id`；
  2. `match.by` = `name_prefix`（设备名前缀，与现有 device_info 短名匹配一致）/ `group` / `camera_type`；
  3. 兜底：按 `camera_type` 的默认模板（内置 vehicle / person 各一套）。
- **自动套用触发点**：
  - 设备注册/同步（`wvp_sync` 或手动注册）后，若无已配线且开启「自动套用」，直接以 模板坐标×设备分辨率 调现有 `/enable` 逻辑启流；
  - 前端「套用模板」按钮：选中设备后一键把匹配模板画到 canvas（不点鼠标），用户可微调后再提交；
  - 「批量自动配置」：对全部未配线设备按匹配规则一次性套用。
- **后端改动（复用现有 `/enable`）**：新增 `GET/POST/PUT/DELETE /api/line-templates` 管理接口；`POST /api/devices/{id}/apply-template/{tpl_id}` 与 `matched-template` 预览接口；归一化→绝对坐标换算（×设备流分辨率）注入既有 enable 流程，新增代码量小。
- **UI 改动（仅描述）**：新增「模板管理」区（列表/另存当前画布为模板/编辑/删除）；设备选中后展示「匹配模板：XXX（预览）」与「套用」按钮；可选「新设备自动套用」开关。
- **正确性兜底**：模板作为**起点**而非强制——自动套用默认 opt-in；机位差异大时用户可在预览后微调；支持多模板按更精确 match 胜出。

### P3 — 文案与健壮性（顺手）
- 预览失败文案当前写 `CDN 脚本加载失败(内网?)`（`device-config.html:422`），实际源是本地 `vendor`，改为 `本地播放库未就绪或流地址无效`，避免误导。
- 截帧超时/失败状态码（`504`/`502`）已有明确提示，保留。

---

## 4. 离线部署核查清单

构建与启动后逐项确认：

```bash
# 1. 镜像内静态资源就位（构建时 COPY static /app/static 已保证）
docker exec <backend容器> ls /app/static/vendor/      # 应含 flv.min.js hls.min.js

# 2. 运行时 HTTP 可访问（后端 /static 路由）
curl -sI http://<server>:8000/static/vendor/flv.min.js | head -1   # HTTP/1.1 200
curl -sI http://<server>:8000/static/vendor/hls.min.js | head -1   # HTTP/1.1 200
curl -sI http://<server>:8000/static/config.js        | head -1   # 补齐后应为 200

# 3. 全链路内网：WVP / ZLMediaKit / 后端在同一内网
#    device-config.html 的 baseUrl 用内网地址（或留空走同源），不要用公网 DNS

# 4. 模板资源就位（P2 模板式自动划线）
docker exec <backend容器> ls /app/configs/line_templates.json   # 应存在（补齐后）
curl -s http://<server>:8000/api/line-templates | head -c 200   # 返回模板列表 JSON
```

---

## 5. 说明

本文件为**方案文档，不含代码实现**。待确认优化范围后，再进入实施：
- **P0** 全做（config.js 补齐/移除 + vendor 打包确认）；
- **P1** 截帧缓存选 方案 A（Redis，推荐）还是 A+B；
- **P2** 模板式自动划线做 Level 1（模板库+手动套用）还是 Level 1+2（含规则自动匹配+批量）。
