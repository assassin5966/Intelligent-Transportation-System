/**
 * 设备（地图标点）只读状态 —— 对应 api(8).md §3.2 / §4.2（v0.9.0）
 * ---------------------------------------------------------------------------
 * 大屏为只读视图：设备的新增 / 配置 / 删除统一在运维页（设备管理 · 计数启流）完成，
 * 本组合式只负责「读」——拉取设备列表 + 订阅 WS 实时计数 + 维护地图落点。
 *   查 GET /api/devices（基础信息）· GET /api/stats/devices（实时计数兜底）
 *   实时 ws://…/ws stats.devices（主通道）
 *
 * v0.9.0 字段：
 *   - §3.2 设备列表响应含 longitude / latitude（后端按设备名匹配 device_geo.json，未匹配为 null）。
 *   - §4.2 设备统计含 hour_*（当前小时累计）与 person_flow_per_min，随 statsById 透出。
 *   - 拥挤判断字段（congested / roi_vehicles / *_flow_per_min）来自 §4.2 响应。
 *
 * statsRev：统计数据的「变更计数器」。WS 每 2s 覆盖式写入 statsById，
 * 组件若对 statsById 做 deep watch 会带来全量深度遍历 + 全量重绘；
 * 改为写一次自增一次，组件只 watch 这个数字即可。
 */
import { reactive } from 'vue'
import { listDevices, getDeviceStats } from '../api/endpoints.js'
import { useToast } from './useToast.js'

const state = reactive({
  devices: [],        // Device[]（设备基础信息）
  statsById: {},      // { [deviceId]: 实时计数 }
  positions: {},      // { [deviceId]: {lng,lat} } 地图落点（§3.2 经纬度）
  statsRev: 0,        // 统计变更计数器（见上方说明）
  loading: false
})

const { push } = useToast()

/** 归一化后端经纬度字段（§3.2 longitude/latitude；兼容 mock 的 lng/lat），非法返回 null */
function normalizeGeo(d) {
  const candidates = [d.longitude, d.lng]
  const candidatesLat = [d.latitude, d.lat]
  for (let i = 0; i < candidates.length; i++) {
    const lng = Number(candidates[i])
    const lat = Number(candidatesLat[i])
    if (Number.isFinite(lng) && Number.isFinite(lat) && lng !== 0 && lat !== 0) {
      return { lng, lat }
    }
  }
  return null
}

/** 加载设备列表 + 各设备实时计数，并恢复地图落点 */
async function load() {
  state.loading = true
  try {
    const [devs, stats] = await Promise.all([listDevices(), getDeviceStats().catch(() => [])])
    state.devices = devs || []
    applyStats(stats)
    // 落点：严格以后端返回的 longitude/latitude 为准（§3.2）。
    // 后端匹配不到 device_geo.json 时该值为 null，此时不落点（设备仍在列表/统计中，只是不上图），
    // 前端不伪造坐标；已由 WS 落点写入的坐标保持不变。
    devs.forEach((d) => {
      if (state.positions[d.id]) return
      const geo = normalizeGeo(d)
      if (geo) state.positions[d.id] = geo
    })
  } catch (e) {
    push('设备加载失败：' + (e.detail || e.message), 'error')
  } finally {
    state.loading = false
  }
}

/** 应用 §4.2 设备统计到 statsById（轮询/加载共用） */
function applyStats(stats) {
  const map = {}
  ;(stats || []).forEach((s) => { if (s && s.device_id) map[s.device_id] = s })
  state.statsById = map
  state.statsRev++
}

/**
 * 应用 WebSocket /ws `stats.devices` 完整明细到本地状态（v0.11.0 主通道）
 * 同时维护：
 *   - state.devices：设备基础信息（name/camera_type/status/阈值/category/经纬度），保证列表始终最新；
 *   - state.statsById：实时计数 + 拥挤度等全部新字段（组件 charts/cards 读取）；
 *   - state.positions：经纬度落点（WS 提供且合法时写入）。
 * @param {Array} devicesArr  WS stats 消息的 devices 数组（WsDevice[]）
 */
function applyWsDevices(devicesArr) {
  if (!Array.isArray(devicesArr)) return
  devicesArr.forEach((d) => {
    if (!d || !d.device_id) return
    const id = d.device_id
    // 1) 设备基础信息合并进 state.devices
    const idx = state.devices.findIndex((x) => x.id === id)
    const base = {
      id,
      name: d.name,
      camera_type: d.camera_type || '',
      status: d.status,
      max_vehicles: d.max_vehicles ?? 0,
      max_persons: d.max_persons ?? 0,
      category: d.category ?? null,
      longitude: d.longitude ?? null,
      latitude: d.latitude ?? null
    }
    if (idx >= 0) state.devices[idx] = { ...state.devices[idx], ...base }
    else state.devices.push(base)
    // 2) 实时计数 + 拥挤度写入 statsById（统一字段，组件层直接消费）
    state.statsById[id] = {
      device_id: id,
      name: d.name,
      camera_type: d.camera_type || '',
      status: d.status,
      max_vehicles: d.max_vehicles ?? null,
      max_persons: d.max_persons ?? null,
      current_vehicles: d.current_vehicles || 0,
      current_persons: d.current_persons || 0,
      today_vehicle_in: d.today_vehicle_in || 0,
      today_vehicle_out: d.today_vehicle_out || 0,
      today_person_in: d.today_person_in || 0,
      today_person_out: d.today_person_out || 0,
      hour: d.hour ?? new Date().getHours(),
      hour_vehicle_in: d.hour_vehicle_in || 0,
      hour_vehicle_out: d.hour_vehicle_out || 0,
      hour_person_in: d.hour_person_in || 0,
      hour_person_out: d.hour_person_out || 0,
      roi_vehicles: d.roi_vehicles ?? 0,
      roi_persons: d.roi_persons ?? 0,
      vehicle_flow_per_min: d.vehicle_flow_per_min || 0,
      person_flow_per_min: d.person_flow_per_min || 0,
      vehicle_congested: !!d.vehicle_congested,
      person_congested: !!d.person_congested,
      vehicle_score: d.vehicle_score ?? 0,
      person_score: d.person_score ?? 0,
      congestion_score: d.congestion_score ?? 0,
      congested: !!d.congested
    }
    // 3) 经纬度落点：WS 提供且数值合法 → 写入 positions
    const lng = Number(d.longitude)
    const lat = Number(d.latitude)
    if (Number.isFinite(lng) && Number.isFinite(lat) && lng !== 0 && lat !== 0) {
      state.positions[id] = { lng, lat }
    }
  })
  state.statsRev++
}

/** 静默刷新各设备实时计数（§4.2，含拥挤/小时数据；供 useRealtime 轮询兜底调用，失败不弹窗） */
async function refreshStats() {
  const stats = await getDeviceStats()
  applyStats(stats)
}

/** 按 id 取设备基础信息（搜索定位 / 告警跳转用） */
function findById(id) {
  return state.devices.find((d) => d.id === id) || null
}

export function useDevices() {
  return { state, load, refreshStats, applyWsDevices, findById }
}
