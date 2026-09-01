<template>
  <div>
    <div id="container"></div>

    <!-- 添加模式提示 -->
    <div class="op-tip" v-if="addMode">📍 添加模式：点击地图选择设备位置（Esc 取消）</div>

    <!-- 设备编辑 / 添加弹窗 -->
    <DeviceEditor :open="editorOpen" :mode="editorMode" :device="editorDevice" :pos="editorPos"
      @close="editorOpen = false" @save="onEditorSave" @remove="onEditorRemove" />
  </div>
</template>

<script setup>
import { onMounted, onBeforeUnmount, ref, computed, watch } from 'vue'
import L from 'leaflet'
import { useDevices } from '../composables/useDevices.js'
import { useMapControl } from '../composables/useMapControl.js'
import { useToast } from '../composables/useToast.js'
import { useTheme } from '../composables/useTheme.js'
import { CITY_WALL_POINTS, sortPointsByPriority } from '../data/cityWallPoints.js'
import { TipLayer } from '../utils/tipLayer.js'
import DeviceEditor from './DeviceEditor.vue'

const { state: dev, load, create, remove, configure, setPosition, select } = useDevices()
const { mapCtl } = useMapControl()
const { push } = useToast()
const { theme, mapStyle, overlayColors } = useTheme()

// —— 坐标转换工具（Leaflet 使用 [lat, lng]，数据使用 [lng, lat]）——
function toLatLng(p) { return [p[1], p[0]] }
function toLatLngArr(pts) { return pts.map(toLatLng) }

// —— 瓦片图层配置（通过环境变量配置内网瓦片服务器地址）——
// 深色/浅色可分别配置，未配置时默认使用同一地址
const TILE_URL_LIGHT = import.meta.env.VITE_TILE_URL_LIGHT || import.meta.env.VITE_TILE_URL || 'http://localhost:8080/tiles/{z}/{x}/{y}.png'
const TILE_URL_DARK  = import.meta.env.VITE_TILE_URL_DARK  || import.meta.env.VITE_TILE_URL || 'http://localhost:8080/tiles/{z}/{x}/{y}.png'
const TILE_ATTR = import.meta.env.VITE_TILE_ATTR || ''

// 1×1 透明兜底瓦片：未下载到的格子显示容器底色，而非破图白块（主题匹配关键）
const ERROR_TILE =
  'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC'

let tileLayerLight = null  // 浅色瓦片层
let tileLayerDark = null   // 深色瓦片层

// —— 城市配置 ——
function computeHull(pts) {
  const p = pts.slice().sort((a, b) => a[0] - b[0] || a[1] - b[1])
  const n = p.length
  if (n < 3) return p.slice()
  const cross = (O, A, B) => (A[0] - O[0]) * (B[1] - O[1]) - (A[1] - O[1]) * (B[0] - O[0])
  const lower = []
  for (let i = 0; i < n; i++) {
    while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p[i]) <= 0) lower.pop()
    lower.push(p[i])
  }
  const upper = []
  for (let i = n - 1; i >= 0; i--) {
    while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], p[i]) <= 0) upper.pop()
    upper.push(p[i])
  }
  return lower.slice(0, -1).concat(upper.slice(0, -1))
}
function boundsToPolygon(b) {
  const [sw, ne] = b
  return [[sw[0], sw[1]], [ne[0], sw[1]], [ne[0], ne[1]], [sw[0], ne[1]]]
}
const CITY = {
  yungangshiku: {
    center: [113.13589, 40.111345], zoom: 15,
    bounds: [[113.12589, 40.101345], [113.14589, 40.121345]],
    polygon: boundsToPolygon([[113.12589, 40.101345], [113.14589, 40.121345]])
  },
  datong: {
    center: [113.3025, 40.09325],
    zoom: 15.68,
    bounds: [[113.289814, 40.083292], [113.315134, 40.103218]],
    polygon: [
      [113.2899, 40.083292],
      [113.289814, 40.103218],
      [113.315048, 40.103089],
      [113.315134, 40.083293]
    ]
  },
  xian: {
    center: [108.9540, 34.2650], zoom: 14,
    bounds: [[108.9440, 34.2550], [108.9640, 34.2750]],
    polygon: boundsToPolygon([[108.9440, 34.2550], [108.9640, 34.2750]])
  }
}
const CITY_NAME = { datong: '大同 · 古城', yungangshiku: '大同 · 云冈石窟', xian: '西安 · 雁塔' }
const DEFAULT_CITY = 'datong'
let current = DEFAULT_CITY

let map = null
let regionBorder = null     // 区域主边界
let regionGlow = null       // 边界外发光层
let regionMask = null       // 圈外遮罩层
let regionLabel = null      // 区域标签
let regionHit = null        // 区域透明命中面
let flowTipLayer = null     // 默认车流提示框层
let clickTipLayer = null    // 点击交互提示框层
const markerMap = {}        // deviceId -> L.marker

const selectedRegion = ref(null)
const pointMarkers = []
const selectedPointId = ref(null)

const addMode = ref(false)
const editorOpen = ref(false)
const editorMode = ref('add')
const editorDevice = ref(null)
const editorPos = ref(null)

// —— 工具函数 ——
function statusColor(s) {
  return s === 'online' ? 'g' : (s === 'offline' ? 'r' : 'y')
}
function congColorClass(score) {
  if (score >= 0.66) return 'r'
  if (score >= 0.33) return 'y'
  return 'g'
}
function flowColorCls(c) {
  if (c === 'r') return 'red'
  if (c === 'y') return 'yellow'
  if (c === 'g') return 'green'
  return 'gray'
}

