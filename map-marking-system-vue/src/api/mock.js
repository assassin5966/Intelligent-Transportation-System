/**
 * 内置 Mock 后端（仅 USE_MOCK=true 时启用）
 * ---------------------------------------------------------------------------
 * 在内存中模拟 api(8).md v0.9.0 的全部前端相关 REST 端点，返回结构与文档一致，
 * 以便无真实后端时也能完整体验「地图标点 CRUD + 统计 + 告警 + 预测 + 警力 + 拥堵 + 历史报表 + 业务规则」联动。
 * 与生产差异仅在于数据来源；前端调用路径（endpoints.js）与错误处理完全一致。
 *
 * v0.9.0 对齐点：
 *   - 设备列表返回 longitude / latitude（§3.2）
 *   - 设备统计含 hour / hour_* / person_flow_per_min（§4.2/§4.3）
 *   - 移除 GET /api/stats/congestion（独立查询端点已废弃）
 *   - 移除 POST /api/devices/wvp-webhook（端点已删除）
 *   - 新增 GET /api/stats/hourly/history（§4.5 长期报表）
 *   - 新增 GET / PUT /api/config/business-rules（§10 业务规则，含 400 校验）
 */

import { CITY_WALL_POINTS } from '../data/cityWallPoints.js'

class MockError extends Error {
  constructor(status, detail) {
    super(detail)
    this.status = status
    this.detail = detail
  }
}

const now = () => new Date().toISOString()
const delay = (ms = 180) => new Promise((r) => setTimeout(r, ms))

// —— 内存存储 ——
const devices = new Map()
const regions = new Map()
const alerts = []
const events = []
let totalOfficers = 0
let predictionCache = null
let policePlan = null

// —— §10 业务规则配置（v0.9.0 新增；结构与 business_rules.yaml 元数据一致）——
const RULE_GROUPS = [
  {
    key: 'counting', title: '越线计数', desc: '跨线计数算法参数 (AI 服务, counter.py)',
    params: [
      { key: 'min_distance_ratio', label: '最小距离比例', desc: '距线最小距离占帧短边比例, 防抖', type: 'float', step: 0.001, min: 0, max: 1, default: 0.02 },
      { key: 'hold_frames', label: '滞留确认帧数', desc: '跨线后需在新侧连续保持的帧数', type: 'int', min: 1, max: 10, default: 3 }
    ]
  },
  {
    key: 'congestion', title: '拥挤判定', desc: '双阈值拥挤判定参数 (后端)',
    params: [
      { key: 'congestion_min_flow', label: '最小车流速度', desc: '低于该值且 ROI 车辆超阈值判为拥挤 (辆/分钟)', type: 'float', step: 0.1, min: 0, max: 50, default: 5 }
    ]
  },
  {
    key: 'police', title: '警力分配', desc: '三阶段分配算法权重 (后端)',
    params: [
      { key: 'alpha', label: '当前人数权重 α', desc: '需求 = α×当前 + β×预测', type: 'float', step: 0.05, min: 0, max: 1, default: 0.6 },
      { key: 'beta', label: '预测人数权重 β', desc: '需求 = α×当前 + β×预测', type: 'float', step: 0.05, min: 0, max: 1, default: 0.4 },
      { key: 'min_officers_per_region', label: '区域最小警力', desc: '每区域保底警力数', type: 'int', min: 0, max: 10, default: 1 }
    ]
  },
  {
    key: 'prediction', title: '时序预测', desc: 'Chronos-2 预测调度参数 (后端)',
    params: [
      { key: 'interval_minutes', label: '预测区间(分钟)', desc: 'N 分钟区间预测', type: 'int', min: 5, max: 120, default: 15 },
      { key: 'series_length', label: '历史序列长度', desc: '输入历史序列长度', type: 'int', min: 10, max: 100, default: 30 },
      { key: 'vehicle_person_max', label: '车转人上限', desc: '车流按 random(2,该值) 转化为人流', type: 'int', min: 2, max: 10, default: 5 }
    ]
  },
  {
    key: 'anomaly', title: '视频异常', desc: '黑屏/花屏检测参数 (AI 服务)',
    params: [
      { key: 'anomaly_cooldown_seconds', label: '异常冷却(秒)', desc: '同一设备同一异常同相位去重窗口', type: 'int', min: 10, max: 600, default: 60 },
      { key: 'anomaly_confirm_frames', label: '连续确认帧数', desc: '状态转移确认去抖帧数', type: 'int', min: 1, max: 60, default: 5 }
    ]
  },
  {
    key: 'tracking', title: '目标跟踪', desc: '检测跟踪参数 (AI 服务, tracker.py)',
    params: [
      { key: 'track_buffer', label: '轨迹缓冲帧数', desc: '丢失目标保留帧数', type: 'int', min: 10, max: 300, default: 30 },
      { key: 'detect_interval', label: '检测间隔帧', desc: '每 N 帧执行一次检测', type: 'int', min: 1, max: 10, default: 1 }
    ]
  }
]
// 当前生效值存储（缺省回落 default）
const ruleValues = {}
RULE_GROUPS.forEach((g) => g.params.forEach((p) => { ruleValues[g.key + '.' + p.key] = p.default }))
function ruleGroupsView() {
  return {
    file: 'configs/business_rules.yaml',
    hot_reload: true,
    groups: RULE_GROUPS.map((g) => ({
      key: g.key, title: g.title, desc: g.desc,
      params: g.params.map((p) => ({ ...p, value: ruleValues[g.key + '.' + p.key] }))
    }))
  }
}

