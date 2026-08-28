/**
 * 接口层 —— 严格对接 api(8).md v0.9.0 定义的全部前端相关 REST 端点
 * ---------------------------------------------------------------------------
 * 每个函数对应文档一个接口，并在 JSDoc 中标注：请求方法、路径、参数、返回结构。
 * 字段命名 / 数据类型 / 嵌套结构完全沿用文档，未擅自修改。
 * 真实发送由 client.js 统一完成（JSON 请求体 + 可选 Bearer + 错误 {detail} 拦截）。
 *
 * v0.9.0 新增/变更（相对 v0.7.0）：
 *   - §3.2 设备列表响应新增 longitude / latitude（按设备名匹配 device_geo.json，未匹配 null）
 *   - §3.5 刷新流地址响应新增 longitude / latitude
 *   - §4.2/§4.3 设备计数响应新增 hour / hour_*（当前小时累计）/ person_flow_per_min
 *   - §4.4 移除 GET /api/stats/congestion 独立查询端点（拥挤数据已随 §4.2/§4.3 返回）
 *   - §4.5 GET  /api/stats/hourly/history  小时级长期报表（MySQL 归档）[新增]
 *   - §10.1 GET  /api/config/business-rules 读取业务规则配置 [新增]
 *   - §10.2 PUT  /api/config/business-rules 更新业务规则配置（保存即热重载）[新增]
 *   - 移除 POST /api/devices/wvp-webhook（v0.9.0 已删除该端点）
 *   - §5.3 告警 level 含 info（拥挤 recovery / 视频异常 recovery）
 *
 * 接口清单速查（章节号严格对齐 api(8).md v0.9.0）：
 *   §1     GET  /health                                健康检查
 *   §3.1   POST /api/devices                          注册设备
 *   §3.2   GET  /api/devices                          设备列表(含 longitude/latitude)
 *   §3.3   DELETE /api/devices/{id}                    删除设备
 *   §3.4   POST /api/devices/sync                     WVP 同步
 *   §3.5   GET  /api/devices/{id}/stream              刷新流地址(AI拉流用)
 *   §3.6   GET  /api/devices/{id}/play                前端播放地址(浏览器直放)
 *   §3.7   POST /api/devices/{id}/enable              启流计数(配置计数线)
 *   §3.8   GET  /api/devices/{id}/snapshot            截取画面(返回 JPEG)
 *   §4.1   GET  /api/stats/realtime                   实时统计
 *   §4.2   GET  /api/stats/devices                    各设备计数
 *   §4.3   GET  /api/stats/devices/{id}               单设备计数
 *   §4.4   POST /api/stats/congestion                 AI上报拥挤数据(前端通常不调)
 *   §4.5   GET  /api/stats/hourly/history             小时级长期报表 [v0.9 新增]
 *   §5.1   GET  /api/alerts?limit=                     告警列表
 *   §5.3   POST /api/alerts/anomaly                    视频异常上报
 *   §6.1   POST /api/prediction/predict               触发预测
 *   §6.2   GET  /api/prediction/latest                最近预测
 *   §6.3   GET  /api/prediction/health                预测服务健康
 *   §7.1   POST /api/police/regions                   注册警力区域
 *   §7.2   GET  /api/police/regions                    区域列表
 *   §7.3   DELETE /api/police/regions/{id}            删除区域
 *   §7.4   POST /api/police/total                     设置总警力
 *   §7.5   GET  /api/police/allocation                 当前分配
 *   §7.6   POST /api/police/optimize                  触发优化
 *   §7.7   GET  /api/police/plan                       最近方案
 *   §8.1   GET  /api/events?limit=                     事件历史(前端大屏用)
 *   §10.1  GET  /api/config/business-rules            读取业务规则配置 [v0.9 新增]
 *   §10.2  PUT  /api/config/business-rules            更新业务规则配置 [v0.9 新增]
 * ---------------------------------------------------------------------------
 */
import { get, post, put, del } from './client.js'
import { API_BASE } from './config.js'

/* ============================ 数据模型（JSDoc） ============================ */