function gateOf(name) {
  if (!name) return '大同古城'
  for (const [g, kw] of [['和阳门', '和阳'], ['永泰门', '永泰'], ['清远门', '清远'], ['武定门', '武定']]) {
    if (name.includes(kw)) return g
  }
  return '大同古城'
}
function dirOf(name) {
  if (name && name.includes('入口')) return '入口'
  if (name && name.includes('出口')) return '出口'
  return '其他'
}
function backendWallPoints() {
  const list = []
  dev.devices.forEach((d) => {
    if (!d || !d.id) return
    if (!dev.showOffline && d.status !== 'online') return
    const pos = dev.positions[d.id]
    if (!pos || !Number.isFinite(pos.lng) || !Number.isFinite(pos.lat)) return
    const isKakou = d.camera_type === 'vehicle' || /卡口/.test(d.category || '')
    list.push({
      id: d.id, desc: d.name || d.id,
      type: isKakou ? '卡口' : '便道',
      gate: gateOf(d.name), direction: dirOf(d.name),
      coord: [pos.lng, pos.lat], lastActive: '',
      status: d.status || 'syncing',
      typeLabel: isKakou ? '车行' : '人行',
      count: 0, category: d.category || '', camera_type: d.camera_type || ''
    })
  })
  return list
}
const wallPoints = computed(() => {
  const back = backendWallPoints()
  return back.length ? back : CITY_WALL_POINTS
})
const WALL_IDS = computed(() => new Set(wallPoints.value.map((p) => p.id)))
function findPoint(id) { return wallPoints.value.find((p) => p.id === id) }

function statusTextOf(level, isVeh) {
  if (level === 'offline') return '离线'
  if (level === 'abnormal') return '异常'
  if (level === 'severe') return isVeh ? '严重拥堵' : '严重拥挤'
  if (level === 'congested') return '拥堵'
  if (level === 'slow' || level === 'crowd') return isVeh ? '缓行' : '拥挤'
  return '畅通'
}

let hoverPointId = null

function resolvePointInfo(p) {
  const isVeh = p.type === '卡口'
  const ws = dev.statsById[p.id] || null
  if (ws) {
    const status = ws.status || p.status
    if (status !== 'online') {
      return {
        fromWs: true, source: 'ws', isVeh, kind: isVeh ? 'veh' : 'per',
        status, abnormal: true,
        current: 0, daily: 0, dailyIn: 0, dailyOut: 0, hourly: 0, hourlyIn: 0, hourlyOut: 0,
        congScore: 0, congCls: 'gray', level: status === 'offline' ? 'offline' : 'abnormal',
        vehicleFlowPerMin: 0, personFlowPerMin: 0,
        vehicleCongested: false, personCongested: false,
        hour: ws.hour || new Date().getHours(), congestionRaw: false
      }
    }
    const current = isVeh ? (ws.current_vehicles || 0) : (ws.current_persons || 0)
    const dailyIn = isVeh ? (ws.today_vehicle_in || 0) : (ws.today_person_in || 0)
    const dailyOut = isVeh ? (ws.today_vehicle_out || 0) : (ws.today_person_out || 0)
    const daily = dailyIn + dailyOut
    const hourlyIn = isVeh ? (ws.hour_vehicle_in || 0) : (ws.hour_person_in || 0)
    const hourlyOut = isVeh ? (ws.hour_vehicle_out || 0) : (ws.hour_person_out || 0)
    const hourly = hourlyIn + hourlyOut
    const congScore = ws.congestion_score || 0
    const congCls = congScore >= 0.66 ? 'r' : (congScore >= 0.33 ? 'y' : 'g')
    const vCong = !!ws.vehicle_congested
    const pCong = !!ws.person_congested
    const dimCong = isVeh ? vCong : pCong
    let level
    if (dimCong) level = 'severe'
    else if (congScore >= 0.66) level = 'congested'
    else if (congScore >= 0.33) level = 'slow'
    else level = 'free'
    return {
      fromWs: true, source: 'ws', isVeh, kind: isVeh ? 'veh' : 'per',
      status, abnormal: false,
      current, daily, dailyIn, dailyOut, hourly, hourlyIn, hourlyOut,
      congScore, congCls, level,
      vehicleFlowPerMin: ws.vehicle_flow_per_min || 0,
      personFlowPerMin: ws.person_flow_per_min || 0,
      vehicleCongested: vCong, personCongested: pCong,
      hour: ws.hour || new Date().getHours(), congestionRaw: !!ws.congested
    }
  }
  const status = p.status || 'abnormal'
  return {
    fromWs: false, source: 'fallback', isVeh, kind: isVeh ? 'veh' : 'per',
    status: status === 'online' ? 'abnormal' : status, abnormal: true,
    current: 0, daily: 0, dailyIn: 0, dailyOut: 0, hourly: 0, hourlyIn: 0, hourlyOut: 0,
    congScore: 0, congCls: 'gray', level: status === 'offline' ? 'offline' : 'abnormal',
    vehicleFlowPerMin: 0, personFlowPerMin: 0,
    vehicleCongested: false, personCongested: false, hour: new Date().getHours(), congestionRaw: false
  }
}

