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
import { onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { useDevices } from '../composables/useDevices.js'
import { useMapControl } from '../composables/useMapControl.js'
import { useToast } from '../composables/useToast.js'
import { useTheme } from '../composables/useTheme.js'
import { CITY_WALL_POINTS, sortPointsByPriority } from '../data/cityWallPoints.js'
import { TipLayer } from '../utils/tipLayer.js'
// [临时停用] 点位到点位路径高亮功能 —— roadHighlight 渲染器已整体注释，此处 import 一并停用
// 恢复方式：取消下面一行的注释即可
// import { getRoadHighlight, buildAdjacencyEdges } from '../utils/roadHighlight.js'
import DeviceEditor from './DeviceEditor.vue'

const { state: dev, load, create, remove, configure, setPosition, select } = useDevices()
const { mapCtl } = useMapControl()
const { push } = useToast()
const { theme, mapStyle, overlayColors } = useTheme()

// —— 城市配置（与原大屏一致） ——
// 凸包算法（Andrew monotone chain）：用城墙点位反推大同古城真实轮廓
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
// bounds 矩形转 4 角多边形（云冈/西安：矩形近似区域）
function boundsToPolygon(b) {
  const [sw, ne] = b
  return [[sw[0], sw[1]], [ne[0], sw[1]], [ne[0], ne[1]], [sw[0], ne[1]]]
}
const CITY = {
  yungangshiku: {
    center: [113.13589, 40.111345], zoom: 15, pitch: 55,
    bounds: [[113.12589, 40.101345], [113.14589, 40.121345]],
    // 圈地多边形（矩形近似）
    polygon: boundsToPolygon([[113.12589, 40.101345], [113.14589, 40.121345]])
  },
  datong: {
    // 大同古城：以「古城范围.xlsx」4 角点经纬度构建默认选中区域
    center: [113.3025, 40.09325],
    zoom: 15.68,
    pitch: 55,
    bounds: [[113.289814, 40.083292], [113.315134, 40.103218]],
    // 圈地多边形：古城范围 4 角点（西南→西北→东北→东南，顺时针闭合）
    polygon: [
      [113.2899, 40.083292],
      [113.289814, 40.103218],
      [113.315048, 40.103089],
      [113.315134, 40.083293]
    ]
  },
  xian: {
    center: [108.9540, 34.2650], zoom: 14, pitch: 50,
    bounds: [[108.9440, 34.2550], [108.9640, 34.2750]],
    polygon: boundsToPolygon([[108.9440, 34.2550], [108.9640, 34.2750]])
  }
}
const CITY_NAME = { datong: '大同 · 古城', yungangshiku: '大同 · 云冈石窟', xian: '西安 · 雁塔' }
const DEFAULT_CITY = 'datong'   // 默认选中区域（初始化即应用，保证首屏有区域渲染）
let current = DEFAULT_CITY
let free3D = false

let map = null
let regionBorder = null     // 区域主边界（清晰描边 + 淡填充）
let regionGlow = null       // 边界外发光层（宽描边低透明，模拟荧光辉光）
let regionMask = null       // 圈外遮罩层（圈外变暗、圈内镂空，高德原生圈地质感）
let regionLabel = null      // 区域标签
let regionHit = null       // 区域透明命中面（拦截区域内点击用于选中，不穿透地图）
let flowTipLayer = null     // 默认车流提示框层（TipLayer DOM 层：SVG 连接线 + 卡片，多实例并存，带碰撞避让）
let simpleMarkerClass = null     // AMapUI SimpleMarker 类（字体图标标注）
let simpleInfoWindowClass = null // AMapUI SimpleInfoWindow 类（点击信息窗体）
let HAS_SIMPLE_MARKER = false   // 运行期探测：AMapUI SimpleMarker 是否可用（否则回退 AMap.Marker）
let HAS_SIMPLE_IW = false       // 运行期探测：AMapUI SimpleInfoWindow 是否可用（否则回退 AMap.InfoWindow）
let amapUIPromise = null        // AMapUI 模块加载 Promise（仅加载一次）
let clickTipLayer = null     // 点击交互提示框层（独立 TipLayer 实例，buildTipHtml 大卡，zIndex 高于 flowTipLayer）
// [临时停用] 点位到点位路径高亮功能 —— 渲染器实例变量已注释
// let roadHL = null          // 道路路径高亮渲染器（3D 立体多段折线，沿真实道路）
const markerMap = {} // deviceId -> AMap.Marker
// let roadInfoWindow = null  // 点击路径弹出的距离 InfoWindow

// 区域选中状态（地图交互核心）：记录当前选中的城市区域 key，驱动高亮与回调
const selectedRegion = ref(null)

// 默认城墙点位标记集合（按需点击触发 tip，不再常驻展示）
const pointMarkers = []
// 当前选中点位（点击触发 tip 后填充；点击空白/切换城市则清空）
const selectedPointId = ref(null)

// 本地交互状态
const addMode = ref(false)
const editorOpen = ref(false)
const editorMode = ref('add')
const editorDevice = ref(null)
const editorPos = ref(null)

// —— 工具 ——
function statusColor(s) {
  return s === 'online' ? 'g' : (s === 'offline' ? 'r' : 'y')
}

// 综合拥挤度(congestion_score, 0-1) → 着色档位（v0.11.0 WS 字段）
function congColorClass(score) {
  if (score >= 0.66) return 'r'
  if (score >= 0.33) return 'y'
  return 'g'
}

// —— 点位流量状态（按点位类型分流模拟实时）——
// 卡口 → 车辆数据：speed(km/h) / daily(veh/D) / hourly(veh/H) / current(veh)
// 便道 → 行人数据：speed(m/min) / daily(ped/d) / hourly(ped/h) / current(ped) / density(ped/m²) / flow=Q·K·v
// 点位数据本身无流量字段，按 id 哈希做确定性基准，再由定时器做轻微随机游走，
// 模拟「实时」流量变化；speed 按类型阈值 → 拥堵/通畅四档状态。（需求 #1+图片 1+2）
const VEH_FLOW_THRESHOLD = 28   // km/h，车辆低于此值判定拥堵
const PED_FLOW_THRESHOLD = 22   // m/min，行人低于此值判定拥堵
const flowState = {}            // id -> { ...type-specific }
let hoverPointId = null         // 当前悬浮点位（用于实时刷新时保留 hover 态）

function hashId(s) {
  let h = 0
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0
  return h
}
function seedFlow(p) {
  const h = hashId(p.id)
  if (p.type === '卡口') {
    flowState[p.id] = {
      kind: 'veh',
      speed: 18 + (h % 47),     // 18~64 km/h
      daily: 800 + (h % 9200),  // veh/D 800~10000
      hourly: 40 + (h % 360),   // veh/H 40~400
      current: 5 + (h % 55)     // 当前 veh 5~60
    }
  } else {
    // 便道 = 行人：v_p(m/min) / K_p(ped/m²) / Q_p = K_p · v_p (ped/h)
    const vp = 10 + (h % 45)            // 10~54 m/min
    const kp = 0.3 + ((h >> 4) % 27) / 10 // 0.3~3.0 ped/m²
    const qp = Math.round(kp * vp * 60)  // ped/h = K · v · 60
    flowState[p.id] = {
      kind: 'ped',
      speed: vp, daily: 2000 + (h % 48000), hourly: qp, current: 8 + (h % 120),
      density: kp, flow: qp
    }
  }
  return flowState[p.id]
}
function getFlow(p) {
  if (!flowState[p.id] || flowState[p.id].kind !== (p.type === '卡口' ? 'veh' : 'ped')) seedFlow(p)
  return flowState[p.id]
}
// 状态分级（四档）：畅通/缓行(拥挤)/拥堵/严重拥堵
function flowLevel(p) {
  const f = getFlow(p)
  const t = p.type === '卡口' ? VEH_FLOW_THRESHOLD : PED_FLOW_THRESHOLD
  // 车辆：<10 严重拥堵 / 10-20 拥堵 / 20-40 缓行 / >40 畅通（参考图片 1）
  // 行人：<10 严重拥挤 / 10-20 拥堵 / 20-40 拥挤(人多多走走停停) / >40 畅通（参考图片 2）
  const s = f.speed
  if (p.type === '卡口') {
    if (s < 10) return 'severe'
    if (s < 20) return 'congested'
    if (s < t)  return 'slow'
    return 'free'
  }
  if (s < 10) return 'severe'
  if (s < 20) return 'congested'
  if (s < t)  return 'crowd'  // 缓行(车多，不算真正拥堵)
  return 'free'
}
// 车流状态色：四档（红/橙/黄/绿），与图片 1+2 图例一致
function flowColor(p) {
  const lv = flowLevel(p)
  return lv === 'severe' ? 'r' : (lv === 'congested' ? 'r' : (lv === 'slow' || lv === 'crowd' ? 'y' : 'g'))
}

// —— 点位实时信息解析：优先取 WebSocket /ws stats.devices（按 id 匹配），无则保底写死 ——
// 统一返回「点位信息视图」，供标点着色 / 详情卡 / 车流卡 / 实时刷新统一消费：
//   · WS 命中：status / 综合拥挤度(congestion_score) / 实时计数 / 今日·本小时累计 全部来自 WS；
//     车速由 congestion_score 反推（越拥堵车速越低），保证可视化连贯且数据驱动。
//   · WS 未命中（无连接 / 该点位不在 WS 列表）：回退 CITY_WALL_POINTS 写死字段 + 模拟流量。
const WALL_IDS = new Set(CITY_WALL_POINTS.map((p) => p.id))

function statusTextOf(level, isVeh) {
  if (level === 'severe') return isVeh ? '严重拥堵' : '严重拥挤'
  if (level === 'congested') return '拥堵'
  if (level === 'slow' || level === 'crowd') return isVeh ? '缓行' : '拥挤'
  return '畅通'
}

/**
 * 点位实时信息解析（统一入口，供标点/详情卡/车流卡/实时刷新消费）
 * ---------------------------------------------------------------------------
 * 优先级：WebSocket /ws `stats.devices`（按 device_id == 点位 id 匹配）> CITY_WALL_POINTS 写死 + 模拟流量。
 *   · WS 命中：直接采用其真实统计字段 ——
 *       current_vehicles / current_persons
 *       today_vehicle_in/out、today_person_in/out
 *       hour_vehicle_in/out、hour_person_in/out、hour
 *       vehicle_flow_per_min、person_flow_per_min
 *       congestion_score、vehicle_congested、person_congested、congested、status
 *     展示派生量（current/daily/hourly/speed/congCls/level）全部由上述真实字段计算，不引入模拟值。
 *   · WS 未命中（无连接 / 该点位不在 WS 列表 / 报错）：回退 CITY_WALL_POINTS 写死字段 + getFlow() 模拟流量。
 *     保底数据仅作占位，绝不被写入或覆盖 WS 真实数据（见 tickFlow 守卫）。
 */
function resolvePointInfo(p) {
  const isVeh = p.type === '卡口'
  const ws = dev.statsById[p.id] || null
  console.log('resolvePointInfo', p, ws)
  if (ws) {
    // —— 真实 WebSocket 统计（v0.11.0 stats.devices）——
    const current = isVeh ? (ws.current_vehicles || 0) : (ws.current_persons || 0)
    const dailyIn = isVeh ? (ws.today_vehicle_in || 0) : (ws.today_person_in || 0)
    const dailyOut = isVeh ? (ws.today_vehicle_out || 0) : (ws.today_person_out || 0)
    const daily = dailyIn + dailyOut
    const hourlyIn = isVeh ? (ws.hour_vehicle_in || 0) : (ws.hour_person_in || 0)
    const hourlyOut = isVeh ? (ws.hour_vehicle_out || 0) : (ws.hour_person_out || 0)
    const hourly = hourlyIn + hourlyOut
    const congScore = ws.congestion_score || 0
    // 车速按综合拥挤度反推（越堵越慢），保证可视化连贯且数据驱动
    const speed = Math.round((1 - congScore) * (isVeh ? 60 : 50)) + 8
    const congCls = congScore >= 0.66 ? 'r' : (congScore >= 0.33 ? 'y' : 'g')
    // 拥堵分级：优先以各维度 congested 标志，其次综合分
    const vCong = !!ws.vehicle_congested
    const pCong = !!ws.person_congested
    const dimCong = isVeh ? vCong : pCong
    let level
    if (dimCong) level = 'severe'
    else if (congScore >= 0.66) level = 'congested'
    else if (congScore >= 0.33) level = 'slow'
    else level = 'free'
    let density = 0, flow = 0
    if (!isVeh) {
      density = +((current || 0) / 50).toFixed(2)
      flow = Math.round(density * speed * 60)
    }
    return {
      fromWs: true, source: 'ws', isVeh, kind: isVeh ? 'veh' : 'ped',
      status: ws.status || p.status,
      current, daily, dailyIn, dailyOut, hourly, hourlyIn, hourlyOut,
      speed, congScore, congCls, level, density, flow,
      vehicleFlowPerMin: ws.vehicle_flow_per_min || 0,
      personFlowPerMin: ws.person_flow_per_min || 0,
      vehicleCongested: vCong, personCongested: pCong,
      hour: ws.hour || new Date().getHours(),
      congestionRaw: !!ws.congested
    }
  }
  // —— 保底占位：CITY_WALL_POINTS 写死字段 + 模拟流量（仅报错/无数据时启用，绝不覆盖 WS）——
  const f = getFlow(p)
  const lv = flowLevel(p)
  const isCong = lv === 'severe' || lv === 'congested'
  const congCls = isCong ? 'r' : (lv === 'slow' || lv === 'crowd' ? 'y' : 'g')
  return {
    fromWs: false, source: 'fallback', isVeh, kind: isVeh ? 'veh' : 'ped',
    status: p.status,
    current: f.current, daily: f.daily, dailyIn: 0, dailyOut: 0,
    hourly: f.hourly, hourlyIn: 0, hourlyOut: 0,
    speed: f.speed, congScore: 0, congCls, level: lv, density: f.density || 0, flow: f.flow || 0,
    vehicleFlowPerMin: 0, personFlowPerMin: 0,
    vehicleCongested: false, personCongested: false, hour: new Date().getHours(), congestionRaw: false
  }
}

// —— 设备标点渲染 ——
function renderDevices() {
  if (!map) return
  Object.values(markerMap).forEach((m) => map.remove(m))
  for (const k in markerMap) delete markerMap[k]

  dev.devices.forEach((d) => {
    if (WALL_IDS.has(d.id)) return  // 城墙点位由 renderDefaultMarkers 单独渲染，避免双层标点
    const pos = dev.positions[d.id] || CITY[current].center
    // 防御：严格数值类型校验坐标（Number.isFinite 不做隐式转换，排除 null/''/字符串/布尔），
    // 非法则跳过，避免 AMap lngLatToContainer 报 Pixel(NaN, NaN) 中断整批渲染
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
    const mk = new AMap.Marker({
      position: pos, anchor: 'bottom-center', zIndex: sel ? 200 : 150,
      cursor: 'pointer', content, draggable: true, bubble: true
    })
    mk.on('click', () => openEdit(d))
    mk.on('dragend', (e) => {
      const ll = mk.getPosition()
      setPosition(d.id, ll.lng, ll.lat)
      push(`已更新「${d.name}」地图位置`, 'info')
    })
    // 兜底：单个标点 add 失败不影响整批渲染（AMap 坐标/尺寸异常时可能抛 NaN 像素）
    try {
      map.add(mk)
    } catch (err) {
      console.warn('[renderDevices] 标点添加失败，已跳过：', d.id, err)
      return
    }
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
    const ll = e.lnglat
    editorMode.value = 'add'
    editorDevice.value = null
    editorPos.value = { lng: ll.lng, lat: ll.lat }
    editorOpen.value = true
    addMode.value = false
    document.body.classList.remove('adding')
    return
  }
  // 非添加模式：点击地图空白 → 关闭当前 tip
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
  pointMarkers.forEach((it) => map.remove(it.marker))
  pointMarkers.length = 0
}

// —— pin 内容构建（active=点击选中态高亮；hover=悬浮临时高亮，与选中态互补）——
// 状态标识按车流状态着色：拥堵红 / 通畅蓝（需求 #1）
function buildPinContent(p, mode = false) {
  const typeCls = p.type === '卡口' ? 'kakou' : 'biandao'
  const info = resolvePointInfo(p)
  const cls = mode === 'active' ? ' active' : (mode === 'hover' ? ' hover' : '')
  const colorCls = info.congCls === 'r' ? 'red' : (info.congCls === 'y' ? 'yellow' : 'green')
  const stCls = info.status === 'online' ? 'online' : (info.status === 'offline' ? 'offline' : 'syncing')
  return (
    `<div class="cam-pin ${typeCls} s-${stCls}${cls}">` +
    `<div class="pin">${camIcon}</div>` +
    `<span class="badge dot flow-${colorCls}"></span>` +
    `</div>`
  )
}

// —— 重置所有 pin 为非激活态 ——
function resetPinActive() {
  pointMarkers.forEach((it) => {
    const p = CITY_WALL_POINTS.find((pp) => pp.id === it.id)
    if (p) setMarkerMode(it.marker, p, false)
  })
}

// 仅刷新已渲染城墙点位的实时着色（WS 推送后即时反映，不重建 DOM）
function refreshWallPointPins() {
  pointMarkers.forEach((it) => {
    const p = CITY_WALL_POINTS.find((pp) => pp.id === it.id)
    if (!p) return
    const mode = selectedPointId.value === p.id ? 'active' : (hoverPointId === p.id ? 'hover' : false)
    setMarkerMode(it.marker, p, mode)
  })
}

// —— 区域圈地可视化：主边界（描边+淡填充）+ 外发光层 + 圈外遮罩（高德原生圈地逻辑）—— //
function renderRegionOverlay() {
  if (!map) return
  // 清除旧图层
  ;[regionBorder, regionGlow, regionMask, regionLabel, regionHit].forEach((m) => {
    if (m) try { map.remove(m) } catch (e) { }
  })
  regionBorder = regionGlow = regionMask = regionLabel = regionHit = null

  const poly = CITY[current] && CITY[current].polygon
  if (!poly || poly.length < 3) return
  const c = overlayColors.value
  const center = CITY[current].center

  // 1) 圈外遮罩层：外大环（城市中心 ±3°，足以覆盖视口）+ 城市多边形反向作洞
  //    利用 AMap Polygon 多环镂空规则 → 圈外被遮罩填充（变暗），圈内镂空（清晰）
  const d = 3
  const outer = [
    [center[0] - d, center[1] - d],
    [center[0] + d, center[1] - d],
    [center[0] + d, center[1] + d],
    [center[0] - d, center[1] + d]
  ]
  regionMask = new AMap.Polygon({
    path: [outer, poly.slice().reverse()],
    fillColor: c.maskFill,
    fillOpacity: c.maskOp,
    strokeColor: 'transparent',
    strokeWeight: 0,
    strokeOpacity: 0,
    bubble: true,
    zIndex: 110
  })
  map.add(regionMask)

  // 2) 边界外发光层：宽描边低透明度，模拟荧光辉光（科技感）
  regionGlow = new AMap.Polygon({
    path: poly,
    strokeColor: c.regionStroke,
    strokeWeight: 7,
    strokeOpacity: 0.28,
    fillColor: 'transparent',
    fillOpacity: 0,
    bubble: true,
    zIndex: 118
  })
  map.add(regionGlow)

  // 3) 主边界：清晰描边 + 淡填充（凸显区域，不遮挡底图）
  regionBorder = new AMap.Polygon({
    path: poly,
    strokeColor: c.regionStroke,
    strokeOpacity: c.regionStrokeOp,
    strokeWeight: 2.5,
    fillColor: c.polyFill,
    fillOpacity: c.polyFillOp,
    bubble: true,
    zIndex: 119
  })
  map.add(regionBorder)

  // 4) 区域标签：多边形质心上方
  const cy = poly.reduce((s, p) => s + p[1], 0) / poly.length
  regionLabel = new AMap.Marker({
    position: [center[0], cy + 0.0009],
    anchor: 'bottom-center', zIndex: 200, cursor: 'default',
    content: `<div class="region-label">🏯 ${CITY_NAME[current] || current} · 监控区域</div>`
  })
  map.add(regionLabel)

  // 5) 区域选中交互层：透明命中面（bubble:false 拦截区域内点击，不穿透到地图）
  //    —— 这是「区域选中效果」的核心：点击区域内任意位置 → 选中该区域并高亮边界 + 触发回调
  const isSel = selectedRegion.value === current
  regionHit = new AMap.Polygon({
    path: poly,
    fillColor: isSel ? c.regionStroke : 'transparent',
    fillOpacity: isSel ? 0.06 : 0,
    strokeColor: 'transparent', strokeWeight: 0, strokeOpacity: 0,
    bubble: false, zIndex: 115
  })
  regionHit.on('click', () => selectRegion(current))
  regionHit.on('mouseover', () => { if (selectedRegion.value !== current) setRegionHover(true) })
  regionHit.on('mouseout', () => setRegionHover(false))
  map.add(regionHit)

  // 选中态视觉强化：主边界加粗 + 发光层提亮
  if (isSel) applyRegionSelectedStyle()
}

// —— 区域选中态视觉应用（边界加粗 + 发光增强）——
function applyRegionSelectedStyle() {
  const c = overlayColors.value
  if (regionBorder) {
    try {
      regionBorder.setOptions({ strokeWeight: 4, strokeOpacity: 1, strokeColor: c.regionStroke })
    } catch (e) { }
  }
  if (regionGlow) {
    try { regionGlow.setOptions({ strokeWeight: 10, strokeOpacity: 0.5 }) } catch (e) { }
  }
}

// —— 悬浮态（非选中时轻微提亮边界，给用户可点反馈）——
let _hoverTimer = null
function setRegionHover(on) {
  if (!regionBorder) return
  try {
    regionBorder.setOptions({ strokeOpacity: on ? 0.85 : (selectedRegion.value ? 1 : overlayColors.value.regionStrokeOp) })
  } catch (e) { }
}

// —— 区域选中 / 取消 ——
function selectRegion(key) {
  if (selectedRegion.value === key) {
    // 再次点击同区域 → 取消选中
    selectedRegion.value = null
    push('已取消区域选中', 'info')
  } else {
    selectedRegion.value = key
    const name = CITY_NAME[key] || key
    push('已选中区域：' + name + ' · 监控区域', 'success')
    // 回调钩子（可扩展：上报选中区域、联动其他面板等）
    onRegionSelected && onRegionSelected(key)
  }
  // 重绘区域层以应用选中样式（命中面填充 + 边界加粗）
  if (map) renderRegionOverlay()
}

// 外部可注入的选中回调（默认无操作）
let onRegionSelected = null
function setRegionSelectCallback(fn) { onRegionSelected = fn }

// —— 点位提示框 HTML（tip 卡：头部标题栏 + 信息网格 + 右侧视频占位） ——
// 优化：渐变头部带类型徽章 / 图标化字段标签 / 经纬度展示 / 数值高亮 / CSS 分隔线
function buildTipHtml(p) {
  const info = resolvePointInfo(p)
  const statusText = info.status === 'online' ? '在线' : (info.status === 'offline' ? '离线' : '待验证')
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
  // 视频占位：加监控扫描线动效
  const media = `<div class="cam-placeholder"><div class="cam-scan"></div><div class="ph-ic">${placeholderIcon}</div><span>暂无视频流</span></div>`
  const lng = p.coord[0].toFixed(6)
  const lat = p.coord[1].toFixed(6)
  // 趋势指示灯：基于流量派生方向 + 百分比（监控数据可视化）
  const trend = info.current > 80 ? 'up' : (info.current > 30 ? 'flat' : 'down')
  const trendIcon = trend === 'up' ? '▲' : (trend === 'down' ? '▼' : '◆')
  const trendPct = trend === 'up'
    ? `+${Math.min((info.current % 40) + 5, 45)}%`
    : (trend === 'down' ? `-${(info.current % 15) + 3}%` : '0%')
  return (
    `<div class="cam-info cam-info-static tip-monitor">` +
    // 头部：荧光标题 + 实时脉冲指示灯 + 类型徽章 + 状态
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
    // 路段监控行：方向 + 城门 + 实时指示灯
    `<div class="tip-row tip-row-monitor">` +
    `<span class="tip-ic">${dirIcon}</span>` +
    `<span class="tip-lbl">路段</span>` +
    `<span class="tip-val tip-val-monitor">${p.gate} · ${p.direction}</span>` +
    `<span class="tip-led tip-led-sm" data-status="${info.status}"></span>` +
    `</div>` +
    // 流量详情区：按点位类型分流（车辆/行人），左侧字段网格 + 右侧视频占位
    `<div class="tip-body">` +
    `<div class="cam-info-left">` +
    `<div class="tip-field"><span class="tip-ic">${idIcon}</span><span class="tip-lbl">编号</span><span class="tip-val">${p.id}</span></div>` +
    `<div class="tip-field"><span class="tip-ic">${coordIcon}</span><span class="tip-lbl">坐标</span><span class="tip-val tip-coord">${lng}, ${lat}</span></div>` +
    `<div class="tip-field"><span class="tip-ic">${activeIcon}</span><span class="tip-lbl">活跃</span><span class="tip-val">${p.lastActive}</span></div>` +
    (() => {
      const f = resolvePointInfo(p)
      const isVeh = f.isVeh
      const isCong = f.congCls === 'r'
      const colorCls = f.congCls === 'r' ? 'red' : (f.congCls === 'y' ? 'yellow' : 'green')
      const statusText = statusTextOf(f.level, isVeh)
      // 车辆版：日过车 / 小时过车 / 实时 veh + 速度
      if (isVeh) {
        return (
          `<div class="tip-flow tip-flow-veh">` +
          `<div class="tip-flow-title">🚗 车流量详情</div>` +
          `<div class="tip-flow-row"><span class="tip-flow-k">平均车速</span><span class="tip-flow-v"><b>${f.speed}</b> km/h</span></div>` +
          `<div class="tip-flow-row"><span class="tip-flow-k">日过车(veh/d)</span><span class="tip-flow-v"><b>${f.daily.toLocaleString()}</b> veh/D</span></div>` +
          `<div class="tip-flow-row"><span class="tip-flow-k">小时过车(veh/h)</span><span class="tip-flow-v"><b>${f.hourly.toLocaleString()}</b> veh/H</span></div>` +
          `<div class="tip-flow-row"><span class="tip-flow-k">实时计数(veh)</span><span class="tip-flow-v"><b>${f.current}</b> veh</span></div>` +
          `<div class="tip-flow-row"><span class="tip-flow-k">车辆速率(辆/min)</span><span class="tip-flow-v"><b>${f.vehicleFlowPerMin}</b></span></div>` +
          `<div class="tip-flow-row"><span class="tip-flow-k">统计小时</span><span class="tip-flow-v"><b>${f.hour}:00</b></span></div>` +
          `<div class="tip-flow-status tip-flow-status-${colorCls}">${statusText}</div>` +
          `</div>`
        )
      }
      // 行人版：行人交通三参数 Q_p = K_p · v_p + 经验阈值参考
      const pedMin = Math.round(f.flow / 60)
      return (
        `<div class="tip-flow tip-flow-ped">` +
        `<div class="tip-flow-title">🚶 人流量详情</div>` +
        // 三参数主体（参照图片 2）
        `<div class="tip-3p">` +
        `<div class="tip-3p-cell"><span class="tip-3p-k">行人流量 Q<sub>p</sub></span><span class="tip-3p-v"><b>${f.flow.toLocaleString()}</b><span class="tip-3p-u">ped/h</span></span></div>` +
        `<div class="tip-3p-cell"><span class="tip-3p-k">行人平均速度 v<sub>p</sub></span><span class="tip-3p-v"><b>${f.speed}</b><span class="tip-3p-u">m/min</span></span></div>` +
        `<div class="tip-3p-cell"><span class="tip-3p-k">行人密度 K<sub>p</sub></span><span class="tip-3p-v"><b>${f.density.toFixed(2)}</b><span class="tip-3p-u">ped/m²</span></span></div>` +
        `</div>` +
        `<div class="tip-formula">公式：Q<sub>p</sub> = K<sub>p</sub> · v<sub>p</sub> = ${f.density.toFixed(2)} × ${f.speed} × 60 ≈ <b>${f.flow}</b> ped/h</div>` +
        // 详细数字（按图片 2 单位说明）
        `<div class="tip-flow-row"><span class="tip-flow-k">日过人 (ped/d)</span><span class="tip-flow-v"><b>${f.daily.toLocaleString()}</b></span></div>` +
        `<div class="tip-flow-row"><span class="tip-flow-k">小时过人 (ped/h)</span><span class="tip-flow-v"><b>${f.hourly.toLocaleString()}</b></span></div>` +
        `<div class="tip-flow-row"><span class="tip-flow-k">分钟人流 (ped/min)</span><span class="tip-flow-v"><b>${pedMin}</b></span></div>` +
        `<div class="tip-flow-row"><span class="tip-flow-k">实时计数 (ped)</span><span class="tip-flow-v"><b>${f.current}</b></span></div>` +
        `<div class="tip-flow-row"><span class="tip-flow-k">人流速率(人/min)</span><span class="tip-flow-v"><b>${f.personFlowPerMin}</b></span></div>` +
        `<div class="tip-flow-row"><span class="tip-flow-k">统计小时</span><span class="tip-flow-v"><b>${f.hour}:00</b></span></div>` +
        // 经验阈值参考（按图片 2 表格）
        `<div class="tip-threshold">` +
        `<div class="tip-threshold-title">📊 经验阈值参考</div>` +
        `<div class="tip-threshold-row ${f.daily < 12000 ? 'on' : ''}"><span>小区出入口</span><span>5000-12000 ped/d</span><span class="tip-threshold-state">畅通</span></div>` +
        `<div class="tip-threshold-row ${f.daily >= 20000 && f.daily < 40000 ? 'on' : ''}"><span>公园/景区</span><span>20000-40000</span><span class="tip-threshold-state">拥挤</span></div>` +
        `<div class="tip-threshold-row ${f.daily >= 40000 && f.daily < 70000 ? 'on' : ''}"><span>商圈热门</span><span>40000-70000</span><span class="tip-threshold-state">拥堵</span></div>` +
        `<div class="tip-threshold-row ${f.daily >= 70000 ? 'on' : ''}"><span>大型节假日</span><span>70000+</span><span class="tip-threshold-state">严重拥堵</span></div>` +
        `</div>` +
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

// —— 点位流量提示框 HTML（默认初始化即显示）——
// 按点位类型分流：
//   卡口（车辆）：平均车速(km/h) / 日过车(veh/D) / 小时过车(veh/H) / 实时 veh
//   便道（行人）：平均速度(m/min) / 日过人(ped/d) / 小时过人(ped/h) / 实时 ped
// 一次显示 4 个数值（需求 #2，参照图片 1+2 综合格式）
function buildFlowTipHtml(p) {
  const info = resolvePointInfo(p)
  const isVeh = info.isVeh
  const colorCls = info.congCls === 'r' ? 'red' : (info.congCls === 'y' ? 'yellow' : 'green')
  const statusText = statusTextOf(info.level, isVeh)
  const typeBadge = p.type === '卡口'
    ? `<span class="flow-tag flow-tag-kakou">卡口</span>`
    : `<span class="flow-tag flow-tag-biandao">便道</span>`
  const speedUnit = isVeh ? 'km/h' : 'm/min'
  const dailyLabel = isVeh ? '日过车(veh/d)' : '日过人(ped/d)'
  const hourlyLabel = isVeh ? '时过车(veh/h)' : '时过人(ped/h)'
  const curUnit = isVeh ? 'veh' : 'ped'
  // 紧凑两行：标题行（色点 + 名称 + 类型 + 状态胶囊）/ 指标行（速度·日·时·实时 内联分隔）
  return (
    `<div class="cam-flow flow-compact tip-monitor" data-flow="${colorCls}" data-kind="${info.kind}" data-id="${p.id}">` +
    `<div class="flow-head">` +
    `<span class="flow-dot flow-dot-${colorCls}"></span>` +
    `<span class="flow-title" title="${p.desc}">${p.desc}</span>` +
    typeBadge +
    `<span class="flow-pill flow-pill-${colorCls}">${statusText}</span>` +
    `</div>` +
    `<div class="flow-metrics">` +
    `<span class="fm"><b class="flow-speed-val" data-id="${p.id}">${info.speed}</b><i>${speedUnit}</i></span>` +
    `<span class="fm-sep"></span>` +
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

// —— AMapUI 组件库加载（字体图标 Marker + 信息窗体）——
// index.html 已引入 AMapUI（ui/1.1/main.js）；此处获取其模块并探测可用性，失败则降级。
function ensureAMapUI() {
  return new Promise((resolve) => {
    if (window.AMapUI && window.AMapUI.loadUI) return resolve(window.AMapUI)
    let n = 0
    const t = setInterval(() => {
      if (window.AMapUI && window.AMapUI.loadUI) { clearInterval(t); resolve(window.AMapUI) }
      else if (++n > 60) { clearInterval(t); resolve(null) } // 6s 超时放弃
    }, 100)
  })
}
function loadAMapUIModules() {
  if (amapUIPromise) return amapUIPromise
  amapUIPromise = new Promise((resolve) => {
    let settled = false
    const done = () => { if (!settled) { settled = true; resolve() } }
    ensureAMapUI()
      .then((UI) => {
        if (!UI) return done() // 无 AMapUI → 降级（自定义 Marker + 核心 InfoWindow）
        try {
          UI.loadUI(['overlay/SimpleMarker', 'overlay/SimpleInfoWindow'], (SimpleMarker, SimpleInfoWindow) => {
            if (SimpleMarker) { simpleMarkerClass = SimpleMarker; HAS_SIMPLE_MARKER = true }
            if (SimpleInfoWindow) { simpleInfoWindowClass = SimpleInfoWindow; HAS_SIMPLE_IW = true }
            done()
          })
        } catch (e) { done() }
        // 模块加载兜底：loadUI 回调迟迟未触发也继续渲染（降级）
        setTimeout(done, 4000)
      })
      .catch(() => done())
    // 全局安全兜底
    setTimeout(done, 6000)
  })
  return amapUIPromise
}

// —— 点位标记：优先 AMapUI SimpleMarker（字体图标标注），否则回退自定义 Marker ——
// SimpleMarker 的 iconTheme/iconStyle 为预设主题色（blue/green/orange/red/purple），
// iconLabel.innerHTML 放置矢量图标（车/人），即"字体图标标注（Marker 的图标标注形式）"。
const ICON_CAR = `<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><path d="M5 11l1.5-4.6A2 2 0 0 1 8.4 5h7.2a2 2 0 0 1 1.9 1.4L19 11h1a1 1 0 0 1 1 1v3a1 1 0 0 1-1 1h-1v1a1 1 0 0 1-2 0v-1H7v1a1 1 0 0 1-2 0v-1H4a1 1 0 0 1-1-1v-3a1 1 0 0 1 1-1h1zm2.2-.6L6 13h12l-1.2-2.6A.8.8 0 0 0 16 9.8H8a.8.8 0 0 0-.8.6zM7.5 15.2a1.2 1.2 0 1 0 0 .01zM16.5 15.2a1.2 1.2 0 1 0 0 .01z"/></svg>`
const ICON_USER = `<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><path d="M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zm0 2c-4 0-7 2-7 5v1h14v-1c0-3-3-5-7-5z"/></svg>`

// 点位标记配色（数据驱动：状态 + 车流等级），映射 SimpleMarker 预设主题色
function markerColorName(p, mode) {
  if (mode === 'active') return 'purple'  // 紫色高亮选中态
  if (mode === 'hover') return 'orange'   // 悬停提亮
  if (p.status === 'offline' || p.status === 'syncing') return 'blue' // 蓝（离线/待验证）
  const lv = flowLevel(p)
  const isCong = lv === 'severe' || lv === 'congested'
  if (isCong) return 'red'               // 红（拥堵）
  if (lv === 'slow' || lv === 'crowd') return 'orange' // 黄（缓行/拥挤）
  return 'green'                          // 绿（畅通）
}
function markerTitle(p) {
  const info = resolvePointInfo(p)
  const st = info.status === 'online' ? '在线' : info.status === 'syncing' ? '待验证' : '离线'
  return `${p.desc}｜${p.gate}·${p.direction}｜${st}`
}
// 创建点位标记
// 统一用 AMap.Marker + content（SVG 字体图标 + 圆形背景）+ anchor:'center'
// content 为 .cam-pin（34×34 圆形），几何尺寸明确，anchor:'center' 把 div 几何中心精确对齐坐标点，零偏差。
// SimpleMarker 内部 DOM 结构与 anchor 交互不可控，已弃用。
function createPointMarker(p) {
  const info = resolvePointInfo(p)
  const baseZ = p.type === '卡口' ? 280 : 250
  const statZ = info.status === 'online' ? 20 : (info.status === 'syncing' ? 10 : 0)
  return new AMap.Marker({
    position: p.coord,
    anchor: 'center',          // 图标几何中心精确对齐经纬度坐标
    offset: new AMap.Pixel(0, 0), // 无额外偏移，保证零偏差
    zIndex: baseZ + statZ,
    cursor: 'pointer',
    content: buildPinContent(p, false),
    title: markerTitle(p)
  })
}
// 切换点位标记激活/悬浮态（统一用 setContent 重建图标，保留颜色态切换）
function setMarkerMode(mk, p, mode) {
  if (!mk) return
  try { mk.setContent(buildPinContent(p, mode)) } catch (e) { }
}

// —— 点击交互提示框（clickTipLayer 独立 TipLayer 实例）：还原之前格式 ——
// 还原为 TipLayer DOM 层方案：buildTipHtml 原始大卡 + SVG 连接线 + 箭头，
// zIndex 9750 高于 flowTipLayer(9600)，点击详情卡永远浮于默认车流卡之上、不被遮挡。
// 与 flowTipLayer 完全独立：各自显示/隐藏、互不干扰。
function updateClickTip() {
  if (!map || !clickTipLayer) return
  if (current !== 'datong' || !selectedPointId.value) { clickTipLayer.update([]); return }
  const p = CITY_WALL_POINTS.find((pp) => pp.id === selectedPointId.value)
  if (!p) { clickTipLayer.update([]); return }
  const pt = map.lngLatToContainer(new AMap.LngLat(p.coord[0], p.coord[1]))
  clickTipLayer.update([{ id: p.id, x: pt.x, y: pt.y, html: buildTipHtml(p) }])
}
function closeClickTip() {
  if (clickTipLayer) clickTipLayer.update([])
}

// —— 默认车流提示框（TipLayer DOM 层）：初始化 / 切换区域即全量展示，每个点位一个 ——
// TipLayer：SVG 连接线（2px 实线 + 80% 透明度 + 两端三角形箭头）+ DOM 卡片，多实例并存；
// 内置碰撞避让（多轮两两分离 + minimumSpacing 间距约束）+ 视口裁剪 + rAF 节流；
// 随地图 move/zoom/rotate 由 updateFlowTips() 重新投影坐标。
// 与点击交互提示框(clickTipLayer)完全独立：各自显示/隐藏、互不干扰。
function openFlowTips() {
  closeFlowTips()
  if (!map || current !== 'datong') return
  if (!CITY_WALL_POINTS || CITY_WALL_POINTS.length === 0) return
  if (!flowTipLayer) return
  updateFlowTips()
}
function closeFlowTips() {
  if (flowTipLayer) flowTipLayer.update([])
}
// 地图事件触发：重新投影所有点位坐标到容器像素，刷新 TipLayer（连接线 + 卡片位置）
function updateFlowTips() {
  if (!map || !flowTipLayer) return
  if (current !== 'datong') { flowTipLayer.update([]); return }
  if (!CITY_WALL_POINTS || CITY_WALL_POINTS.length === 0) { flowTipLayer.update([]); return }
  const items = []
  const sorted = sortPointsByPriority(CITY_WALL_POINTS)
  for (const p of sorted) {
    if (!p.coord || !isFinite(p.coord[0]) || !isFinite(p.coord[1])) continue
    const pt = map.lngLatToContainer(new AMap.LngLat(p.coord[0], p.coord[1]))
    items.push({ id: p.id, x: pt.x, y: pt.y, html: buildFlowTipHtml(p) })
  }
  flowTipLayer.update(items)
}
// 实时刷新已渲染卡片内的车流数值文本（不重建 DOM，避免闪烁）
function updateFlowTipContent() {
  if (!flowTipLayer) return
  flowTipLayer.updateFlowText((card, id) => {
    const p = CITY_WALL_POINTS.find((pp) => pp.id === id)
    if (!p) return
    const info = resolvePointInfo(p)
    const colorCls = info.congCls === 'r' ? 'red' : (info.congCls === 'y' ? 'yellow' : 'green')
    const statusText = statusTextOf(info.level, info.isVeh)
    card.setAttribute('data-flow', colorCls)
    const dot = card.querySelector('.flow-dot'); if (dot) dot.className = 'flow-dot flow-dot-' + colorCls
    const sp = card.querySelector('.flow-speed-val'); if (sp) sp.textContent = info.speed
    // 实时刷新各数值文本（WS 推送后同步，不重建 DOM）：日/时累计、实时计数、每分钟速率
    const setFld = (k, v) => { const el = card.querySelector('[data-fld="' + k + '"]'); if (el) el.textContent = v }
    setFld('daily', info.daily.toLocaleString())
    setFld('hourly', info.hourly.toLocaleString())
    setFld('current', info.current)
    setFld('fpm', info.isVeh ? info.vehicleFlowPerMin : info.personFlowPerMin)
    const st = card.querySelector('.flow-status'); if (st) { st.className = 'flow-status flow-status-' + colorCls; st.textContent = statusText }
    const pill = card.querySelector('.flow-pill'); if (pill) { pill.className = 'flow-pill flow-pill-' + colorCls; pill.textContent = statusText }
  })
}

// —— 点位标注渲染（仅标记，不渲染默认 tip；默认 tip 由 openFlowTips 独立负责）——
// 兜底：空数据 toast；无效坐标跳过；整体 try/catch 降级
function renderDefaultMarkers() {
  clearPointMarkers()
  if (!map) return
  if (current !== 'datong') return // 非大同区域不显示城墙点位
  if (!CITY_WALL_POINTS || CITY_WALL_POINTS.length === 0) {
    push('该区域暂无监控点位', 'warn'); return
  }
  try {
    // 按优先级排序：城门顺序 → 类型优先级（卡口>便道）→ 方向（入口>出口>其他）
    const sorted = sortPointsByPriority(CITY_WALL_POINTS)
    sorted.forEach((p) => {
      // 无效坐标兜底：跳过不渲染，避免 Marker 异常
      if (!p.coord || !isFinite(p.coord[0]) || !isFinite(p.coord[1])) {
        console.warn('[renderDefaultMarkers] 点位坐标无效，已跳过：', p.id, p.coord)
        return
      }
      const mk = createPointMarker(p)
      mk.on('click', () => selectPoint(p.id))
      // 悬浮高亮：进入时标记提亮，离开恢复（与点击选中态互补，不冲突）
      mk.on('mouseover', () => {
        hoverPointId = p.id
        if (selectedPointId.value !== p.id) setMarkerMode(mk, p, 'hover')
      })
      mk.on('mouseout', () => {
        hoverPointId = null
        if (selectedPointId.value !== p.id) setMarkerMode(mk, p, false)
      })
      map.add(mk)
      pointMarkers.push({ id: p.id, marker: mk })
    })
    // 重建后若仍有选中点位（视角重置等场景），保持其 pin 激活
    if (selectedPointId.value) {
      pointMarkers.forEach((it) => {
        const p = CITY_WALL_POINTS.find((pp) => pp.id === it.id)
        if (p) setMarkerMode(it.marker, p, it.id === selectedPointId.value)
      })
    }
  } catch (e) {
    console.error('[renderDefaultMarkers] 渲染失败：', e)
    push('点位渲染异常，请刷新重试', 'error')
  }
}

// —— 点击点位触发交互提示框 / 再次点击关闭 / 切换城市关闭 ——
// 关键：点击交互只作用于「独立的 clickTipLayer」（详细卡 + 连接线），完全不触碰常驻的 flowTipLayer（初始 25 个车流提示框）；
//       二者各自显示/隐藏、互不干扰。clickTipLayer zIndex(9750) 高于 flowTipLayer(9600)，详情卡永远浮于上层。
function selectPoint(id) {
  if (id == null) {
    selectedPointId.value = null
    resetPinActive()
    closeClickTip() // 仅关闭点击层，默认知名点位 tip 不受影响
    return
  }
  // 再次点击同一点位 → 关闭
  if (selectedPointId.value === id) {
    selectedPointId.value = null
    resetPinActive()
    closeClickTip()
    return
  }
  selectedPointId.value = id
  // 仅高亮选中 pin（地图标点，独立于提示框层）
  pointMarkers.forEach((it) => {
    const p = CITY_WALL_POINTS.find((pp) => pp.id === it.id)
    if (p) setMarkerMode(it.marker, p, it.id === id ? 'active' : false)
  })
  // 在独立的 clickTipLayer 中渲染该点「详细交互卡」（buildTipHtml 原始大卡样式 + 连接线），初始各点位 tip 完全不变
  updateClickTip()
}

/* ===== [临时停用] 点位到点位路径高亮功能 —— 函数定义块已整体注释 =====
 * 恢复方式：删除本行注释开标记与本块末尾的注释闭标记即可
// —— 自动路径高亮：初始化 / 切换区域时，仅高亮"相邻点位"之间的连接 ——
// 数据驱动：连接关系完全来自点位数据的 neighbors 字段（由 buildAdjacencyEdges 计算去重无向边），
// 不再把全部点位串联成一条线。相邻点位物理距离近、同属一条道路进出口，连线即真实道路走向；
// 渲染采用 AMap.Object3D.MeshLine（路面 + 箭头指引双层），路标风格、3D 立体、点击弹距离。
function highlightPointPaths() {
  if (!map || !roadHL) return
  if (current !== 'datong') { roadHL.clear(); closeRoadInfoWindow(); return }
  if (!CITY_WALL_POINTS || CITY_WALL_POINTS.length < 2) return
  roadHL.clear()
  closeRoadInfoWindow()
  // 仅高亮相邻点位（由数据 neighbors 定义），每段独立绘制
  const edges = buildAdjacencyEdges(CITY_WALL_POINTS)
  let count = 0
  for (const e of edges) {
    if (roadHL.highlightEdge(e.a, e.b)) count++
  }
  if (count) push(`已沿真实道路高亮 ${count} 段相邻点位连接`, 'success')
}

// 关闭路径距离 InfoWindow
function closeRoadInfoWindow() {
  if (roadInfoWindow) {
    try { roadInfoWindow.close() } catch (e) { }
    roadInfoWindow = null
  }
}
===== 临时停用块结束 ===== */


// —— 信息窗体同步说明 ——
// flowTipLayer：常驻的城墙各点位「车流提示框 + 连接线」（TipLayer DOM 层，SVG 连接线 + 卡片，带碰撞避让 + 两端箭头，默认初始化即显示）；
// clickTipLayer：点击后单独出现的「详细交互卡 + 连接线」（独立 TipLayer 实例，buildTipHtml 大卡，zIndex 9750 高于 flowTipLayer）。
// 二者互不干扰：均为 DOM 覆盖层，随地图平移/缩放由 updateFlowTips/updateClickTip 重新投影。

// —— 地图控制（注册到总线，供 TopBar 调用） ——
function changeCity(key) {
  current = key
  selectedPointId.value = null // 切换城市关闭当前 tip
  closeClickTip()            // 仅清点击层，默认知名点位 tip 不受影响
  if (!map) return
  renderRegionOverlay() // 先重绘圈地（遮罩 / 发光 / 边界 / 标签）
  // 自动缩放定位：框住所选区域多边形（高德原生圈地后的视角行为）
  try {
    map.setFitView([regionBorder, regionGlow], false, [80, 80, 80, 80])
  } catch (e) {
    map.setCenter(CITY[key].center, false, 300)
    map.setZoom(CITY[key].zoom, false, 300)
  }
  resetView(true) // 重设 pitch 保持 3D 视角
  clearPointMarkers()
  renderDefaultMarkers() // 重建点位标注（AMapUI 字体图标 Marker，失败降级）
  openFlowTips()        // 重建默认车流提示框（TipLayer：连接线 + 卡片，每点位一个）
  // [临时停用] 点位到点位路径高亮功能：调用已注释
  // highlightPointPaths() // 重建点位间道路路径高亮（datong 外区域自动清空）
  push('已定位至「' + (CITY_NAME[key] || key) + '」', 'info')
}
function changeStyle(s) { if (map) map.setMapStyle(s) }

// —— 主题切换：换底图瓦片 + 更新叠加层 JS 侧颜色 + 淡入淡出过渡 ——
function applyTheme() {
  if (!map) return
  const el = document.getElementById('container')
  if (el) el.style.opacity = '0.35'
  try { map.setMapStyle(mapStyle.value) } catch (e) { }
  const c = overlayColors.value
  // 圈外遮罩层
  if (regionMask) {
    try { regionMask.setOptions({ fillColor: c.maskFill, fillOpacity: c.maskOp }) } catch (e) { }
  }
  // 边界外发光层
  if (regionGlow) {
    try { regionGlow.setOptions({ strokeColor: c.regionStroke, strokeOpacity: 0.28 }) } catch (e) { }
  }
  // 主边界
  if (regionBorder) {
    try {
      regionBorder.setOptions({
        strokeColor: c.regionStroke, strokeOpacity: c.regionStrokeOp, strokeWeight: 2.5,
        fillColor: c.polyFill, fillOpacity: c.polyFillOp
      })
    } catch (e) { }
  }
  // 信息窗体背景由 [data-theme] CSS 变量驱动（.amap-info-content / .smp-ifwn），主题切换时自动重着色，无需 JS 同步
  // 默认车流提示框连接线颜色跟随主题（TipLayer setLineColor 同步线条 + 两端箭头）
  if (flowTipLayer) flowTipLayer.setLineColor(c.regionStroke)
  if (clickTipLayer) clickTipLayer.setLineColor(c.regionStroke)
  if (el) requestAnimationFrame(() => { el.style.opacity = '1' })
}
function resetView(silent) {
  if (!map) return
  map.setPitch(free3D ? 72 : CITY[current].pitch); map.setRotation(0)
  if (!silent) console.log('视角已重置')
}
function setTopView() { if (!map) return; free3D = false; map.setPitch(0) }
function setOblique() { if (!map) return; free3D = false; map.setPitch(CITY[current].pitch) }
function toggleFree3D() { if (!map) return; free3D = !free3D; map.setPitch(free3D ? 72 : CITY[current].pitch) }
function fitRange() {
  const b = CITY[current] && CITY[current].bounds
  if (!map || !b) return
  map.setBounds(new AMap.Bounds(b[0], b[1]), false, [60, 60, 60, 60])
}

// —— 初始化辅助：等待高德 SDK 就绪（避免 onMounted 早于 SDK 加载完成导致地图不显示）——
function whenAMapReady(timeout = 10000) {
  return new Promise((resolve) => {
    if (typeof window.AMap !== 'undefined' && window.AMap.Map) return resolve(true)
    const t0 = Date.now()
    const timer = setInterval(() => {
      if (typeof window.AMap !== 'undefined' && window.AMap.Map) {
        clearInterval(timer); resolve(true)
      } else if (Date.now() - t0 > timeout) {
        clearInterval(timer); resolve(false)
      }
    }, 120)
  })
}

// 渲染静态叠加层（区域圈地 + 点位标记 + 点位间道路路径高亮），不依赖设备加载，保证首屏即可见
// 每步独立容错：单步失败仅提示，不阻断其余图层
function renderStaticLayers() {
  try { renderRegionOverlay() }
  catch (e) { console.error('[renderRegionOverlay] 失败：', e); push('区域边界渲染失败', 'error') }
  // [临时停用] 点位到点位路径高亮功能：调用已注释
  // try { highlightPointPaths() }
  // catch (e) { console.error('[highlightPointPaths] 失败：', e) }
  // 点位标注 + 默认车流提示框均依赖 AMapUI（字体图标 Marker / 信息窗体）；
  // 加载完成后渲染，失败则降级自定义 Marker + 核心 InfoWindow
  loadAMapUIModules()
    .then(() => {
      try { renderDefaultMarkers() } catch (e) { console.error('[renderDefaultMarkers] 失败：', e) }
      try { openFlowTips() } catch (e) { console.error('[openFlowTips] 失败：', e) }
    })
    .catch(() => {
      try { renderDefaultMarkers() } catch (e) { console.error('[renderDefaultMarkers] 失败：', e) }
      try { openFlowTips() } catch (e) { console.error('[openFlowTips] 失败：', e) }
    })
}

// —— 模拟实时流量：定时轻微随机游走，刷新点位状态色与 tip 数值（需求 #1）——
let flowTimer = null
function tickFlow() {
  CITY_WALL_POINTS.forEach((p) => {
    // 仅维护「无 WebSocket 真实数据」点位的模拟流量（保底占位）；
    // 已被 WS 驱动的点位不在此处更新，杜绝模拟值覆盖真实数据
    if (dev.statsById[p.id]) return
    const f = getFlow(p)
    // 速度 ±3 随机游走，按类型限制范围（车辆 8~70 km/h，行人 5~60 m/min）
    const isVeh = p.type === '卡口'
    const smin = isVeh ? 8 : 5
    const smax = isVeh ? 70 : 60
    let s = f.speed + Math.round((Math.random() - 0.5) * 6)
    s = Math.max(smin, Math.min(smax, s))
    f.speed = s
    f.daily += Math.round(Math.random() * 12)
    f.hourly = Math.max(10, f.hourly + Math.round((Math.random() - 0.5) * 8))
    f.current = Math.max(0, f.current + Math.round((Math.random() - 0.5) * 3))
    // 行人派生量同步：v_p 变 → Q_p 跟着变（Q_p = K_p · v_p · 60）
    if (!isVeh) f.flow = Math.round(f.density * f.speed * 60)
  })
  // 1) 刷新所有点位标记颜色（保留 hover / active 态）：WS 驱动的点位渲染真实数据，保底点位渲染模拟数据
  pointMarkers.forEach((it) => {
    const p = CITY_WALL_POINTS.find((pp) => pp.id === it.id)
    if (!p) return
    const mode = selectedPointId.value === p.id ? 'active' : (hoverPointId === p.id ? 'hover' : false)
    setMarkerMode(it.marker, p, mode)
  })
  // 2) 直接更新已渲染默认信息窗体内的车流数值（经 updateFlowTipContent 重建各窗体内容）
  updateFlowTipContent()
}
function startFlowTicker() {
  if (flowTimer) return
  flowTimer = setInterval(tickFlow, 5000)
}

// —— 生命周期 ——
onMounted(async () => {
  // 1) 等待 SDK 就绪（网络慢/缓存未命中时不早退，最多等 10s）
  const ready = await whenAMapReady()
  if (!ready) {
    push('⚠ 高德地图 SDK 未加载（请检查网络 / Key 配置）', 'error')
    return
  }
  try {
    // 2) 显式应用默认区域配置，确保初始化参数完整
    current = DEFAULT_CITY

    map = new AMap.Map('container', {
      viewMode: '3D', pitch: CITY[current].pitch, rotation: 0,
      zoom: CITY[current].zoom, center: CITY[current].center,
      mapStyle: mapStyle.value,
      features: ['bg', 'road', 'building', 'point'], buildingAnimation: true,
      rotateEnable: true, pitchEnable: true, zoomEnable: true, dragEnable: true
    })

    // AMapUI（SimpleMarker / SimpleInfoWindow）可用性在 loadAMapUIModules() 内探测，失败自动降级

    map.on('click', onMapClick)

    // 主题化地图控件：缩放、比例尺、指南针
    try {
      if (window.AMap && AMap.plugin) {
        AMap.plugin(['AMap.Scale', 'AMap.ControlBar'], () => {
          try { map.addControl(new AMap.Scale()) } catch (e) { }
          try {
            map.addControl(new AMap.ControlBar({ position: { top: '110px', right: '18px' } }))
          } catch (e) { }
        })
      }
    } catch (e) { }

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
    // 路径高亮：初始化 / 切换区域时自动沿真实道路绘制（无需点击模式切换）
    // [临时停用] 点位到点位路径高亮功能：控制总线注册已注释
    // mapCtl.refreshRoadPaths = () => highlightPointPaths()

    // 3) 挂载默认车流提示框层（TipLayer DOM 层：SVG 连接线 + 卡片，带碰撞避让 + 两端箭头）
    //    在 renderStaticLayers 内 openFlowTips() 统一填充内容
    flowTipLayer = new TipLayer({
      lineColor: overlayColors.value.regionStroke,
      strokeWidth: 2,        // 2px 实线
      lineOpacity: 0.8,      // 80% 透明度
      gap: 56,               // 卡与点位间距
      maxLine: 420,          // 连接线最大长度（Phase A 常规避让的软约束；Phase B 兜底不重叠优先）
      minimumSpacing: 16     // 卡片间最小间距 16px（避让保证）
    })
    const containerEl = document.getElementById('container')
    if (containerEl) {
      flowTipLayer.mount(containerEl)
      // 点击交互提示框层（独立 TipLayer 实例，zIndex 9750 高于 flowTipLayer，详情卡永远浮于上层）
      clickTipLayer = new TipLayer({
        lineColor: overlayColors.value.regionStroke,
        strokeWidth: 2, lineOpacity: 0.8,
        gap: 56, maxLine: 200, minimumSpacing: 12,
        zIndex: 9750
      })
      clickTipLayer.mount(containerEl)
    }

    /* ===== [临时停用] 点位到点位路径高亮功能 —— 挂载渲染器 + 点击 InfoWindow 块已整体注释 =====
     * 恢复方式：删除本行注释开标记与本块末尾的注释闭标记即可
    // 3.5) 挂载路径高亮渲染器（沿真实道路的 3D 立体路径，需求 #1~#3）
    // 容错：即便 3D/Object3D 不可用或挂载异常，也不应阻断下方静态图层（区域+点位+提示框）的渲染
    try {
      roadHL = getRoadHighlight()
      roadHL.mount(map)
    } catch (e) {
      console.error('[roadHighlight] 挂载失败，已跳过路径高亮：', e)
      roadHL = null
    }
    if (roadHL) roadHL._onSelect = (info) => {
      // 点击相邻连接段 → 弹距离 InfoWindow（不阻塞地图操作）
      if (!map || !info) return
      closeRoadInfoWindow()
      const distKm = (info.distanceMeters / 1000).toFixed(2)
      const mid = [
        (info.coordA[0] + info.coordB[0]) / 2,
        (info.coordA[1] + info.coordB[1]) / 2
      ]
      roadInfoWindow = new AMap.InfoWindow({
        content: `
          <div class="road-info">
            <div class="road-info-title">🛣 相邻点位道路连接</div>
            <div class="road-info-row"><span>起</span><b>${info.descA}</b></div>
            <div class="road-info-row"><span>终</span><b>${info.descB}</b></div>
            <div class="road-info-distance">${distKm} <i>km</i></div>
            <div class="road-info-meta">同组相邻点位 · ${info.isMain ? '主路（卡口）' : '辅路（便道）'}</div>
            <button class="road-info-close" id="roadInfoClose2">关闭</button>
          </div>`,
        offset: new AMap.Pixel(0, -8),
        closeWhenClickMap: true,
        autoMove: true
      })
      roadInfoWindow.open(map, mid)
      setTimeout(() => {
        const btn = document.getElementById('roadInfoClose2')
        if (btn) btn.addEventListener('click', () => closeRoadInfoWindow(), { once: true })
      }, 0)
    }
    ===== 临时停用块结束 ===== */


    // 4) 关键：静态叠加层（区域 + 点位）独立渲染，不等待设备加载，
    //    确保初始化即显示区域边界与点位标记（与切换区域行为一致）
    renderStaticLayers()

    // 5) 容器尺寸兜底：下一帧若布局已完成，触发一次 resize 修正
    //    （避免初始化时容器尚未就绪导致地图空白，切换时容器已就绪故正常）
    requestAnimationFrame(() => {
      try { if (map && typeof map.resize === 'function') map.resize() } catch (e) { }
    })

    // 6) 设备加载（仅影响设备标点；失败仅提示，不阻断区域/点位显示）
    //    devices 变化由 watch 触发 renderDevices，这里额外确保一次即时渲染
    load()
      .then(() => { try { renderDevices() } catch (e) { console.error('[renderDevices]', e) } })
      .catch(() => { /* 错误提示已在 useDevices.load 内部处理 */ })

    // 地图事件：move/zoom/rotate 后重新投影 TipLayer（连接线 + 卡片位置随地图变化重绘）
    // flowTipLayer 与 clickTipLayer 均为 DOM 覆盖层，不随地图自动重投影，需手动触发
    map.on('complete', () => { updateFlowTips(); updateClickTip() })
    map.on('moveend', () => { updateFlowTips(); updateClickTip() })
    map.on('zoomend', () => { updateFlowTips(); updateClickTip() })
    map.on('rotatechange', () => { updateFlowTips(); updateClickTip() })

    // 7) 启动模拟实时车流：定时刷新点位红/蓝标识与 tip 数值（需求 #1）
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
  // [临时停用] 点位到点位路径高亮功能：卸载清理已注释
  // if (roadHL) { roadHL.destroy(); roadHL = null }
  // closeRoadInfoWindow()
  if (map) {
    if (regionBorder) try { map.remove(regionBorder) } catch { }
    if (regionGlow) try { map.remove(regionGlow) } catch { }
    if (regionMask) try { map.remove(regionMask) } catch { }
    if (regionLabel) try { map.remove(regionLabel) } catch { }
    if (regionHit) try { map.remove(regionHit) } catch { }
    try { map.destroy() } catch { }
  }
})

// 设备列表 / 选中变化 → 重绘标点（双向同步）
watch(
  () => dev.devices.map((d) => d.id + ':' + d.status).join('|') + '#' + dev.selectedId,
  () => renderDevices()
)
watch(() => dev.statsById, () => { renderDevices(); updateFlowTipContent(); refreshWallPointPins(); if (selectedPointId.value) updateClickTip() }, { deep: true })

// 主题变化 → 同步底图瓦片 / 区域叠加 / 控件配色
watch(theme, () => applyTheme())
</script>