// 预置几个摄像头设备（含地理 lng/lat 仅用于地图落点，文档设备模型无该字段）
function seed() {
  const demo = [
    { id: 'cam-gate-north', name: '北门摄像头', stream_url: 'rtsp://192.168.1.100:554/stream1', line_coords: '0.1,0.75,0.9,0.75', anchor_coords: '0.5,0.9', count_only: '', camera_type: 'vehicle', roi_coords: '0.05,0.6,0.95,0.6,0.95,0.95,0.05,0.95', max_vehicles: 10, max_persons: 0, gb_device_id: '', gb_channel_id: '', status: 'online', lng: 113.3132, lat: 40.0935 },
    { id: 'cam-square-south', name: '南广场人流', stream_url: 'rtsp://192.168.1.101:554/stream2', line_coords: '0.2,0.5,0.8,0.5', anchor_coords: '0.5,0.7', count_only: '', camera_type: 'person', roi_coords: '', max_vehicles: 0, max_persons: 200, gb_device_id: '', gb_channel_id: '', status: 'online', lng: 113.3180, lat: 40.0890 },
    { id: 'cam-bridge-west', name: '西桥车流', stream_url: 'rtsp://192.168.1.102:554/stream3', line_coords: '0.5,0.1,0.5,0.9', anchor_coords: '0.7,0.5', count_only: 'enter', camera_type: 'vehicle', roi_coords: '', max_vehicles: 8, max_persons: 0, gb_device_id: 'GB-001', gb_channel_id: 'CH-1', status: 'synced', lng: 113.3090, lat: 40.0950 }
  ]
  demo.forEach((d) => devices.set(d.id, { ...d, last_heartbeat: now() }))
}
seed()

let aid = 100
function pushAlert(a) {
  alerts.unshift({ id: ++aid, created_at: now(), ...a })
  if (alerts.length > 1000) alerts.length = 1000
}
// 预置几条告警
pushAlert({ rule_id: 'vehicle_saturate_warning', level: 'warning', category: 'vehicle_saturate', message: '车辆接近饱和 (205/200)', value: 205, threshold: 200 })
pushAlert({ rule_id: 'person_saturate_warning', level: 'warning', category: 'person_saturate', message: '游客数量接近饱和 (8120/8000)', value: 8120, threshold: 8000 })
// v0.7.0 拥挤告警样本（§5.2）
pushAlert({ rule_id: 'congestion_cam-gate-north', level: 'critical', category: 'congestion', message: '设备 北门摄像头 拥挤 (ROI 12/10, 流量 3.2/min)', value: 12, threshold: 10, phase: 'onset', device_id: 'cam-gate-north', vehicle_flow_per_min: 3.2 })

/** 由设备状态推导实时统计（贴合 §4.1 字段） */
function computeRealtime() {
  let cv = 0, cp = 0, tvin = 0, tvout = 0, tpin = 0, tpout = 0
  devices.forEach((d) => {
    const base = (d.camera_type === 'person' ? 0 : 30) + (d.id.length % 7) * 3
    const pbase = d.camera_type === 'vehicle' ? 0 : 150 + (d.id.length % 5) * 20
    cv += base; cp += pbase
    tvin += base * 9; tvout += base * 8
    tpin += pbase * 13; tpout += pbase * 12
  })
  return {
    current_vehicles: cv,
    current_persons: cp,
    today_vehicle_in: tvin,
    today_vehicle_out: tvout,
    today_person_in: tpin,
    today_person_out: tpout,
    active_devices: [...devices.values()].filter((d) => d.status === 'online').length,
    updated_at: now()
  }
}