// —— 设备标点渲染（Leaflet DivIcon）——
function renderDevices() {
  if (!map) return
  Object.values(markerMap).forEach((m) => map.removeLayer(m))
  for (const k in markerMap) delete markerMap[k]

  dev.devices.forEach((d) => {
    if (WALL_IDS.value.has(d.id)) return
    const pos = dev.positions[d.id] || CITY[current].center
    if (!pos ||
      typeof pos.lng !== 'number' || typeof pos.lat !== 'number' ||
      !Number.isFinite(pos.lng) || !Number.isFinite(pos.lat)) {
      console.warn('[renderDevices] 设备坐标无效，已跳过：', d.id, pos)
      return
    }
    const stat = dev.statsById[d.id]
    const sel = dev.selectedId === d.id
    const score = stat ? (stat.congestion_score || 0) : 0
    const congCls = congColorClass(score)
    const cnt = stat
      ? (d.camera_type === 'person'
        ? `👤${stat.current_persons}`
        : `🚗${stat.current_vehicles}`)
      : ''
    const cat = d.category ? d.category : ''
    const content =
      `<div class="mk ${sel ? 'sel' : ''} cong-${congCls}">` +
      `<div class="pin"><i>📷</i></div>` +
      `<div class="lbl">${d.name} <span class="dot ${statusColor(d.status)}"></span> ${cnt}</div>` +
      (cat ? `<div class="mk-cat" title="${cat}">${cat}</div>` : '') +
      `</div>`
    const mk = L.marker([pos.lat, pos.lng], {
      icon: L.divIcon({ className: 'custom-device-icon', html: content, iconSize: [0, 0], iconAnchor: [0, 0] }),
      zIndexOffset: sel ? 200 : 150, draggable: true
    })
    mk.on('click', () => openEdit(d))
    mk.on('dragend', (e) => {
      const ll = mk.getLatLng()
      setPosition(d.id, ll.lng, ll.lat)
      push(`已更新「${d.name}」地图位置`, 'info')
    })
    mk.addTo(map)
    markerMap[d.id] = mk
  })
}

// —— 编辑 / 添加 ——
function openEdit(d) {
  select(d.id)
  editorMode.value = 'edit'
  editorDevice.value = d
  editorPos.value = dev.positions[d.id] || null
  editorOpen.value = true
}
function startAdd() {
  addMode.value = !addMode.value
  if (addMode.value) {
    document.body.classList.add('adding')
    push('📍 添加模式：点击地图选择设备位置', 'info')
  } else {
    document.body.classList.remove('adding')
  }
}
function onMapClick(e) {
  if (addMode.value) {
    const ll = e.latlng
    editorMode.value = 'add'
    editorDevice.value = null
    editorPos.value = { lng: ll.lng, lat: ll.lat }
    editorOpen.value = true
    addMode.value = false
    document.body.classList.remove('adding')
    return
  }
  if (selectedPointId.value) selectPoint(null)
}
async function onEditorSave({ mode, id, payload, pos }) {
  if (mode === 'add') {
    if (!payload.id || !payload.name || !payload.stream_url) {
      push('设备 ID / 名称 / 流地址 为必填', 'warn'); return
    }
    const ok = await create(payload, pos)
    if (ok) editorOpen.value = false
  } else {
    const ok = await configure(id, {
      line_coords: payload.line_coords, anchor_coords: payload.anchor_coords,
      count_only: payload.count_only, camera_type: payload.camera_type, roi_coords: payload.roi_coords
    })
    if (ok) editorOpen.value = false
  }
}
async function onEditorRemove(id) {
  const ok = await remove(id)
  if (ok) editorOpen.value = false
}