/**
 * @typedef {Object} Device
 * @property {string} id            设备唯一标识
 * @property {string} name          设备名称
 * @property {string} stream_url    视频流地址
 * @property {string} [line_coords] 计数线 "x1,y1,x2,y2" 归一化
 * @property {string} [anchor_coords] 内侧锚点 "x,y"
 * @property {string} [count_only]  null | "enter" | "exit"
 * @property {string} [camera_type] null | "vehicle" | "person"
 * @property {string} [roi_coords]  ROI 多边形
 * @property {number} [max_vehicles] 拥挤判断阈值：ROI 内最大车辆数（>0 时开启拥挤判断）[v0.7 新增]
 * @property {string} [gb_device_id] 国标设备ID(仅WVP)
 * @property {string} [gb_channel_id] 国标通道ID(仅WVP)
 * @property {string} [status]      registered|synced|online|offline
 * @property {string} [last_heartbeat] ISO8601
 * @property {number|null} [longitude] 设备经度（按名称匹配 device_geo.json，未匹配 null）[v0.9 新增]
 * @property {number|null} [latitude]  设备纬度（同上）[v0.9 新增]
 */

/**
 * @typedef {Object} DeviceStat
 * @property {string} device_id
 * @property {string} name
 * @property {string} camera_type   vehicle|person|空
 * @property {string} status        online|offline|synced|registered
 * @property {number|null} max_vehicles  拥挤判断阈值（未配置为 null）
 * @property {number} current_vehicles
 * @property {number} current_persons
 * @property {number} today_vehicle_in
 * @property {number} today_vehicle_out
 * @property {number} today_person_in
 * @property {number} today_person_out
 * @property {number} hour               当前小时（0-23）[v0.9 新增]
 * @property {number} hour_vehicle_in    当前小时内车辆进入累计 [v0.9 新增]
 * @property {number} hour_vehicle_out   当前小时内车辆离开累计 [v0.9 新增]
 * @property {number} hour_person_in     当前小时内人员进入累计 [v0.9 新增]
 * @property {number} hour_person_out    当前小时内人员离开累计 [v0.9 新增]
 * @property {number} roi_vehicles        最近一次上报的 ROI 内瞬时车辆数（AI 每 2 秒上报）
 * @property {number} vehicle_flow_per_min 最近 60 秒折算的每分钟车流量
 * @property {number} person_flow_per_min  最近 60 秒折算的每分钟人流量 [v0.9 新增]
 * @property {boolean} congested          是否拥挤（双阈值判定）
 */

/**
 * @typedef {Object} WsDevice
 * WebSocket /ws `stats` 消息中 `devices[]` 的单项（v0.11.0 完整 schema）。
 * 同时包含「设备基础信息」与「实时计数/拥挤」两部分，前端地图打点/设备卡片/拥挤标记共用。
 * @property {string} device_id           设备 ID
 * @property {string} name                设备名称
 * @property {string} camera_type         vehicle（只计车）/ person（只计人）/ 空（全检测）
 * @property {string} status              online / offline / synced / registered
 * @property {number|null} max_vehicles   车辆拥挤阈值（未配置为 null，>0 才判车辆拥挤）
 * @property {number|null} max_persons    人流拥挤阈值（未配置为 null，>0 才判人流拥挤）
 * @property {number|null} longitude      设备经度（按名称匹配地理库，未匹配 null）
 * @property {number|null} latitude       设备纬度（同上）
 * @property {string|null} category       点位分类（按名称匹配 device_category.json，未匹配 null）
 * @property {number} current_vehicles    该设备当前在场车辆数
 * @property {number} current_persons     该设备当前在场人员数
 * @property {number} today_vehicle_in    今日车辆进入累计
 * @property {number} today_vehicle_out   今日车辆离开累计
 * @property {number} today_person_in     今日人员进入累计
 * @property {number} today_person_out    今日人员离开累计
 * @property {number} hour                当前小时（0-23）
 * @property {number} hour_vehicle_in     当前小时内车辆进入累计
 * @property {number} hour_vehicle_out    当前小时内车辆离开累计
 * @property {number} hour_person_in      当前小时内人员进入累计
 * @property {number} hour_person_out     当前小时内人员离开累计
 * @property {number} roi_vehicles        最近一次上报的 ROI 内瞬时车辆数
 * @property {number} roi_persons         最近一次上报的 ROI 内瞬时人员数
 * @property {number} vehicle_flow_per_min  车流速度（辆/分钟）
 * @property {number} person_flow_per_min  人流速度（人/分钟）
 * @property {boolean} vehicle_congested  车辆维度拥挤（roi_vehicles>=max_vehicles 且车流速度低于阈值）
 * @property {boolean} person_congested   人流维度拥挤（roi_persons>=max_persons 且人流速度低于阈值）
 * @property {number} vehicle_score       车辆维度拥挤度（0-1）
 * @property {number} person_score        人流维度拥挤度（0-1）
 * @property {number} congestion_score    加权综合拥挤度（0-1，可用于色阶展示）
 * @property {boolean} congested          综合是否拥挤（人车混合时 congestion_score>=0.5）
 */