// 拥堵状态缓存：模拟后端双阈值判定状态机（§4.4）
const congestionState = new Map() // device_id -> { congested, roi_vehicles, vehicle_flow_per_min, updated_at }

function deviceStat(d) {
  const isPerson = d.camera_type === 'person'
  const base = (isPerson ? 0 : 12 + (d.id.length % 5) * 4)
  const pbase = isPerson ? 156 + (d.id.length % 4) * 30 : 0
  // v0.7.0 新增字段：roi_vehicles / vehicle_flow_per_min / congested / max_vehicles
  const mv = d.max_vehicles || 0
  let roi = 0, flow = 0, congested = false
  if (mv > 0 && !isPerson) {
    // 模拟 ROI 内车辆数（围绕 max_vehicles 波动，偶尔触发拥堵）
    roi = Math.max(0, mv + Math.round(Math.sin(Date.now() / 60000 + d.id.length) * 3))
    flow = Math.max(0, +(20 + Math.cos(Date.now() / 30000 + d.id.length) * 15).toFixed(1))
    // 双阈值判定：roi_vehicles >= max_vehicles 且 flow < 5
    congested = roi >= mv && flow < 5
  }
  return {
    device_id: d.id,
    name: d.name,
    camera_type: d.camera_type || '',
    status: d.status,
    max_vehicles: mv || null,
    current_vehicles: base,
    current_persons: pbase,
    today_vehicle_in: base * 7,
    today_vehicle_out: base * 6,
    today_person_in: pbase * 13,
    today_person_out: pbase * 12,
    // v0.9.0 新增：当前小时累计（来自每小时 Hash）
    hour: new Date().getHours(),
    hour_vehicle_in: Math.max(1, Math.round(base * 0.6)),
    hour_vehicle_out: Math.max(1, Math.round(base * 0.5)),
    hour_person_in: isPerson ? Math.max(1, Math.round(pbase * 0.4)) : 0,
    hour_person_out: isPerson ? Math.max(1, Math.round(pbase * 0.35)) : 0,
    roi_vehicles: roi,
    vehicle_flow_per_min: flow,
    // v0.9.0 新增：人流速度（60 秒滑动窗口速率）
    person_flow_per_min: isPerson ? +Math.max(0, (20 + Math.sin(Date.now() / 45000 + d.id.length) * 12)).toFixed(1) : 0,
    congested
  }
}