const camIcon = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="7" width="13" height="8" rx="2"/><rect x="14" y="9" width="4" height="4" rx="1"/><circle cx="18" cy="11" r="1.8" fill="currentColor" stroke="none"/><path d="M9 15v3M7 18h5"/><circle cx="5.5" cy="10" r="0.7" fill="currentColor" stroke="none"/></svg>`
const placeholderIcon = `<svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="7" width="13" height="8" rx="2"/><rect x="14" y="9" width="4" height="4" rx="1"/><circle cx="18" cy="11" r="1.8" fill="currentColor" stroke="none"/><path d="M9 15v3M7 18h5"/><circle cx="5.5" cy="10" r="0.7" fill="currentColor" stroke="none"/></svg>`

function clearPointMarkers() {
  if (!map) return
  pointMarkers.forEach((it) => map.removeLayer(it.marker))
  pointMarkers.length = 0
}

function buildPinContent(p, mode = false) {
  const typeCls = p.type === '卡口' ? 'kakou' : 'biandao'
  const info = resolvePointInfo(p)
  const cls = mode === 'active' ? ' active' : (mode === 'hover' ? ' hover' : '')
  const colorCls = info.congCls === 'r' ? 'red' : (info.congCls === 'y' ? 'yellow' : 'green')
  const stCls = info.status === 'online' ? 'online'
    : (info.status === 'offline' ? 'offline'
      : (info.status === 'abnormal' ? 'abnormal' : 'syncing'))
  return (
    `<div class="cam-pin ${typeCls} s-${stCls}${cls}">` +
    `<div class="pin">${camIcon}</div>` +
    `<span class="badge dot flow-${colorCls}"></span>` +
    `</div>`
  )
}

function resetPinActive() {
  pointMarkers.forEach((it) => {
    const p = findPoint(it.id)
    if (p) setMarkerMode(it.marker, p, false)
  })
}

function refreshWallPointPins() {
  pointMarkers.forEach((it) => {
    const p = findPoint(it.id)
    if (!p) return
    const mode = selectedPointId.value === p.id ? 'active' : (hoverPointId === p.id ? 'hover' : false)
    setMarkerMode(it.marker, p, mode)
  })
}

// —— 区域圈地可视化（Leaflet Polygon）——
function renderRegionOverlay() {
  if (!map) return
  ;[regionBorder, regionGlow, regionMask, regionLabel, regionHit].forEach((m) => {
    if (m) try { map.removeLayer(m) } catch (e) { }
  })
  regionBorder = regionGlow = regionMask = regionLabel = regionHit = null

  const poly = CITY[current] && CITY[current].polygon
  if (!poly || poly.length < 3) return
  const c = overlayColors.value
  const center = CITY[current].center

  // 1) 圈外遮罩层：外大环 + 城市多边形做洞（Leaflet 原生支持多环镂空）
  const d = 3
  const outer = [
    toLatLng([center[0] - d, center[1] - d]),
    toLatLng([center[0] + d, center[1] - d]),
    toLatLng([center[0] + d, center[1] + d]),
    toLatLng([center[0] - d, center[1] + d])
  ]
  regionMask = L.polygon([outer, toLatLngArr(poly)], {
    color: 'transparent', fillColor: c.maskFill, fillOpacity: c.maskOp,
    interactive: false, pane: 'overlayPane'
  }).addTo(map)

  // 2) 边界外发光层
  regionGlow = L.polygon(toLatLngArr(poly), {
    color: c.regionStroke, weight: 7, opacity: 0.28, fill: false,
    interactive: false, pane: 'overlayPane'
  }).addTo(map)

  // 3) 主边界
  regionBorder = L.polygon(toLatLngArr(poly), {
    color: c.regionStroke, weight: 2.5, opacity: c.regionStrokeOp,
    fillColor: c.polyFill, fillOpacity: c.polyFillOp,
    interactive: false, pane: 'overlayPane'
  }).addTo(map)

  // 4) 区域标签
  const cy = poly.reduce((s, p) => s + p[1], 0) / poly.length
  regionLabel = L.marker(toLatLng([center[0], cy + 0.0009]), {
    icon: L.divIcon({
      className: 'region-label-icon',
      html: `<div class="region-label">🏯 ${CITY_NAME[current] || current} · 监控区域</div>`,
      iconSize: [0, 0], iconAnchor: [0, 0]
    }),
    interactive: false, zIndexOffset: 200
  }).addTo(map)

  // 5) 区域选中交互层
  const isSel = selectedRegion.value === current
  regionHit = L.polygon(toLatLngArr(poly), {
    color: 'transparent', weight: 0,
    fillColor: isSel ? c.regionStroke : 'transparent',
    fillOpacity: isSel ? 0.06 : 0,
    interactive: true
  }).addTo(map)
  regionHit.on('click', () => selectRegion(current))
  regionHit.on('mouseover', () => { if (selectedRegion.value !== current) setRegionHover(true) })
  regionHit.on('mouseout', () => setRegionHover(false))

  if (isSel) applyRegionSelectedStyle()
}

function applyRegionSelectedStyle() {
  const c = overlayColors.value
  if (regionBorder) {
    try { regionBorder.setStyle({ weight: 4, opacity: 1, color: c.regionStroke }) } catch (e) { }
  }
  if (regionGlow) {
    try { regionGlow.setStyle({ weight: 10, opacity: 0.5 }) } catch (e) { }
  }
}

let _hoverTimer = null
function setRegionHover(on) {
  if (!regionBorder) return
  try {
    regionBorder.setStyle({ opacity: on ? 0.85 : (selectedRegion.value ? 1 : overlayColors.value.regionStrokeOp) })
  } catch (e) { }
}

function selectRegion(key) {
  if (selectedRegion.value === key) {
    selectedRegion.value = null
    push('已取消区域选中', 'info')
  } else {
    selectedRegion.value = key
    const name = CITY_NAME[key] || key
    push('已选中区域：' + name + ' · 监控区域', 'success')
    onRegionSelected && onRegionSelected(key)
  }
  if (map) renderRegionOverlay()
}

let onRegionSelected = null
function setRegionSelectCallback(fn) { onRegionSelected = fn }

// —— 点位提示框 HTML（与 AMap 版本保持一致）——
function buildTipHtml(p) {
  const info = resolvePointInfo(p)
  const statusText = info.status === 'online' ? '在线'
    : (info.status === 'offline' ? '离线'
      : (info.status === 'abnormal' ? '异常' : '待验证'))
  const countLabel = p.typeLabel === '人行' ? '人流' : '车流'
  const typeBadge = p.type === '卡口'
    ? `<span class="tip-badge tip-badge-kakou">卡口</span>`
    : `<span class="tip-badge tip-badge-biandao">便道</span>`
  const dirIcon = p.direction === '入口'
    ? `<svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M8 2v10M4 6l4-4 4 4M3 14h10"/></svg>`
    : p.direction === '出口'
    ? `<svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M8 14V4M4 10l4 4 4-4M3 2h10"/></svg>`
    : `<svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="8" cy="8" r="5"/><path d="M8 5v3l2 1"/></svg>`
  const gateIcon = `<svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 14V6l6-4 6 4v8M5 14V9h6v5"/></svg>`
  const coordIcon = `<svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="7" r="2.5"/><path d="M8 1.5C5 1.5 3 4 3 7c0 3 5 7.5 5 7.5s5-4.5 5-7.5c0-3-2-5.5-5-5.5z"/></svg>`
  const idIcon = `<svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="3" width="12" height="10" rx="2"/><circle cx="6" cy="8" r="1.5"/><path d="M9 7h3M9 10h3"/></svg>`
  const activeIcon = `<svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="8" r="6"/><path d="M8 5v3l2 2"/></svg>`
  const media = `<div class="cam-placeholder"><div class="cam-scan"></div><div class="ph-ic">${placeholderIcon}</div><span>暂无视频流</span></div>`
  const lng = p.coord[0].toFixed(6)
  const lat = p.coord[1].toFixed(6)
  const trend = info.current > 80 ? 'up' : (info.current > 30 ? 'flat' : 'down')
  const trendIcon = trend === 'up' ? '▲' : (trend === 'down' ? '▼' : '◆')
  const trendPct = trend === 'up'
    ? `+${Math.min((info.current % 40) + 5, 45)}%`
    : (trend === 'down' ? `-${(info.current % 15) + 3}%` : '0%')
  return (
    `<div class="cam-info cam-info-static tip-monitor">` +
    `<div class="tip-head tip-head-glow">` +
    `<div class="tip-head-left">` +
    `<span class="tip-led" data-status="${info.status}"></span>` +
    `<div class="tip-head-title">` +
    `<h4>${p.desc}</h4>` +
    `<div class="tip-head-badges">${typeBadge}</div>` +
    `</div>` +
    `</div>` +
    `<div class="tip-head-status">` +
    `<span class="dot ${statusColor(info.status)}"></span>` +
    `<span class="tip-status-text">${statusText}</span>` +
    `</div>` +
    `</div>` +
    `<div class="tip-row tip-row-monitor">` +
    `<span class="tip-ic">${dirIcon}</span>` +
    `<span class="tip-lbl">路段</span>` +
    `<span class="tip-val tip-val-monitor">${p.gate} · ${p.direction}</span>` +
    `<span class="tip-led tip-led-sm" data-status="${info.status}"></span>` +
    `</div>` +
    `<div class="tip-body">` +
    `<div class="cam-info-left">` +
    `<div class="tip-field"><span class="tip-ic">${idIcon}</span><span class="tip-lbl">编号</span><span class="tip-val">${p.id}</span></div>` +
    `<div class="tip-field"><span class="tip-ic">${coordIcon}</span><span class="tip-lbl">坐标</span><span class="tip-val tip-coord">${lng}, ${lat}</span></div>` +
    `<div class="tip-field"><span class="tip-ic">${activeIcon}</span><span class="tip-lbl">活跃</span><span class="tip-val">${p.lastActive}</span></div>` +
    (() => {
      const f = resolvePointInfo(p)
      const isVeh = f.isVeh
      const colorCls = flowColorCls(f.congCls)
      const statusText = statusTextOf(f.level, isVeh)
      if (f.abnormal) {
        return (
          `<div class="tip-flow tip-flow-veh">` +
          `<div class="tip-flow-title">📡 设备状态</div>` +
          `<div class="tip-flow-row"><span class="tip-flow-k">状态</span><span class="tip-flow-v"><b>${statusText}</b>（无实时数据）</span></div>` +
          `<div class="tip-flow-status tip-flow-status-gray">${statusText}</div>` +
          `</div>`
        )
      }
      if (isVeh) {
        return (
          `<div class="tip-flow tip-flow-veh">` +
          `<div class="tip-flow-title">🚗 车流量详情</div>` +
          `<div class="tip-flow-row"><span class="tip-flow-k">日过车(veh/d)</span><span class="tip-flow-v"><b>${f.daily.toLocaleString()}</b> veh/D</span></div>` +
          `<div class="tip-flow-row"><span class="tip-flow-k">小时过车(veh/h)</span><span class="tip-flow-v"><b>${f.hourly.toLocaleString()}</b> veh/H</span></div>` +
          `<div class="tip-flow-row"><span class="tip-flow-k">实时计数(veh)</span><span class="tip-flow-v"><b>${f.current}</b> veh</span></div>` +
          `<div class="tip-flow-row"><span class="tip-flow-k">车辆速率(辆/min)</span><span class="tip-flow-v"><b>${f.vehicleFlowPerMin}</b></span></div>` +
          `<div class="tip-flow-row"><span class="tip-flow-k">统计小时</span><span class="tip-flow-v"><b>${f.hour}:00</b></span></div>` +
          `<div class="tip-flow-status tip-flow-status-${colorCls}">${statusText}</div>` +
          `</div>`
        )
      }
      const perMin = Math.round((f.personFlowPerMin || 0))
      return (
        `<div class="tip-flow tip-flow-per">` +
        `<div class="tip-flow-title">🚶 人流量详情</div>` +
        `<div class="tip-flow-row"><span class="tip-flow-k">日过人 (per/d)</span><span class="tip-flow-v"><b>${f.daily.toLocaleString()}</b></span></div>` +
        `<div class="tip-flow-row"><span class="tip-flow-k">小时过人 (per/h)</span><span class="tip-flow-v"><b>${f.hourly.toLocaleString()}</b></span></div>` +
        `<div class="tip-flow-row"><span class="tip-flow-k">分钟人流 (per/min)</span><span class="tip-flow-v"><b>${perMin}</b></span></div>` +
        `<div class="tip-flow-row"><span class="tip-flow-k">实时计数 (per)</span><span class="tip-flow-v"><b>${f.current}</b></span></div>` +
        `<div class="tip-flow-row"><span class="tip-flow-k">统计小时</span><span class="tip-flow-v"><b>${f.hour}:00</b></span></div>` +
        `<div class="tip-flow-status tip-flow-status-${colorCls}">${statusText}</div>` +
        `</div>`
      )
    })() +
    `</div>` +
    `<div class="cam-sep"></div>` +
    `<div class="cam-info-right">${media}</div>` +
    `</div>` +
    `</div>`
  )
}