/**
 * @typedef {Object} WsStats
 * WebSocket /ws `stats` 消息体（v0.11.0）。
 * `data` 为全局合计（大屏数字卡片），`devices` 为各设备完整明细。
 * @property {number} current_vehicles    当前在场车辆数（总车流）
 * @property {number} current_persons    当前在场人员数（总人流）
 * @property {number} today_vehicle_in    今日车辆进入累计
 * @property {number} today_vehicle_out   今日车辆离开累计
 * @property {number} today_person_in     今日人员进入累计
 * @property {number} today_person_out    今日人员离开累计
 * @property {number} active_devices      活跃设备数（已产生过事件的设备）
 * @property {string} updated_at          最后更新时间（ISO 8601）
 */

/**
 * @typedef {Object} HourlyHistoryRecord
 * @property {string} stat_date  YYYY-MM-DD
 * @property {number} hour       0-23
 * @property {number} vehicle_in
 * @property {number} vehicle_out
 * @property {number} person_in
 * @property {number} person_out
 */

/**
 * @typedef {Object} RealtimeStats
 * @property {number} current_vehicles
 * @property {number} current_persons
 * @property {number} today_vehicle_in
 * @property {number} today_vehicle_out
 * @property {number} today_person_in
 * @property {number} today_person_out
 * @property {number} active_devices
 * @property {string} updated_at
 */

/**
 * @typedef {Object} Alert
 * @property {number} id
 * @property {string} rule_id
 * @property {'warning'|'critical'|'info'} level
 * @property {string} category
 * @property {string} message
 * @property {number} value
 * @property {number} threshold
 * @property {string} created_at
 * @property {string} [phase]              onset | recovery（拥挤告警 §5.2 / 视频异常 §5.3）[v0.7 新增]
 * @property {string} [device_id]          关联设备 ID（拥挤/视频异常告警）[v0.7 新增]
 * @property {number} [vehicle_flow_per_min] 车流速度（拥挤告警）[v0.7 新增]
 * @property {string} [anomaly_type]       black_screen | flower_screen（视频异常告警）[v0.7 新增]
 * @property {object} [scores]             检测信号分数（视频异常告警）[v0.7 新增]
 */

/* ============================ §1 健康检查 ============================ */

/** GET /health —— 业务后端健康检查（§1） */
export function health() {
  return get('/health')
}

/* ============================ §3 设备管理 ============================ */

/**
 * POST /api/devices —— 注册设备（§3.1）
 * @param {Device} device 必填 id/name/stream_url；其余可选
 * @returns {Promise<{id:string, status:string}>} 201 {id,status}
 */
export function registerDevice(device) {
  return post('/api/devices', device)
}

/** GET /api/devices —— 设备列表（§3.2） */
export function listDevices() {
  return get('/api/devices')
}

/**
 * DELETE /api/devices/{device_id} —— 删除设备（§3.3）
 * @param {string} deviceId
 * @returns {Promise<{status:string, id:string}>} 200 {status:"deleted",id}
 */
export function deleteDevice(deviceId) {
  return del('/api/devices/' + encodeURIComponent(deviceId))
}

/** POST /api/devices/sync —— 手动触发 WVP 同步（§3.4） */
export function syncDevices() {
  return post('/api/devices/sync')
}

/**
 * GET /api/devices/{id}/stream —— 刷新流地址（§3.5）
 * v0.9.0 响应新增 longitude / latitude（按设备名匹配 device_geo.json，未匹配 null）。
 * @returns {Promise<{device_id:string, stream_url:string, stream_id:string, longitude:number|null, latitude:number|null}>}
 */
export function getDeviceStream(deviceId) {
  return get('/api/devices/' + encodeURIComponent(deviceId) + '/stream')
}

/**
 * GET /api/devices/{id}/play —— 前端播放地址（§3.6）
 * 返回浏览器可直接播放的流地址，前端按 protocol 选择播放器：
 *   protocol=flv → flv.js；protocol=hls → hls.js（Safari 原生）。
 * 与 §3.5 stream 的区别：stream 供 AI 拉流，play 供浏览器直放。
 * 设备类型三分支翻译：WVP→FLV、RTSP/MediaMTX→HLS、http(s) 直配→原样。
 * @param {string} deviceId
 * @returns {Promise<{device_id:string, play_url:string, protocol:'flv'|'hls', source:string, source_stream_url?:string, stream_id?:string}>}
 */