/** 路由分发表：根据 method + 正则路径分发 */
function route(method, path, body, query) {
  // 去掉查询串
  const clean = path.split('?')[0]
  const seg = clean.split('/').filter(Boolean) // ["api","devices",...]

  // §1 健康检查
  if (method === 'GET' && clean === '/health') {
    return { status: 'ok', service: 'backend' }
  }

  // §3 设备管理
  if (method === 'GET' && clean === '/api/devices') {
    // v0.9.0 §3.2：返回 longitude / latitude（按设备名匹配地理库；未匹配为 null）
    return [...devices.values()].map((d) => {
      const { lng, lat, ...rest } = d
      return { ...rest, longitude: lng ?? null, latitude: lat ?? null }
    })
  }
  if (method === 'POST' && clean === '/api/devices') {
    if (!body || !body.id || !body.name || !body.stream_url) {
      throw new MockError(400, '缺少必填字段 id / name / stream_url')
    }
    if (devices.has(body.id)) throw new MockError(409, 'device already exists')
    const dev = {
      id: body.id, name: body.name, stream_url: body.stream_url,
      line_coords: body.line_coords || '0.5,0.1,0.5,0.9',
      anchor_coords: body.anchor_coords || '0.5,0.9',
      count_only: body.count_only ?? '',
      camera_type: body.camera_type || '',
      roi_coords: body.roi_coords || '',
      max_vehicles: body.max_vehicles || 0,
      gb_device_id: body.gb_device_id || '', gb_channel_id: body.gb_channel_id || '',
      status: 'registered', last_heartbeat: now(),
      lng: typeof body.lng === 'number' ? body.lng : 113.31,
      lat: typeof body.lat === 'number' ? body.lat : 40.09
    }
    devices.set(dev.id, dev)
    return { id: dev.id, status: 'registered' }
  }
  if (method === 'POST' && clean === '/api/devices/sync') {
    return { added: 0, started: 0, stopped: 0, recovered: 0 }
  }
  // /api/devices/{id}/stream | /play | /enable | /snapshot
  let m = clean.match(/^\/api\/devices\/([^/]+)\/(stream|play|enable|snapshot)$/)
  if (m) {
    const id = decodeURIComponent(m[1])
    if (!devices.has(id)) throw new MockError(404, 'device not found')
    // v0.9.0 §3.5：stream 响应含 longitude / latitude
    if (m[2] === 'stream') {
      const d = devices.get(id)
      return { device_id: id, stream_url: d.stream_url, stream_id: 'mock-' + id, longitude: d.lng ?? null, latitude: d.lat ?? null }
    }
    if (m[2] === 'play') {
      // §3.6 前端播放地址：按设备类型三分支翻译
      const dev = devices.get(id)
      const su = dev.stream_url || ''
      if (/^https?:\/\//.test(su)) {
        return { device_id: id, play_url: su, protocol: 'flv', source: 'direct', source_stream_url: su }
      }
      // RTSP / MediaMTX → HLS（对齐文档 §3.6 示例）
      const path = (su.split('/').pop() || id).replace(/^stream/i, '') || id
      return { device_id: id, play_url: 'http://172.16.168.9:8888/' + path + '/index.m3u8', protocol: 'hls', source: 'mediamtx', source_stream_url: su }
    }
    if (m[2] === 'enable') {
      const dev = devices.get(id)
      dev.status = 'online'
      const cfg = { line_coords: body?.line_coords || dev.line_coords, anchor_coords: body?.anchor_coords || dev.anchor_coords, count_only: body?.count_only ?? dev.count_only, camera_type: body?.camera_type || dev.camera_type, roi_coords: body?.roi_coords || dev.roi_coords }
      if (body?.max_vehicles != null) cfg.max_vehicles = body.max_vehicles
      Object.assign(dev, cfg)
      return { device_id: id, status: 'online', stream_url: dev.stream_url }
    }
    // snapshot: 二进制图片，由 mockRequest 上方特殊处理（返回占位URL），此处不命中
  }
  m = clean.match(/^\/api\/devices\/([^/]+)$/)
  if (m) {
    const id = decodeURIComponent(m[1])
    if (method === 'DELETE') {
      if (!devices.has(id)) throw new MockError(404, 'device not found')
      devices.delete(id)
      return { status: 'deleted', id }
    }
  }

  // §4 实时统计
  if (method === 'GET' && clean === '/api/stats/realtime') return computeRealtime()
  if (method === 'GET' && clean === '/api/stats/devices') {
    return [...devices.values()].map(deviceStat)
  }
  m = clean.match(/^\/api\/stats\/devices\/([^/]+)$/)
  if (m) {
    const d = devices.get(decodeURIComponent(m[1]))
    if (!d) throw new MockError(404, 'device not found')
    return deviceStat(d)
  }

  // §4.4 拥挤数据上报（AI → 后端，v0.9.0 保留 POST；GET 查询端点已废弃）
  if (method === 'POST' && clean === '/api/stats/congestion') {
    if (!body?.device_id) throw new MockError(400, 'device_id is required')
    const d = devices.get(body.device_id)
    if (!d) throw new MockError(404, 'device not found')
    const mv = d.max_vehicles || 0
    const roi = body.roi_vehicles ?? 0
    const flow = body.vehicle_flow_per_min ?? 0
    const congested = mv > 0 && roi >= mv && flow < 5
    congestionState.set(body.device_id, { congested, roi_vehicles: roi, vehicle_flow_per_min: flow, updated_at: now() })
    // 状态转移时触发告警（§5.2 拥挤告警）
    if (congested && (!congestionState.has(body.device_id + ':alerted') || !congestionState.get(body.device_id + ':alerted'))) {
      pushAlert({ rule_id: 'congestion_' + body.device_id, level: 'critical', category: 'congestion', message: `设备 ${d.name} 拥挤 (ROI ${roi}/${mv}, 流量 ${flow.toFixed(1)}/min)`, value: roi, threshold: mv, phase: 'onset', device_id: body.device_id, vehicle_flow_per_min: flow })
      congestionState.set(body.device_id + ':alerted', true)
    }
    return { device_id: body.device_id, congested, roi_vehicles: roi, vehicle_flow_per_min: flow, max_vehicles: mv }
  }

  // §4.5 长期报表（MySQL 归档，v0.9.0 新增）
  if (method === 'GET' && clean === '/api/stats/hourly/history') {
    const start = query?.start_date
    const end = query?.end_date
    // 参数校验：YYYY-MM-DD 或 YYYY-MM-DD:HH（422）
    const pat = /^(\d{4}-\d{2}-\d{2})(?::(\d{1,2}))?$/
    const parse = (v) => {
      const mch = pat.exec(v || '')
      if (!mch) return null
      const hour = mch[2] != null ? Number(mch[2]) : null
      if (hour != null && (hour < 0 || hour > 23)) return null
      return { date: mch[1], hour }
    }
    const s = parse(start)
    const e = parse(end)
    if (!s || !e) throw new MockError(422, '时间格式非法, 应为 YYYY-MM-DD 或 YYYY-MM-DD:HH')
    const startKey = s.date + ':' + String(s.hour != null ? s.hour : 0).padStart(2, '0')
    const endKey = e.date + ':' + String(e.hour != null ? e.hour : 23).padStart(2, '0')
    if (startKey > endKey) throw new MockError(422, 'start_date 不能晚于 end_date')
    // 指定设备须存在（404）；不传 → 全局合计
    const targetId = query?.device_id || null
    if (targetId && !devices.has(targetId)) throw new MockError(404, 'device not found')
    // 生成伪归档记录：按 (日期, 小时) 聚合，数据围绕设备 id 哈希波动
    // 注意：统一用 UTC 解析/格式化，避免本地时区导致日期偏移
    const records = []
    const seedNum = targetId ? targetId.length : 3
    for (let d = new Date(s.date + 'T00:00:00Z'); !isNaN(d) && d <= new Date(e.date + 'T00:00:00Z'); d.setUTCDate(d.getUTCDate() + 1)) {
      const ds = d.toISOString().slice(0, 10)
      for (let h = 0; h <= 23; h++) {
        const inRange = (ds + ':' + String(h).padStart(2, '0')) >= startKey && (ds + ':' + String(h).padStart(2, '0')) <= endKey
        if (!inRange) continue
        // 模拟日内曲线：早晚高峰车多
        const rush = Math.exp(-Math.pow(h - 8, 2) / 8) + Math.exp(-Math.pow(h - 18, 2) / 8)
        const vin = Math.round((3 + rush * 60 + (seedNum % 7) * 2))
        const vout = Math.round(vin * 0.85)
        const pin = Math.round((5 + rush * 40 + (seedNum % 5) * 3))
        const pout = Math.round(pin * 0.9)
        records.push({ stat_date: ds, hour: h, vehicle_in: vin, vehicle_out: vout, person_in: pin, person_out: pout })
      }
    }
    return { device_id: targetId, start: startKey, end: endKey, records }
  }

  // §5 告警
  if (method === 'GET' && clean === '/api/alerts') {
    const lim = Math.min(Math.max(Number(query?.limit) || 100, 1), 1000)
    return alerts.slice(0, lim)
  }
  if (method === 'POST' && clean === '/api/alerts/anomaly') {
    const a = {
      rule_id: 'video_' + (body?.anomaly_type || 'black_screen'),
      level: body?.phase === 'recovery' ? 'info' : 'critical',
      category: 'video_anomaly',
      message: `设备 ${body?.device_id || '?'} 检测到${(body?.anomaly_type === 'flower_screen') ? '花屏' : '黑屏'}异常`,
      device_id: body?.device_id, anomaly_type: body?.anomaly_type, phase: body?.phase || 'onset', scores: body?.scores || {}
    }
    pushAlert(a)
    return { status: 'ok', alert: { id: aid, created_at: now(), ...a } }
  }

  // §6 预测
  if (method === 'POST' && clean === '/api/prediction/predict') {
    const history = { person: [12, 7, 17, 22, 19], vehicle: [11, 9, 8, 13, 10], converted_vehicle: [39, 31, 26, 41, 33], total: [51, 38, 43, 58, 49] }
    predictionCache = { predicted_total: 505, interval_minutes: 15, series_length: 30, degraded: false, history, generated_at: now() }
    return predictionCache
  }
  if (method === 'GET' && clean === '/api/prediction/latest') {
    // 兜底：尚未手动触发预测时，返回一个与 POST 结构一致的默认预测，保证页面始终有数据可展示
    if (!predictionCache) {
      const history = { person: [12, 7, 17, 22, 19], vehicle: [11, 9, 8, 13, 10], converted_vehicle: [39, 31, 26, 41, 33], total: [51, 38, 43, 58, 49] }
      predictionCache = { predicted_total: 505, interval_minutes: 15, series_length: 30, degraded: false, history, generated_at: now() }
    }
    return predictionCache
  }
  if (method === 'GET' && clean === '/api/prediction/health') {
    return { status: 'ok', service: 'prediction', degraded: false }
  }

  // §7 警力
  if (method === 'POST' && clean === '/api/police/regions') {
    if (!body?.id || !body?.name || body?.center_x == null || body?.center_y == null || !body?.device_id) {
      throw new MockError(400, '缺少必填字段 id/name/center_x/center_y/device_id')
    }
    regions.set(body.id, { id: body.id, name: body.name, center_x: body.center_x, center_y: body.center_y, device_id: body.device_id, current_officers: 0, current_persons: 100 + (body.id.length % 5) * 20 })
    return { id: body.id, status: 'registered' }
  }
  if (method === 'GET' && clean === '/api/police/regions') {
    return [...regions.values()]
  }
  if (method === 'DELETE' && clean === '/api/police/regions') {
    const id = decodeURIComponent(clean.split('/').pop())
    if (!regions.has(id)) throw new MockError(404, 'region not found')
    regions.delete(id)
    return { status: 'deleted', id }
  }
  if (method === 'POST' && clean === '/api/police/total') {
    if (typeof body?.total !== 'number' || body.total < 0) throw new MockError(400, 'total 不能为负数')
    totalOfficers = body.total
    return { total: totalOfficers, status: 'ok' }
  }
  if (method === 'GET' && clean === '/api/police/allocation') {
    return { total_officers: totalOfficers, regions: [...regions.values()].map((r) => ({ region_id: r.id, name: r.name, current_officers: r.current_officers, current_persons: r.current_persons })) }
  }
  if (method === 'POST' && clean === '/api/police/optimize') {
    if (regions.size === 0 || totalOfficers === 0) throw new MockError(400, '无注册区域或总警力为 0, 无法优化')
    const regs = [...regions.values()]
    let assigned = 0
    const out = regs.map((r) => {
      const demand = r.current_persons * 0.1 + 5
      const target = Math.max(1, Math.round((demand / (regs.reduce((s, x) => s + x.current_persons, 0) || 1)) * totalOfficers))
      const delta = target - r.current_officers
      assigned += target
      r.current_officers = target
      return { region_id: r.id, name: r.name, device_id: r.device_id, current_crowd: r.current_persons, predicted_crowd: r.current_persons * 1.1, demand, current_officers: target - delta, target_officers: target, delta }
    })
    policePlan = {
      total_officers: totalOfficers, regions: out, movements: [],
      summary: { total_movements: 0, coverage_score: 0.91, efficiency_score: 0.85, overall_score: 0.89, generated_at: now() }
    }
    return policePlan
  }
  if (method === 'GET' && clean === '/api/police/plan') {
    // 兜底：尚未执行优化时，基于当前区域/总警力生成一个结构与 POST /optimize 一致的默认方案
    if (!policePlan) {
      const regs = [...regions.values()]
      policePlan = {
        total_officers: totalOfficers,
        regions: regs.map((r) => ({
          region_id: r.id, name: r.name, device_id: r.device_id,
          current_crowd: r.current_persons, predicted_crowd: Math.round(r.current_persons * 1.1),
          demand: r.current_persons * 0.1 + 5, current_officers: r.current_officers,
          target_officers: r.current_officers, delta: 0
        })),
        movements: [],
        summary: {
          total_movements: 0,
          coverage_score: regs.length ? 0.8 : 0,
          efficiency_score: regs.length ? 0.75 : 0,
          overall_score: regs.length ? 0.78 : 0,
          generated_at: now()
        }
      }
    }
    return policePlan
  }

  // §8 事件历史
  if (method === 'GET' && clean === '/api/events') {
    const lim = Math.min(Math.max(Number(query?.limit) || 100, 1), 2000)
    if (events.length === 0) {
      const types = ['VehicleEnter', 'VehicleExit', 'PersonEnter', 'PersonExit']
      const ids = [...devices.keys()]
      for (let i = 0; i < 30; i++) {
        events.push({ device_id: ids[i % ids.length] || 'cam-gate-north', event_type: types[i % 4], occurred_at: now(), created_at: now() })
      }
    }
    return events.slice(0, lim)
  }

  // §10 业务规则配置（v0.9.0 新增）
  if (method === 'GET' && clean === '/api/config/business-rules') {
    return ruleGroupsView()
  }
  if (method === 'PUT' && clean === '/api/config/business-rules') {
    // 校验：仅接受已定义分组/参数，未知项忽略；按元数据 type 强转，超范围 400
    let applied = 0
    Object.entries(body || {}).forEach(([gk, params]) => {
      const g = RULE_GROUPS.find((x) => x.key === gk)
      if (!g) return
      Object.entries(params || {}).forEach(([pk, rawVal]) => {
        const p = g.params.find((x) => x.key === pk)
        if (!p) return
        const num = p.type === 'int' ? parseInt(rawVal, 10) : parseFloat(rawVal)
        if (!Number.isFinite(num)) throw new MockError(400, `参数 ${gk}.${pk} 类型非法, 应为 ${p.type}`)
        if (p.min != null && num < p.min) throw new MockError(400, `参数 ${gk}.${pk} 超出范围 [${p.min}, ${p.max ?? '∞'}]`)
        if (p.max != null && num > p.max) throw new MockError(400, `参数 ${gk}.${pk} 超出范围 [${p.min}, ${p.max}]`)
        ruleValues[gk + '.' + pk] = num
        applied++
      })
    })
    if (applied === 0) throw new MockError(400, '请求体无有效参数')
    return { status: 'ok', message: '已保存, 热重载已生效', ...ruleGroupsView() }
  }

  throw new MockError(404, 'mock: 未匹配的接口 ' + method + ' ' + clean)
}