function buildFlowTipHtml(p) {
  const info = resolvePointInfo(p)
  const isVeh = info.isVeh
  const colorCls = flowColorCls(info.congCls)
  const statusText = statusTextOf(info.level, isVeh)
  const typeBadge = p.type === '卡口'
    ? `<span class="flow-tag flow-tag-kakou">卡口</span>`
    : `<span class="flow-tag flow-tag-biandao">便道</span>`
  const dailyLabel = isVeh ? '日过车(veh/d)' : '日过人(per/d)'
  const hourlyLabel = isVeh ? '时过车(veh/h)' : '时过人(per/h)'
  const curUnit = isVeh ? 'veh' : 'per'
  if (info.abnormal) {
    return (
      `<div class="cam-flow flow-compact tip-monitor" data-flow="${colorCls}" data-kind="${info.kind}" data-id="${p.id}">` +
      `<div class="flow-head">` +
      `<span class="flow-dot flow-dot-${colorCls}"></span>` +
      `<span class="flow-title" title="${p.desc}">${p.desc}</span>` +
      typeBadge +
      `<span class="flow-pill flow-pill-${colorCls}">${statusText}</span>` +
      `</div>` +
      `<div class="flow-metrics">` +
      `<span class="fm"><b>—</b><i>无实时数据</i></span>` +
      `</div>` +
      `</div>`
    )
  }
  return (
    `<div class="cam-flow flow-compact tip-monitor" data-flow="${colorCls}" data-kind="${info.kind}" data-id="${p.id}">` +
    `<div class="flow-head">` +
    `<span class="flow-dot flow-dot-${colorCls}"></span>` +
    `<span class="flow-title" title="${p.desc}">${p.desc}</span>` +
    typeBadge +
    `<span class="flow-pill flow-pill-${colorCls}">${statusText}</span>` +
    `</div>` +
    `<div class="flow-metrics">` +
    `<span class="fm"><b data-fld="daily">${info.daily.toLocaleString()}</b><i>${dailyLabel}</i></span>` +
    `<span class="fm-sep"></span>` +
    `<span class="fm"><b data-fld="hourly">${info.hourly.toLocaleString()}</b><i>${hourlyLabel}</i></span>` +
    `<span class="fm-sep"></span>` +
    `<span class="fm"><b data-fld="current">${info.current}</b><i>${curUnit}</i></span>` +
    `<span class="fm-sep"></span>` +
    `<span class="fm fpm"><b data-fld="fpm">${info.isVeh ? info.vehicleFlowPerMin : info.personFlowPerMin}</b><i title="${info.isVeh ? '车辆速率(辆/min)' : '人流速率(人/min)'}">速率</i></span>` +
    `</div>` +
    `</div>`
  )
}