export function getDevicePlayUrl(deviceId) {
  return get('/api/devices/' + encodeURIComponent(deviceId) + '/play')
}

/**
 * POST /api/devices/{id}/enable —— 启用 WVP 设备并配置计数线（§3.7）
 * 视为设备「编辑/重新配置」的对应接口（文档无 PUT）。
 * v0.7.0 新增 max_vehicles 参数：拥挤判断阈值（>0 时开启拥挤判断，见 §4.4）。
 * @param {string} deviceId
 * @param {{line_coords:string, anchor_coords?:string, count_only?:string, camera_type?:string, roi_coords?:string, max_vehicles?:number}} cfg
 * @returns {Promise<{device_id:string, status:string, stream_url:string}>}
 */
export function enableDevice(deviceId, cfg) {
  return post('/api/devices/' + encodeURIComponent(deviceId) + '/enable', cfg)
}

/**
 * GET /api/devices/{id}/snapshot —— 截取设备画面（§3.8）
 * 直接返回 image/jpeg 二进制；前端用 <img> 加载，这里返回可访问的图片 URL。
 * 错误：503 WVP 未启用 / 404 设备不存在 / 400 未配置 stream_url / 502 截帧失败 / 504 截帧超时。
 * @param {string} deviceId
 * @returns {string} 图片地址（带鉴权头时由组件层处理为 blob）
 */
export function getDeviceSnapshotUrl(deviceId) {
  return API_BASE + '/api/devices/' + encodeURIComponent(deviceId) + '/snapshot'
}

/* ============================ §4 实时统计 ============================ */

/** GET /api/stats/realtime —— 实时统计（§4.1） */
export function getRealtimeStats() {
  return get('/api/stats/realtime')
}

/** GET /api/stats/devices —— 各设备分别计数（§4.2） */
export function getDeviceStats() {
  return get('/api/stats/devices')
}

/**
 * GET /api/stats/devices/{id} —— 单设备计数（§4.3）
 * v0.7.0 响应新增 max_vehicles / roi_vehicles / vehicle_flow_per_min / congested 字段。
 * @param {string} deviceId
 * @returns {Promise<DeviceStat>}
 */
export function getDeviceStat(deviceId) {
  return get('/api/stats/devices/' + encodeURIComponent(deviceId))
}

/* —— §4.4 拥挤数据上报（AI → 后端，周期调用）—— */

/**
 * POST /api/stats/congestion —— AI 上报拥挤数据（§4.4）
 * 由 AI 服务内部调用，前端一般不直接使用。列出供完整性。
 * v0.9.0：拥挤数据无需独立查询端点，roi_vehicles / *_flow_per_min / congested
 * 已随 §4.2 / §4.3 设备统计返回（原 GET /api/stats/congestion 已移除）。
 * @param {{device_id:string, roi_vehicles:number, vehicle_flow_per_min:number, person_flow_per_min?:number}} payload
 * @returns {Promise<{device_id:string, congested:boolean, roi_vehicles:number, vehicle_flow_per_min:number, max_vehicles:number}>}
 */
export function reportCongestion(payload) {
  return post('/api/stats/congestion', payload)
}

/* —— §4.5 长期报表（MySQL 归档）—— */

/**
 * GET /api/stats/hourly/history —— 小时级车流/人流量历史报表（§4.5，v0.9.0 新增）
 * 查询历史任意时段（MySQL hourly_traffic 归档表），支持跨 Redis 30 天窗口的长期报表。
 * @param {string} [deviceId] 指定设备 ID；不传返回全局合计（按日期+小时聚合所有设备）
 * @param {string} startDate  开始时间：YYYY-MM-DD（整日起）或 YYYY-MM-DD:HH（精确到小时），必填
 * @param {string} endDate    结束时间：YYYY-MM-DD（整日至 23 点）或 YYYY-MM-DD:HH，必填且不早于开始
 * @returns {Promise<{device_id:string|null, start:string, end:string, records:HourlyHistoryRecord[]}>}
 * @throws {ApiError} 422 时间格式非法/开始晚于结束；503 MySQL 归档未启用或查询失败
 */
export function getHourlyHistory(deviceId, startDate, endDate) {
  const query = { start_date: startDate, end_date: endDate }
  if (deviceId) query.device_id = deviceId
  return get('/api/stats/hourly/history', query)
}

/* ============================ §5 告警 ============================ */

/**
 * GET /api/alerts —— 告警列表（§5.1）
 * @param {number} [limit] 1~1000，默认100
 * @returns {Promise<Alert[]>}
 */