/**
 * 点位分类推导（按文档 §4 约定：category 按设备名称匹配 device_category.json）
 * mock 无外部分类库，这里按 camera_type 给出贴近真实命名的分类，便于前端展示「未分类」之外的效果。
 */
function mockCategory(d) {
  if (d.camera_type === 'person') return '城墙出入口便道监控点位'
  if (d.camera_type === 'vehicle') return '城墙入口车辆卡口点位'
  return '城墙出入口便道监控点位'
}

/**
 * 由城墙点位（CITY_WALL_POINTS）派生 WebSocket 设备明细。
 * 使 MockSocket 推送的 stats.devices 携带这 25 个真实点位（device_id = 点位 id），
 * 从而 MapPanel 的城墙点位能真正被 WS 数据驱动（status / congestion_score / 计数）；
 * 真实后端只需以相同点位 id 作为 device_id 推送即可对接。
 * congestion_score 随 id 与时间平滑变化，演示不同点位红/黄/绿分布。
 */
function wallPointDevice(p) {
  const isVeh = p.type === '卡口'
  const seedN = (p.id.charCodeAt(p.id.length - 1) + p.id.length) % 10
  const t = (Math.sin(Date.now() / 50000 + seedN) + 1) / 2   // 0~1 慢变
  const congScore = +t.toFixed(2)
  const current = isVeh ? Math.round(t * 120 + 5) : Math.round(t * 200 + 10)
  const daily = isVeh ? Math.round(t * 9000 + 800) : Math.round(t * 48000 + 2000)
  const hourly = isVeh ? Math.round(t * 400 + 40) : Math.round(t * 1200 + 200)
  const vCong = isVeh && congScore >= 0.66
  const pCong = !isVeh && congScore >= 0.66
  return {
    device_id: p.id,
    name: p.desc,
    camera_type: isVeh ? 'vehicle' : 'person',
    status: p.status,
    max_vehicles: isVeh ? 20 : null,
    max_persons: isVeh ? null : 300,
    longitude: p.coord[0],
    latitude: p.coord[1],
    category: isVeh ? '城墙入口车辆卡口点位' : '城墙出入口便道监控点位',
    current_vehicles: isVeh ? current : 0,
    current_persons: isVeh ? 0 : current,
    today_vehicle_in: isVeh ? Math.round(daily * 0.52) : 0,
    today_vehicle_out: isVeh ? Math.round(daily * 0.48) : 0,
    today_person_in: isVeh ? 0 : Math.round(daily * 0.53),
    today_person_out: isVeh ? 0 : Math.round(daily * 0.47),
    hour: new Date().getHours(),
    hour_vehicle_in: isVeh ? Math.round(hourly * 0.52) : 0,
    hour_vehicle_out: isVeh ? Math.round(hourly * 0.48) : 0,
    hour_person_in: isVeh ? 0 : Math.round(hourly * 0.53),
    hour_person_out: isVeh ? 0 : Math.round(hourly * 0.47),
    roi_vehicles: isVeh ? Math.round(congScore * 18) : 0,
    roi_persons: isVeh ? 0 : Math.round(congScore * 280),
    vehicle_flow_per_min: isVeh ? +(congScore * 12).toFixed(1) : 0,
    person_flow_per_min: isVeh ? 0 : +(congScore * 20).toFixed(1),
    vehicle_congested: vCong,
    person_congested: pCong,
    vehicle_score: isVeh ? congScore : 0,
    person_score: isVeh ? 0 : congScore,
    congestion_score: congScore,
    congested: isVeh ? vCong : pCong
  }
}