// —— 点位标记：Leaflet DivIcon ——
const ICON_CAR = `<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><path d="M5 11l1.5-4.6A2 2 0 0 1 8.4 5h7.2a2 2 0 0 1 1.9 1.4L19 11h1a1 1 0 0 1 1 1v3a1 1 0 0 1-1 1h-1v1a1 1 0 0 1-2 0v-1H7v1a1 1 0 0 1-2 0v-1H4a1 1 0 0 1-1-1v-3a1 1 0 0 1 1-1h1zm2.2-.6L6 13h12l-1.2-2.6A.8.8 0 0 0 16 9.8H8a.8.8 0 0 0-.8.6zM7.5 15.2a1.2 1.2 0 1 0 0 .01zM16.5 15.2a1.2 1.2 0 1 0 0 .01z"/></svg>`
const ICON_USER = `<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><path d="M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zm0 2c-4 0-7 2-7 5v1h14v-1c0-3-3-5-7-5z"/></svg>`

function markerTitle(p) {
  const info = resolvePointInfo(p)
  const st = info.status === 'online' ? '在线'
    : info.status === 'abnormal' ? '异常'
      : info.status === 'offline' ? '离线' : '待验证'
  return `${p.desc}｜${p.gate}·${p.direction}｜${st}`
}

function createPointMarker(p) {
  const info = resolvePointInfo(p)
  const baseZ = p.type === '卡口' ? 280 : 250
  const statZ = info.status === 'online' ? 20 : (info.status === 'syncing' ? 10 : 0)
  return L.marker(toLatLng(p.coord), {
    icon: L.divIcon({
      className: 'custom-pin-icon',
      html: buildPinContent(p, false),
      iconSize: [34, 34],
      iconAnchor: [17, 17]
    }),
    zIndexOffset: baseZ + statZ,
    title: markerTitle(p)
  })
}

function setMarkerMode(mk, p, mode) {
  if (!mk) return
  try {
    mk.setIcon(L.divIcon({
      className: 'custom-pin-icon',
      html: buildPinContent(p, mode),
      iconSize: [34, 34],
      iconAnchor: [17, 17]
    }))
  } catch (e) { }
}

// —— 点击交互提示框 ——
function updateClickTip() {
  if (!map || !clickTipLayer) return
  if (current !== 'datong' || !selectedPointId.value) { clickTipLayer.update([]); return }
  const p = findPoint(selectedPointId.value)
  if (!p) { clickTipLayer.update([]); return }
  const pt = map.latLngToContainerPoint(L.latLng(p.coord[1], p.coord[0]))
  clickTipLayer.update([{ id: p.id, x: pt.x, y: pt.y, html: buildTipHtml(p) }])
}
function closeClickTip() {
  if (clickTipLayer) clickTipLayer.update([])
}

// —— 默认车流提示框 ——
function openFlowTips() {
  closeFlowTips()
  if (!map || current !== 'datong') return
  const pts = wallPoints.value
  if (!pts || pts.length === 0) return
  if (!flowTipLayer) return
  updateFlowTips()
}
function closeFlowTips() {
  if (flowTipLayer) flowTipLayer.update([])
}
function updateFlowTips() {
  if (!map || !flowTipLayer) return
  if (current !== 'datong') { flowTipLayer.update([]); return }
  const pts = wallPoints.value
  if (!pts || pts.length === 0) { flowTipLayer.update([]); return }
  const items = []
  const sorted = sortPointsByPriority(pts)
  for (const p of sorted) {
    if (!p.coord || !isFinite(p.coord[0]) || !isFinite(p.coord[1])) continue
    const pt = map.latLngToContainerPoint(L.latLng(p.coord[1], p.coord[0]))
    items.push({ id: p.id, x: pt.x, y: pt.y, html: buildFlowTipHtml(p) })
  }
  flowTipLayer.update(items)
}
function updateFlowTipContent() {
  if (!flowTipLayer) return
  flowTipLayer.updateFlowText((card, id) => {
    const p = findPoint(id)
    if (!p) return
    const info = resolvePointInfo(p)
    const colorCls = flowColorCls(info.congCls)
    const statusText = statusTextOf(info.level, info.isVeh)
    card.setAttribute('data-flow', colorCls)
    const dot = card.querySelector('.flow-dot'); if (dot) dot.className = 'flow-dot flow-dot-' + colorCls
    const setFld = (k, v) => { const el = card.querySelector('[data-fld="' + k + '"]'); if (el) el.textContent = v }
    setFld('daily', info.daily.toLocaleString())
    setFld('hourly', info.hourly.toLocaleString())
    setFld('current', info.current)
    setFld('fpm', info.isVeh ? info.vehicleFlowPerMin : info.personFlowPerMin)
    const st = card.querySelector('.flow-status'); if (st) { st.className = 'flow-status flow-status-' + colorCls; st.textContent = statusText }
    const pill = card.querySelector('.flow-pill'); if (pill) { pill.className = 'flow-pill flow-pill-' + colorCls; pill.textContent = statusText }
  })
}

