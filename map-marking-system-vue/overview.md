# 交付总览 · API v0.9.0 全面对接

> 基于最新 API 文档 `api(8).md`（v0.9.0），从 v0.7.0 全面升级。所有指定页面的接口交互已实现，含错误处理、加载状态、构建/冒烟/浏览器三级验证。

## 📦 接口层（`src/api/`）
- `endpoints.js`：移除 `wvpWebhook` + `getCongestionData`（已废弃）；新增 `getHourlyHistory` (§4.5)、`getBusinessRules` / `updateBusinessRules` (§10)；JSDoc 同步 `longitude/latitude`、`hour/hour_vehicle_in/out/hour_person_in/out/person_flow_per_min` 字段。
- `client.js`：EXPECT_NON_EMPTY 同步。
- `config.js`：版本注释更新至 v0.9.0。

## 🔄 数据层（`src/composables/`）
- `useRealtime.js`：移除 `state.congestion`；`pollOnce()` 改用 `useDevices().refreshStats()`，让拥挤/小时数据跟随 §4.2 设备统计刷新。
- `useDevices.js`：新增 `normalizeGeo`（`Number.isFinite` 校验 + 多字段回退）与 `refreshStats()`。

## 🧪 Mock（`src/api/mock.js`）
- 设备列表/流响应增加 `longitude/latitude`。
- 移除 `wvp-webhook` 与 `GET /api/stats/congestion` 路由。
- `deviceStat()` 注入 `hour` + `hour_*` + `person_flow_per_min` 字段。
- §4.5 历史报表（UTC 日期循环生成早晚高峰曲线，422 校验格式/顺序，404 校验设备）。
- §10 业务规则（6 组 RULE_GROUPS + ruleValues 存储 + GET/PUT 强类型校验）。

## 🧩 新组件
- **`HourlyHistoryChart.vue`**：§4.5 历史车流报表。
  - 下拉（不选=全局合计）+ 日期范围 + 查询/导出按钮。
  - ECharts 四线图（车辆进/出 + 人员进/出，双 y 轴）。
  - 错误处理：`{422: '时间参数格式非法', 503: 'MySQL 未启用', 404: '设备不存在'}` + 重试按钮 + Toast。
  - 加载状态：`showLoading/hideLoading` + 按钮禁用；默认查最近 7 天。
- **`BusinessRulesPanel.vue`**：§10 业务规则配置 Modal。
  - 分组卡片 + number input（dirty 标黄 / invalid 标红）。
  - `patch` computed 仅收集修改项；`save()` PUT 后用响应 groups 回写。
  - 打开时 `watch(props.open)` 重新拉取；前端预校验（后端同样 400 拦截）。

## 🔗 集成
- `StatBoard.vue`：注释掉的「⑤ 指标分析」区块替换为 `<HourlyHistoryChart />`。
- `App.vue`：主题按钮旁新增「⚙ 业务规则」入口（`left:calc(50% + 170px)`）。

## ✅ 测试结果
| 阶段 | 命令 | 结果 |
|---|---|---|
| 构建 | `npm run build` | ✅ 通过（43 modules, 1.21s）|
| 冒烟 | `node scripts/api-smoke.mjs` | ✅ 14/14 全 PASS |
| 浏览器截图 | 主页 / 历史报表 / 业务规则 | ✅ 渲染正常 |

### 14 项冒烟用例
1. 设备列表含 `longitude/latitude`
2. 设备统计含 `hour_vehicle_in/out` + `person_flow_per_min`
3-5. 历史报表：正常（48 条）/ 精确小时（24 条）/ 422 格式非法 × 2
6. 历史报表：404 设备不存在
7-10. 业务规则：读取 / 合法修改 PUT / 超范围 400 / 空体 400
11-12. 废弃端点 404：`GET /api/stats/congestion`、`POST /api/devices/wvp-webhook`
13. 流地址响应含经纬度
14. Mock 完整链路

## ▶️ 运行方式
```bash
pnpm install        # 或 npm install
npm run build       # 生产构建
npm run dev         # 开发服务器（默认 http://localhost:5173）

# Mock 模式（全量走本地内存后端）
http://localhost:5173/?mock=1
```

## 📷 验证截图
- `.workbuddy/verify-main.png` — 主页全貌（3D 地图 + 大屏科技感布局 + ⚙ 业务规则入口）
- `.workbuddy/verify-history2.png` — 历史车流报表四线图渲染（双峰曲线）
- `.workbuddy/verify-rules.png` — 业务规则 Modal 打开

## ⚠️ 已知限制
1. **右侧栏布局挤压**：新增 HourlyHistoryChart (200px) 后总高度超出视口，道路监控/拥堵状态/实时告警/时序预测被挤到屏幕外。建议给 `.side-right` 加 `overflow-y: auto` 或减少 panel。不影响核心功能。
2. **MapPanel.vue:231 `Pixel(NaN, NaN)` 警告**：地图投影未就绪时的既有 try/catch 兜底（line 1177/1179 watch + 1136-1138 `.then()` 自动重渲），非本次改动引入。

## 🛠️ 工具链备注
- agent-browser 安装位置：`C:\Users\DELL\.workbuddy\binaries\node\workspace\node_modules\.bin\agent-browser`（受管理 node workspace，非全局）。
- Git Bash 调用统一用 `C:/...` 风格路径，避免 node 解析 `d:\c\...`。