/**
 * 构造 WebSocket /ws 的 `stats` 消息体（v0.11.0 完整 schema）
 * 返回 { type:'stats', data:<全局合计>, devices:[<完整设备明细>], timestamp }
 * 供前端 MockSocket 周期推送，使 WS 主链路在纯前端（无后端）模式下也能完整运行。
 */
export function mockWsStats() {
  const data = computeRealtime()
  const devicesArr = [...devices.values()].map((d) => {
    const s = deviceStat(d)
    const isPerson = d.camera_type === 'person'
    const mv = d.max_vehicles || 0
    const mp = d.max_persons || 0
    const roiV = s.roi_vehicles
    const roiP = isPerson ? Math.max(0, Math.round(150 + Math.sin(Date.now() / 45000 + d.id.length) * 80)) : 0
    const vFlow = s.vehicle_flow_per_min
    const pFlow = s.person_flow_per_min
    const vCong = mv > 0 && roiV >= mv && vFlow < 5
    const pCong = mp > 0 && roiP >= mp && pFlow < 5
    const vScore = mv > 0 ? Math.max(0, Math.min(1, roiV / mv)) : 0
    const pScore = mp > 0 ? Math.max(0, Math.min(1, roiP / mp)) : 0
    const congScore = isPerson ? pScore : vScore
    return {
      device_id: d.id,
      name: d.name,
      camera_type: d.camera_type || '',
      status: d.status,
      max_vehicles: mv || null,
      max_persons: mp || null,
      longitude: typeof d.lng === 'number' ? d.lng : null,
      latitude: typeof d.lat === 'number' ? d.lat : null,
      category: mockCategory(d),
      current_vehicles: s.current_vehicles,
      current_persons: s.current_persons,
      today_vehicle_in: s.today_vehicle_in,
      today_vehicle_out: s.today_vehicle_out,
      today_person_in: s.today_person_in,
      today_person_out: s.today_person_out,
      hour: s.hour,
      hour_vehicle_in: s.hour_vehicle_in,
      hour_vehicle_out: s.hour_vehicle_out,
      hour_person_in: s.hour_person_in,
      hour_person_out: s.hour_person_out,
      roi_vehicles: roiV,
      roi_persons: roiP,
      vehicle_flow_per_min: vFlow,
      person_flow_per_min: pFlow,
      vehicle_congested: vCong,
      person_congested: pCong,
      vehicle_score: +vScore.toFixed(2),
      person_score: +pScore.toFixed(2),
      congestion_score: +congScore.toFixed(2),
      congested: isPerson ? pCong : vCong
    }
  })
  // 叠加城墙点位（device_id = 点位 id），让 WS 主链路可驱动 MapPanel 的 25 个真实点位
  const wallArr = CITY_WALL_POINTS.map(wallPointDevice)
  return { type: 'stats', data, devices: devicesArr.concat(wallArr), timestamp: now() }
}