// —— 点位标注渲染 ——
function renderDefaultMarkers() {
  clearPointMarkers()
  if (!map) return
  if (current !== 'datong') return
  const pts = wallPoints.value
  if (!pts || pts.length === 0) {
    push('该区域暂无监控点位', 'warn'); return
  }
  try {
    const sorted = sortPointsByPriority(pts)
    sorted.forEach((p) => {
      if (!p.coord || !isFinite(p.coord[0]) || !isFinite(p.coord[1])) {
        console.warn('[renderDefaultMarkers] 点位坐标无效，已跳过：', p.id, p.coord)
        return
      }
      const mk = createPointMarker(p)
      mk.on('click', () => selectPoint(p.id))
      mk.on('mouseover', () => {
        hoverPointId = p.id
        if (selectedPointId.value !== p.id) setMarkerMode(mk, p, 'hover')
      })
      mk.on('mouseout', () => {
        hoverPointId = null
        if (selectedPointId.value !== p.id) setMarkerMode(mk, p, false)
      })
      mk.addTo(map)
      pointMarkers.push({ id: p.id, marker: mk })
    })
    if (selectedPointId.value) {
      pointMarkers.forEach((it) => {
        const p = findPoint(it.id)
        if (p) setMarkerMode(it.marker, p, it.id === selectedPointId.value)
      })
    }
  } catch (e) {
    console.error('[renderDefaultMarkers] 渲染失败：', e)
    push('点位渲染异常，请刷新重试', 'error')
  }
}

function selectPoint(id) {
  if (id == null) {
    selectedPointId.value = null
    resetPinActive()
    closeClickTip()
    return
  }
  if (selectedPointId.value === id) {
    selectedPointId.value = null
    resetPinActive()
    closeClickTip()
    return
  }
  selectedPointId.value = id
  pointMarkers.forEach((it) => {
    const p = findPoint(it.id)
    if (p) setMarkerMode(it.marker, p, it.id === id ? 'active' : false)
  })
  updateClickTip()
}

// —— 地图控制 ——
function changeCity(key) {
  current = key
  selectedPointId.value = null
  closeClickTip()
  if (!map) return
  renderRegionOverlay()
  const b = CITY[key].bounds
  if (b) {
    map.fitBounds(L.latLngBounds(L.latLng(b[0][1], b[0][0]), L.latLng(b[1][1], b[1][0])), { padding: [60, 60, 60, 60] })
  }
  clearPointMarkers()
  renderDefaultMarkers()
  openFlowTips()
  push('已定位至「' + (CITY_NAME[key] || key) + '」', 'info')
}
function changeStyle() {
  // 主题切换由 applyTheme 统一处理（切换瓦片层）
  applyTheme()
}

// —— 主题切换：切换瓦片层 + 更新叠加层颜色 ——
function applyTheme() {
  if (!map) return
  const el = document.getElementById('container')
  if (el) el.style.opacity = '0.35'

  // 切换瓦片层
  const isDark = theme.value === 'dark'
  if (tileLayerLight) {
    if (isDark) { map.removeLayer(tileLayerLight) } else { tileLayerLight.addTo(map) }
  }
  if (tileLayerDark) {
    if (isDark) { tileLayerDark.addTo(map) } else { map.removeLayer(tileLayerDark) }
  }

  const c = overlayColors.value
  if (regionMask) { try { regionMask.setStyle({ fillColor: c.maskFill, fillOpacity: c.maskOp }) } catch (e) { } }
  if (regionGlow) { try { regionGlow.setStyle({ color: c.regionStroke, opacity: 0.28 }) } catch (e) { } }
  if (regionBorder) {
    try {
      regionBorder.setStyle({
        color: c.regionStroke, opacity: c.regionStrokeOp, weight: 2.5,
        fillColor: c.polyFill, fillOpacity: c.polyFillOp
      })
    } catch (e) { }
  }
  if (flowTipLayer) flowTipLayer.setLineColor(c.regionStroke)
  if (clickTipLayer) clickTipLayer.setLineColor(c.regionStroke)
  if (el) requestAnimationFrame(() => { el.style.opacity = '1' })
}

function reprojectTips() {
  if (!map) return
  updateFlowTips(); updateClickTip()
}

// Leaflet 无 3D/俯仰角，以下方法保留为无操作（可被 TopBar 调用，不报错）
function resetView() { reprojectTips() }
function setTopView() { reprojectTips() }
function setOblique() { reprojectTips() }
function toggleFree3D() { /* Leaflet 不支持 3D */ }
function fitRange() {
  const b = CITY[current] && CITY[current].bounds
  if (!map || !b) return
  map.fitBounds(L.latLngBounds(L.latLng(b[0][1], b[0][0]), L.latLng(b[1][1], b[1][0])), { padding: [60, 60, 60, 60] })
  reprojectTips()
}

// —— 渲染静态叠加层 ——
function renderStaticLayers() {
  try { renderRegionOverlay() }
  catch (e) { console.error('[renderRegionOverlay] 失败：', e); push('区域边界渲染失败', 'error') }
  try { renderDefaultMarkers() } catch (e) { console.error('[renderDefaultMarkers] 失败：', e) }
  try { openFlowTips() } catch (e) { console.error('[openFlowTips] 失败：', e) }
}

// —— 定时刷新 ——
let flowTimer = null
function tickFlow() {
  pointMarkers.forEach((it) => {
    const p = findPoint(it.id)
    if (!p) return
    const mode = selectedPointId.value === p.id ? 'active' : (hoverPointId === p.id ? 'hover' : false)
    setMarkerMode(it.marker, p, mode)
  })
  updateFlowTipContent()
}
function startFlowTicker() {
  if (flowTimer) return
  flowTimer = setInterval(tickFlow, 5000)
}

