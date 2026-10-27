# 接口联调升级小结（frontend_api.md v0.11.0）

> 目标：按最新前端对接文档，把页面实时数据链路从「REST 轮询 + 双 WS」改造为
> **「后端 `/ws` 单通道为主、REST 仅兜底」**，并补齐 v0.11.0 新增设备字段的展示。

## 关键架构变更
| 项 | v0.9.0（旧） | v0.11.0（新） |
|----|------|------|
| 实时通道 | 后端 WS + **AI 8001 WS** | **仅后端 8000 `/ws`**（AI 不对前端开放） |
| `stats` 来源 | 轮询 `/api/stats/realtime` + `/api/stats/devices` | `/ws` 的 `stats` 消息：`data`(全局)+`devices[]`(完整明细) |
| 推送内容 | — | `stats`(2s) / `alert` / `prediction` / `police_plan` 即时推 |
| 兜底 | 6s REST 轮询常驻 | WS 不可用时才静默 REST 轮询（`restMode`） |

## `stats.devices[]` 新增字段（已全链路打通）
`longitude`/`latitude`、`category`、`roi_persons`、`vehicle_congested`、
`person_congested`、`vehicle_score`、`person_score`、`congestion_score`(0-1)、`congested`。

## 改动文件
- `src/composables/useRealtime.js`：WS 主通道 + `MockSocket` 模拟器（USE_MOCK 周期推送同构消息）+ REST 兜底 + `restMode` 标记。
- `src/composables/useDevices.js`：新增 `applyWsDevices(devices)` 合并 WS 明细到 `statsById`/`positions`（手动拖拽设备不覆盖）。
- `src/api/endpoints.js`：补充 `WsStats` / `WsDevice` JSDoc 类型定义。
- `src/api/mock.js`：新增 `mockWsStats()`（含 v0.11 全字段）、`mockWsPrediction()`、`mockWsPolicePlan()`；seed 补 `max_persons`。
- `src/api/config.js`：注释修正（AI 不对前端开放）。
- `src/components/MapPanel.vue`：设备标点按 `congestion_score` 着色 + 显示 `category`。
- `src/components/StatBoard.vue`：拥堵分级汇总（severe≥0.66 / mid≥0.33 / normal + 车·人维度）。
- `src/components/AlertPanel.vue`：拥堵告警展示 `congestion_score` 与 `vehicle/person_congested` 维度。
- `src/components/TopBar.vue`：「AI WS」指示灯 → 「REST兜底」指示灯。

## 验证
- `npm run build` ✅ 通过（43 模块，mock 正确拆分为独立 chunk）。
- 运行方式：`npm run dev`（默认 `USE_MOCK` 由 `?mock=1` 或 `.env` 控制；即使无后端，MockSocket 也会驱动完整 WS 链路）。
- 已知环境坑：`vite build` 清空 `dist` 时本机 safe-delete/trash 会报错（非代码问题），构建前 `rm -rf dist` 可规避。

## 对接后端真实服务时
- 后端实现 `/ws` 并按 `frontend_api.md` 推送 4 类消息即可直接联调；
- 设备 `devices[]` 需按文档返回 `longitude/latitude`（按名称匹配地理库）、`category`（点位分类）、`congestion_score` 等；前端已做空值兜底。