export function listAlerts(limit) {
  return get('/api/alerts', limit != null ? { limit } : undefined)
}

/**
 * POST /api/alerts/anomaly —— 视频异常上报（§5.3，AI→后端）
 * 前端在检测到异常时也可调用以注入告警。
 * @param {{device_id:string, anomaly_type:'black_screen'|'flower_screen', phase?:string, scores?:object}}
 */
export function reportAnomaly(payload) {
  return post('/api/alerts/anomaly', payload)
}

/* ============================ §6 时序预测 ============================ */

/** POST /api/prediction/predict —— 触发预测（§6.1） */
export function predict() {
  return post('/api/prediction/predict')
}

/** GET /api/prediction/latest —— 最近一次预测（§6.2） */
export function getLatestPrediction() {
  return get('/api/prediction/latest')
}

/** GET /api/prediction/health —— 预测服务健康（§6.3） */
export function getPredictionHealth() {
  return get('/api/prediction/health')
}

/* ============================ §7 警力分配 ============================ */

/**
 * POST /api/police/regions —— 注册警力区域（§7.1）
 * @param {{id:string, name:string, center_x:number, center_y:number, device_id:string}}
 * @returns {Promise<{id:string, status:string}>}
 */
export function registerRegion(region) {
  return post('/api/police/regions', region)
}

/** GET /api/police/regions —— 区域列表（§7.2） */
export function listRegions() {
  return get('/api/police/regions')
}

/**
 * DELETE /api/police/regions/{id} —— 删除区域（§7.3）
 * @param {string} regionId
 */
export function deleteRegion(regionId) {
  return del('/api/police/regions/' + encodeURIComponent(regionId))
}

/**
 * POST /api/police/total —— 设置总警力（§7.4）
 * @param {number} total
 */
export function setTotalOfficers(total) {
  return post('/api/police/total', { total })
}

/** GET /api/police/allocation —— 当前分配状态（§7.5） */
export function getAllocation() {
  return get('/api/police/allocation')
}

/** POST /api/police/optimize —— 触发分配优化（§7.6） */
export function optimizePolice() {
  return post('/api/police/optimize')
}

/** GET /api/police/plan —— 最近分配方案（§7.7） */
export function getPolicePlan() {
  return get('/api/police/plan')
}

/* ============================ §8 事件历史 ============================ */

/**
 * GET /api/events —— 越线事件历史（§8.1，前端大屏滚动列表用）
 * @param {number} [limit] 1~2000，默认100
 */
export function listEvents(limit) {
  return get('/api/events', limit != null ? { limit } : undefined)
}

/* ============================ §10 业务规则配置 ============================ */

/**
 * @typedef {Object} RuleParam
 * @property {string} key      参数标识（与 yaml 键一致）
 * @property {string} label    展示名
 * @property {string} desc     说明
 * @property {'int'|'float'} type  参数类型
 * @property {number} [step]   步长
 * @property {number} [min]    最小值
 * @property {number} [max]    最大值
 * @property {number} default  出厂默认值
 * @property {number} value    当前生效值
 * @property {string} groupKey 所属分组 key
 */

/**
 * @typedef {Object} RuleGroup
 * @property {string} key    分组标识（counting/alerts/congestion/police/prediction/anomaly/tracking）
 * @property {string} title  分组标题
 * @property {string} desc   分组说明
 * @property {Array<RuleParam>} params 参数列表
 */

/**
 * GET /api/config/business-rules —— 读取业务规则配置（§10.1，v0.9.0 新增）
 * 集中读取 configs/business_rules.yaml（计数/告警/拥挤/警力/预测/视频异常/跟踪参数）。
 * @returns {Promise<{file:string, hot_reload:boolean, groups:RuleGroup[]}>}
 */
export function getBusinessRules() {
  return get('/api/config/business-rules')
}

/**
 * PUT /api/config/business-rules —— 更新业务规则配置（§10.2，v0.9.0 新增）
 * 仅提交需修改的分组即可：{ 分组: { 参数: 值 } }；保存后 mtime 触发热重载，无需重启服务。
 * @param {Object.<string, Object.<string, number>>} patch 分组 -> 参数 -> 值
 * @returns {Promise<{status:string, message:string, hot_reload:boolean, file:string, groups:RuleGroup[]}>}
 * @throws {ApiError} 400 无有效参数 / 值类型或范围非法（detail 含参数名）；500 规则文件不存在
 */
export function updateBusinessRules(patch) {
  return put('/api/config/business-rules', patch)
}