// —— 生命周期 ——
onMounted(async () => {
  try {
    current = DEFAULT_CITY

    // 初始化地图（Leaflet 2D 视图）
    map = L.map('container', {
      zoom: CITY[current].zoom,
      center: toLatLng(CITY[current].center),
      minZoom: 9,        // 与已下载瓦片下限一致，避免缩太小无瓦片露白
      maxZoom: 17,       // 古城 z18 仅下载约 80%，封顶到覆盖完整的层级
      zoomControl: true,
      attributionControl: false,
      zoomEnable: true, dragEnable: true,
      zoomSnap: 0.25
    })

    // 创建瓦片层（浅色/深色各一，按主题切换可见性）
    tileLayerLight = L.tileLayer(TILE_URL_LIGHT, { attribution: TILE_ATTR, minZoom: 9, maxZoom: 18, errorTileUrl: ERROR_TILE, noWrap: true })
    tileLayerDark = L.tileLayer(TILE_URL_DARK, { attribution: TILE_ATTR, minZoom: 9, maxZoom: 18, errorTileUrl: ERROR_TILE, noWrap: true })
    if (theme.value === 'dark') {
      tileLayerDark.addTo(map)
    } else {
      tileLayerLight.addTo(map)
    }

    // 容器在挂载时可能尚未完成布局，强制重算尺寸，
    // 否则 Leaflet 会按初始 0 尺寸渲染，地图只显示一半 / 四周露白（"没铺满"主因）
    map.invalidateSize()
    setTimeout(() => { if (map) map.invalidateSize() }, 250)
    window.addEventListener('resize', () => { if (map) map.invalidateSize() })

    // 比例尺控件
    L.control.scale({ position: 'bottomleft', imperial: false }).addTo(map)

    map.on('click', onMapClick)

    // 注册到控制总线
    mapCtl.ready = true
    mapCtl.changeCity = changeCity
    mapCtl.changeStyle = changeStyle
    mapCtl.resetView = resetView
    mapCtl.setTopView = setTopView
    mapCtl.setOblique = setOblique
    mapCtl.fitRange = fitRange
    mapCtl.toggleFree3D = toggleFree3D
    mapCtl.startAdd = startAdd
    mapCtl.setRegionSelectCallback = setRegionSelectCallback

    // 挂载 TipLayer
    flowTipLayer = new TipLayer({
      lineColor: overlayColors.value.regionStroke,
      strokeWidth: 2, lineOpacity: 0.8, gap: 56,
      maxLine: 420, minimumSpacing: 16
    })
    const containerEl = document.getElementById('container')
    if (containerEl) {
      flowTipLayer.mount(containerEl)
      clickTipLayer = new TipLayer({
        lineColor: overlayColors.value.regionStroke,
        strokeWidth: 2, lineOpacity: 0.8, gap: 56,
        maxLine: 200, minimumSpacing: 12, zIndex: 9750
      })
      clickTipLayer.mount(containerEl)
    }

    // 渲染静态叠加层
    let staticDone = false
    const renderStaticOnce = () => {
      if (staticDone) return
      staticDone = true
      try { renderStaticLayers() } catch (e) { console.error('[renderStaticLayers] 失败：', e) }
    }
    setTimeout(renderStaticOnce, 500)

    // 容器尺寸修正
    requestAnimationFrame(() => {
      try { if (map) map.invalidateSize() } catch (e) { }
    })

    // 加载设备
    load()
      .then(() => { try { renderDevices() } catch (e) { console.error('[renderDevices]', e) } })
      .catch(() => { })

    // 地图事件：重投影 TipLayer
    map.whenReady(() => { renderStaticOnce(); updateFlowTips(); updateClickTip() })
    map.on('moveend', () => { updateFlowTips(); updateClickTip() })
    map.on('zoomend', () => { updateFlowTips(); updateClickTip() })
    map.on('resize', () => { updateFlowTips(); updateClickTip() })

    // 启动定时刷新
    startFlowTicker()
  } catch (e) {
    console.error(e)
    push('⚠ 地图初始化失败：' + (e && e.message ? e.message : e), 'error')
  }
})

onBeforeUnmount(() => {
  document.body.classList.remove('adding')
  clearPointMarkers()
  if (clickTipLayer) { clickTipLayer.destroy(); clickTipLayer = null }
  if (flowTipLayer) { flowTipLayer.destroy(); flowTipLayer = null }
  if (map) {
    if (regionBorder) try { map.removeLayer(regionBorder) } catch { }
    if (regionGlow) try { map.removeLayer(regionGlow) } catch { }
    if (regionMask) try { map.removeLayer(regionMask) } catch { }
    if (regionLabel) try { map.removeLayer(regionLabel) } catch { }
    if (regionHit) try { map.removeLayer(regionHit) } catch { }
    if (tileLayerLight) try { map.removeLayer(tileLayerLight) } catch { }
    if (tileLayerDark) try { map.removeLayer(tileLayerDark) } catch { }
    try { map.remove() } catch { }
  }
})

// 设备列表 / 选中变化 → 重绘标点
watch(
  () => dev.devices.map((d) => d.id + ':' + d.status).join('|') + '#' + dev.selectedId,
  () => renderDevices()
)
watch(() => dev.statsById, () => { renderDevices(); updateFlowTipContent(); refreshWallPointPins(); if (selectedPointId.value) updateClickTip() }, { deep: true })

// 点位源变化
watch(
  () => wallPoints.value.map((p) => p.id + ':' + p.status + ':' + (p.coord ? p.coord.join(',') : '')).join('|'),
  () => { renderDefaultMarkers(); openFlowTips() }
)

// 主题变化
watch(theme, () => applyTheme())
</script>