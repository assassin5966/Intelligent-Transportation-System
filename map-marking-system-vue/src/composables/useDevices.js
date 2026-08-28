/**
 * 设备（地图标点）状态与 CRUD —— 对应 api(8).md §3 设备管理（v0.9.0）
 * ---------------------------------------------------------------------------
 * 地图标点 = 摄像头设备。接口映射：
 *   增 POST /api/devices           查 GET /api/devices (+ /api/stats/devices 取实时计数)
 *   删 DELETE /api/devices/{id}    改 POST /api/devices/{id}/enable（文档无 PUT，以"启用/重配置"对应编辑）
 *
 * v0.9.0 变更：
 *   - §3.2 设备列表响应新增 longitude / latitude（后端按设备名匹配 device_geo.json，
 *     未匹配为 null）。落点优先采用后端返回的经纬度，其次沿用前端已维护位置。
 *   - §4.2 设备统计新增 hour_*（当前小时累计）与 person_flow_per_min，随 statsById 透出。
 *   - 拥挤判断字段（congested / roi_vehicles / *_flow_per_min）仍来自 §4.2 响应。
 */
import { reactive } from 'vue'
import {
  listDevices, getDeviceStats, registerDevice, deleteDevice, enableDevice
} from '../api/endpoints.js'
import { useToast } from './useToast.js'

const state = reactive({
  devices: [],        // Device[]（设备基础信息）
  statsById: {},      // { [deviceId]: 实时计数 }
  positions: {},      // { [deviceId]: {lng,lat} } 前端地图落点（文档无此字段）
  loading: false,
  selectedId: null
})

// 用户手动拖拽过的设备：WS 经纬度落点不覆盖其手动位置（仅前端交互用）
const manualPos = new Set()

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
    // 落点：优先用后端返回的 longitude/latitude（§3.2，v0.9.0），
    // 其次保留前端已维护落点（拖拽/新增），否则按城市中心散开。
    // 严格类型校验：仅接受有限数值（排除 undefined/NaN/null/字符串/布尔），非法回退散开默认值
    devs.forEach((d, i) => {
      if (!state.positions[d.id]) {
        const geo = normalizeGeo(d)
        state.positions[d.id] = geo || {
          lng: 113.3132 + (i % 5) * 0.0016 - 0.004,
          lat: 40.0931 + Math.floor(i / 5) * 0.0016 - 0.004
        }
      }
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
}

/**
 * 应用 WebSocket /ws `stats.devices` 完整明细到本地状态（v0.11.0 主通道）
 * 同时维护：
 *   - state.devices：设备基础信息（name/camera_type/status/阈值/category/经纬度），保证列表始终最新；
 *   - state.statsById：实时计数 + 拥挤度等全部新字段（组件 charts/cards 读取）；
 *   - state.positions：经纬度落点（WS 提供且合法、且非用户手动拖拽时写入）。
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
    // 3) 经纬度落点：WS 提供且数值合法、且非用户手动拖拽过 → 写入 positions
    if (!manualPos.has(id)) {
      const lng = Number(d.longitude)
      const lat = Number(d.latitude)
      if (Number.isFinite(lng) && Number.isFinite(lat) && lng !== 0 && lat !== 0) {
        state.positions[id] = { lng, lat }
      }
    }
  })
}

/** 静默刷新各设备实时计数（§4.2，含拥挤/小时数据；供 useRealtime 轮询兜底调用，失败不弹窗） */
async function refreshStats() {
  const stats = await getDeviceStats()
  applyStats(stats)
}

/**
 * 注册设备（地图标点新增）
 * @param {object} payload 文档字段：id/name/stream_url/line_coords/anchor_coords/count_only/camera_type/roi_coords/max_vehicles
 * @param {{lng:number,lat:number}} [pos] 地图落点（仅前端）
 */
async function create(payload, pos) {
  try {
    await registerDevice(payload)
    // 仅为合法数值坐标写入落点，非法（含 NaN）时不设位置，交由 load() 兜底散开
    if (pos && typeof pos.lng === 'number' && typeof pos.lat === 'number' &&
        Number.isFinite(pos.lng) && Number.isFinite(pos.lat)) {
      state.positions[payload.id] = pos
    }
    push('设备「' + payload.name + '」注册成功', 'success')
    await load()
    return true
  } catch (e) {
    push('注册失败：' + (e.detail || e.message), 'error')
    return false
  }
}

/** 删除设备（地图标点删除） */
async function remove(id) {
  try {
    await deleteDevice(id)
    delete state.positions[id]
    if (state.selectedId === id) state.selectedId = null
    push('设备已删除', 'success')
    await load()
    return true
  } catch (e) {
    push('删除失败：' + (e.detail || e.message), 'error')
    return false
  }
}

/**
 * 重新配置设备（编辑/保存计数线等）—— 对应文档 §3.7
 * 文档设备无 PUT，故以 enable 接口承载"更新"语义。
 * v0.7.0 新增 max_vehicles 透传（拥挤判断阈值 §4.4）。
 */
async function configure(id, cfg) {
  try {
    await enableDevice(id, cfg)
    push('设备配置已更新', 'success')
    await load()
    return true
  } catch (e) {
    push('配置失败：' + (e.detail || e.message), 'error')
    return false
  }
}

/** 更新地图落点（拖拽）—— 仅前端位置同步，不触发后端（文档无地理字段） */
function setPosition(id, lng, lat) {
  // 只接受有限数值坐标，非法值不写入，避免后方渲染 NaN 像素
  if (typeof lng !== 'number' || typeof lat !== 'number' ||
      !Number.isFinite(lng) || !Number.isFinite(lat)) {
    console.warn('[useDevices] 落点坐标无效，忽略更新：', id, lng, lat)
    return
  }
  manualPos.add(id) // 标记手动拖拽，WS 经纬度不再覆盖
  state.positions[id] = { lng, lat }
}

function select(id) { state.selectedId = id }

/** 合并后的设备视图（含实时计数） */
function merged() {
  return state.devices.map((d) => ({ ...d, stat: state.statsById[d.id] || null }))
}

export function useDevices() {
  return { state, load, refreshStats, applyWsDevices, create, remove, configure, setPosition, select, merged }
}