/**
 * 构造 WebSocket /ws 的 `prediction` 消息体（v0.11.0）
 * 返回 { type:'prediction', data:<与 POST /api/prediction/predict 一致> }
 */
export function mockWsPrediction() {
  if (!predictionCache) {
    const history = { person: [12, 7, 17, 22, 19], vehicle: [11, 9, 8, 13, 10], converted_vehicle: [39, 31, 26, 41, 33], total: [51, 38, 43, 58, 49] }
    predictionCache = { predicted_total: 505, interval_minutes: 15, series_length: 30, degraded: false, history, generated_at: now() }
  }
  return { type: 'prediction', data: predictionCache }
}

/**
 * 构造 WebSocket /ws 的 `police_plan` 消息体（v0.11.0）
 * 返回 { type:'police_plan', data:<与 GET /api/police/plan 一致> }
 */
export function mockWsPolicePlan() {
  if (!policePlan) {
    const regs = [...regions.values()]
    policePlan = {
      total_officers: totalOfficers,
      regions: regs.map((r) => ({
        region_id: r.id, name: r.name, device_id: r.device_id,
        current_crowd: r.current_persons, predicted_crowd: Math.round(r.current_persons * 1.1),
        demand: r.current_persons * 0.1 + 5, current_officers: r.current_officers,
        target_officers: r.current_officers, delta: 0
      })),
      movements: [],
      summary: {
        total_movements: 0,
        coverage_score: regs.length ? 0.8 : 0,
        efficiency_score: regs.length ? 0.75 : 0,
        overall_score: regs.length ? 0.78 : 0,
        generated_at: now()
      }
    }
  }
  return { type: 'police_plan', data: policePlan }
}

/**
 * Mock 请求入口（被 client.request 在 USE_MOCK 时调用）
 * 注意：/snapshot 返回图片二进制，无法经此 JSON 通道，组件层在 mock 模式下直接用占位图。
 */
export async function mockRequest(method, path, body, query) {
  await delay()
  try {
    return route(method, path, body, query)
  } catch (e) {
    if (e instanceof MockError) throw e
    throw new MockError(500, e.message || 'mock error')
  }
}
