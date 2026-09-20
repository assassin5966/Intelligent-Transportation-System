<template>
  <div>
    <div id="container"></div>

    <!-- 添加模式提示 -->
    <div class="op-tip" v-if="addMode">📍 添加模式：点击地图选择设备位置（Esc 取消）</div>

    <!-- 设备编辑 / 添加弹窗 -->
    <DeviceEditor :open="editorOpen" :mode="editorMode" :device="editorDevice" :pos="editorPos"
      @close="editorOpen = false" @save="onEditorSave" @remove="onEditorRemove" />

    <!-- 单路摄像头 · 接口数据详情弹窗（点大卡内某张槽位小卡打开） -->
    <Teleport to="body">
      <div v-if="camDetail" class="cam-detail-mask" @click.self="closeCamDetail">
        <div class="cam-detail-modal">
          <div class="cdm-hd">
            <div class="cdm-hd-l">
              <span class="cdm-title">{{ camDetail.title }}</span>
              <span class="cdm-sub">{{ camDetail.subtitle }}</span>
            </div>
            <button type="button" class="cdm-close" title="关闭" @click="closeCamDetail">✕</button>
          </div>
          <div class="cdm-bd">
            <div class="cdm-grp" v-for="grp in camDetail.groups" :key="grp.title">
              <div class="cdm-grp-t">{{ grp.title }}</div>
              <div class="cdm-grid">
                <div class="cdm-item" v-for="it in grp.items" :key="it.k">
                  <span class="cdm-k">{{ it.k }}</span>
                  <b class="cdm-v">{{ it.v }}</b>
                </div>
              </div>
            </div>
          </div>
          <div class="cdm-ft">数据来源：/api/devices（设备信息）· ws://…/ws stats.devices（实时计数，随推送刷新）</div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup>
import { onMounted, onBeforeUnmount, ref, computed, watch } from 'vue'
import L from 'leaflet'
import { useDevices } from '../composables/useDevices.js'
import { useMapControl } from '../composables/useMapControl.js'
import { useToast } from '../composables/useToast.js'
import { useTheme } from '../composables/useTheme.js'
import { useVideoDetail } from '../composables/useVideoDetail.js'
import { sortPointsByPriority, sortPointsForZoneLayout, zoneOfPoint, ioKindOf, ioArrowDeg, IO_LABEL, roadInfoOf, anchorOutOfCross, metersPerPixelAt, CARD_BADGE_CLEAR_PX, WALL_OUT_SIDE, projectToWall } from '../data/cityWallPoints.js'
import { TipLayer } from '../utils/tipLayer.js'
import DeviceEditor from './DeviceEditor.vue'

const { state: dev, load, create, remove, configure, setPosition, select } = useDevices()
const { mapCtl } = useMapControl()
const { push } = useToast()
const { theme, mapStyle, overlayColors } = useTheme()
const { state: videoDetailState, open: openVideoDetail, close: closeVideoDetail } = useVideoDetail()

// —— 坐标转换工具（Leaflet 使用 [lat, lng]，数据使用 [lng, lat]）——
function toLatLng(p) { return [p[1], p[0]] }
function toLatLngArr(pts) { return pts.map(toLatLng) }

// —— 瓦片图层配置（同源相对路径，由 nginx.conf 的 location /tiles/ 直接静态服务）——
// 深色/浅色可分别配置，未配置时回退 VITE_TILE_URL，最终兜底同源 /tiles/{z}/{x}/{y}.png
const TILE_FALLBACK = '/tiles/{z}/{x}/{y}.png'
const TILE_URL_LIGHT = import.meta.env.VITE_TILE_URL_LIGHT || import.meta.env.VITE_TILE_URL || TILE_FALLBACK
const TILE_URL_DARK  = import.meta.env.VITE_TILE_URL_DARK  || import.meta.env.VITE_TILE_URL || TILE_FALLBACK
const TILE_ATTR = import.meta.env.VITE_TILE_ATTR || ''

// 1×1 透明兜底瓦片：未下载到的格子显示容器底色，而非破图白块（主题匹配关键）
const ERROR_TILE =
  'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC'

let tileLayerLight = null  // 浅色瓦片层
let tileLayerDark = null   // 深色瓦片层

// —— 城市配置 ——
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
    // 卡片缩放基准：默认视图下 2^(15.68-16.5) ≈ 0.57 → 大卡 ~215px 宽，
    // 8 张卡沿框线外环排开（tipLayer Phase B 兜底微调防挤叠）；
    // 放大地图时按 2^(zoom-16.5) 同步放大——z17 时 1.41 倍（~540px）、z18 时触顶 2.5 倍（~950px）
    cardBaseZoom: 16.5,
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
let ioLayer = null          // 框线「进出标识」层（框线与点位交会处的 进/出 闸口）
let flowTipLayer = null     // 默认车流提示框层
let clickTipLayer = null    // 点击交互提示框层
let flowConnLayer = null    // 大数据卡 → 古城框线 的虚线连接层（cross→coord）
let videoDetailClick = null // 大卡「详情」点击委托（挂 #container，卸载时移除）
const markerMap = {}        // deviceId -> L.marker

/* --------------------------------------------------------------------------
 * 信息卡布局模式
 *
 *   'anchor' —— 卡片贴附在指定一侧（当前采用）：8 张大卡的锚点是
 *               「框线交会点（进/出 徽标处）沿所在墙边垂直法向外偏
 *               CARD_BADGE_CLEAR_PX 像素」得到的经纬度 —— 即卡片挂靠在
 *               地图上已有的 进/出 标识上，与进出口保持固定位置关系，
 *               随地图平移/缩放始终锁定；卡片展开侧由 WALL_OUT_SIDE 给出
 *               （永远朝城外），内容按 cardScale 与地图等比缩放。
 *               另开启 TipLayer 视口裁剪（viewportCull）：锚点徽标完全出
 *               视口的卡自动隐藏（DOM/数据保留），放大单个城门时其余卡
 *               不堆在视口边缘，缩回总览自动全部恢复。
 *   TipLayer 内部还保留 'zone' / 'auto' 两种旧布局实现，MapPanel 已不再使用。
 * -------------------------------------------------------------------------- */
const FLOW_LAYOUT_MODE = 'anchor'
const FLOW_ZONE_SPACING = 12        // TipLayer 'zone' 布局的通道内卡片间距（当前未用）
const FLOW_ZONE_MIN_PAD_X = 300     // 非大同城市 fitCity() 的左右预留最小留白（px）

// —— 大数据卡随地图缩放等比放大缩小 ——
// 系数 = 2^(当前 zoom − 当前城市基准 zoom)：基准 zoom 时 = 1（即设计尺寸），
// 与古城框线 polygon 的屏幕尺寸严格成正比；放大系数 >1、缩小 <1。
// 系数做钳制，避免缩太小卡片消失 / 放太大卡片溢出。
// MIN 0.38：默认视图（z15.68）下 8 张大卡按 ~0.4 渲染（约 150px 宽），
// 恰好沿框线外环排开、互不重叠也不压框线；越放大越大，符合"随地图缩放"。
const CARD_SCALE_MIN = 0.38
const CARD_SCALE_MAX = 2.5
function cardScaleNow() {
  if (!map || !CITY[current]) return 1
  // 卡片缩放基准与地图初始 zoom 解耦：cardBaseZoom 才是"卡片 = 原始尺寸"的缩放级
  const base = CITY[current].cardBaseZoom || CITY[current].zoom
  const raw = Math.pow(2, map.getZoom() - base)
  return Math.min(CARD_SCALE_MAX, Math.max(CARD_SCALE_MIN, raw))
}

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
/**
 * 门级进 / 出方向：从该门 4 路设备里任意一路的名字 / 分类中提取。
 * 卡口设备名（如「GAKK-0021和阳北门御河西路-卡口193015」）不带出入口字样，
 * 方向只出现在便道设备名里，因此必须扫全组而不是只看卡口。
 */
function gateDirectionOf(items) {
  for (const x of items) {
    const txt = `${(x.d && x.d.name) || ''} ${(x.d && x.d.category) || ''}`
    if (txt.includes('入口')) return '入口'
    if (txt.includes('出口')) return '出口'
  }
  return '其他'
}
/** 车辆卡口设备判定（后端 camera_type 缺失时按名称 / 分类兜底）。 */
function isVehDevice(d) {
  if (!d) return false
  if (d.camera_type === 'vehicle') return true
  if (d.camera_type === 'person') return false
  return /摄像头|卡口|车道/.test(`${d.name || ''} ${d.category || ''}`)
}

/**
 * 后端设备 → 标点点位（一门 4 路，共 32 个标点）。
 * 设备基础信息（名称 / 类型 / 状态 / 分类 / 经纬度）全部取自 /api/devices；
 * cross / edge 由设备坐标垂直投影到古城框线几何算出（见 projectToWall）；
 * 计数不在这里取，统一走 WS stats.devices（dev.statsById）实时刷新。
 * 注意：这里产出的是「一台设备一个标点」，地图上仍是 32 路标点；
 *      8 张门级大卡由 backendGateCards 聚合产出。
 */
function backendWallPoints() {
  const poly = CITY.datong.polygon
  const list = []
  dev.devices.forEach((d) => {
    if (!d || !d.id) return
    if (!dev.showOffline && d.status !== 'online') return
    const pos = dev.positions[d.id]
    if (!pos || !Number.isFinite(pos.lng) || !Number.isFinite(pos.lat)) return
    const isKakou = isVehDevice(d)
    const coord = [pos.lng, pos.lat]
    const proj = projectToWall(coord, poly) || {}
    list.push({
      id: d.id, desc: d.name || d.id,
      type: isKakou ? '卡口' : '便道',
      gate: gateOf(d.name), direction: dirOf(d.name),
      coord,
      cross: proj.cross || null,
      edge: proj.edge || '东墙',
      lastActive: d.last_heartbeat ? String(d.last_heartbeat).slice(0, 10) : '—',
      status: d.status || 'syncing',
      typeLabel: isKakou ? '车行' : '人行',
      count: 0, category: d.category || '', camera_type: d.camera_type || ''
    })
  })
  return list
}
/**
 * 标点点位集合 = 后端 /api/devices 的设备（有几台画几个标点）。
 * 设备基础信息以 /api/devices 为准，实时计数以 WS stats.devices 为准；
 * cross / edge 由设备坐标投影到古城框线自动计算，硬编码城门点位表已退出主链路。
 */
const wallPoints = computed(() => backendWallPoints())
const WALL_IDS = computed(() => new Set(wallPoints.value.map((p) => p.id)))
function findPoint(id) { return wallPoints.value.find((p) => p.id === id) }

/* --------------------------------------------------------------------------
 * 按门聚合：32 路设备（8 门 × 4 路）→ 8 张门级大卡
 *
 * 数据侧一门 4 路（见 data/device_geo.json）：
 *   便道 ×2（GAJK-*，point_type=便道）  +  卡口 ×2（GAKK-*，point_type=车辆卡口）
 * 分组键 = 城门断面完整名（和阳北门 / 和阳南门 / 永泰东门 / 永泰西门 /
 *          清远南门 / 清远北门 / 武定西门 / 武定东门），从设备名 / 分类里识别；
 * 识不出断面名时回落到 gateOf() 的四大城门，保证不丢设备。
 *
 * 4 路设备归槽（按设备归属，标签与数据一一对应）：
 *   per0 = 断面左便道（方位与 roadInfoOf().sideText[0] 一致的便道）
 *   vehA = 摄像头A·车道1-2      vehB = 摄像头B·车道3-4
 *   per1 = 断面右便道
 * 便道左右不能只看设备名方位词：断面随行驶方向旋转后「左/右」对应哪个方位
 * 由「所在城墙边 + 进/出」推出（sideText），须按该方位认领对应便道设备。
 *
 * 卡片锚点 = 该门卡口点（多台卡口取坐标中点，兼容武定西门 GAKK-821/822
 * 两个不同 point_id 的情况）；标点仍走 wallPoints（32 路，不聚合）。
 * ------------------------------------------------------------------------ */

/** 断面完整名 → 所属大城门（用于分区布局 zone / 排序） */
const GATE_KEY_DEFS = [
  ['和阳北门', '和阳门'], ['和阳南门', '和阳门'],
  ['永泰东门', '永泰门'], ['永泰西门', '永泰门'],
  ['清远南门', '清远门'], ['清远北门', '清远门'],
  ['武定西门', '武定门'], ['武定东门', '武定门']
]

function gateKeyOf(d) {
  const txt = `${(d && d.name) || ''} ${(d && d.category) || ''}`
  for (const [key, gate] of GATE_KEY_DEFS) {
    if (txt.includes(key)) return { key, gate }
  }
  const gate = gateOf(d && d.name)
  return { key: gate, gate }
}

/**
 * 单路设备 → 槽位。
 * @param {object} d 设备
 * @param {string[]} sideText 断面旋转后左 / 右便道朝向的方位字（roadInfoOf）
 */
function slotOfDevice(d, sideText) {
  const txt = `${(d && d.name) || ''} ${(d && d.category) || ''}`
  if (isVehDevice(d)) {
    if (/摄像头A|车道1-2|车道1·2/.test(txt)) return 'vehA'
    if (/摄像头B|车道3-4|车道3·4/.test(txt)) return 'vehB'
    // 分类里没带 A/B 时按「进 = 车道1-2、出 = 车道3-4」兜底
    return /出口/.test(txt) ? 'vehB' : 'vehA'
  }
  const side = /北侧/.test(txt) ? '北'
    : (/南侧/.test(txt) ? '南' : (/东侧/.test(txt) ? '东' : (/西侧/.test(txt) ? '西' : '')))
  if (!side) return 'per0'
  return side === sideText[0] ? 'per0' : 'per1'
}

function backendGateCards() {
  const poly = CITY.datong.polygon
  const groups = new Map()
  dev.devices.forEach((d) => {
    if (!d || !d.id) return
    if (!dev.showOffline && d.status !== 'online') return
    const pos = dev.positions[d.id]
    if (!pos || !Number.isFinite(pos.lng) || !Number.isFinite(pos.lat)) return
    const { key, gate } = gateKeyOf(d)
    let g = groups.get(key)
    if (!g) { g = { key, gate, items: [] }; groups.set(key, g) }
    g.items.push({ d, pos })
  })

  const cards = []
  groups.forEach((g) => {
    // 锚点 = 卡口点（一门两台时取中点）；无卡口数据时退到该门任一设备
    const vehItems = g.items.filter((x) => isVehDevice(x.d))
    const anchorItems = vehItems.length ? vehItems : g.items
    const coord = [
      anchorItems.reduce((s, x) => s + x.pos.lng, 0) / anchorItems.length,
      anchorItems.reduce((s, x) => s + x.pos.lat, 0) / anchorItems.length
    ]
    const proj = projectToWall(coord, poly) || {}
    // 方向必须扫全组：卡口设备名不含「入口/出口」，只看卡口会判成「其他」，
    // 进而 roadInfoOf 的 sideText 失真、便道左右认领出错。
    const direction = gateDirectionOf(g.items) || dirOf((vehItems[0] || g.items[0]).d.name)
    const ri = roadInfoOf({ edge: proj.edge || '东墙', direction })

    const slots = {}
    g.items.forEach((x) => {
      const slot = slotOfDevice(x.d, ri.sideText)
      if (!slots[slot]) slots[slot] = x.d.id
    })
    const statuses = g.items.map((x) => x.d.status || 'syncing')
    const latest = g.items.reduce((acc, x) => {
      const t = (x.d.last_heartbeat && String(x.d.last_heartbeat).slice(0, 10)) || ''
      return t > acc ? t : acc
    }, '')
    cards.push({
      id: g.key, desc: g.key,
      type: '卡口', typeLabel: '车行',
      gate: g.gate, direction,
      coord, cross: proj.cross || null, edge: proj.edge || '东墙',
      lastActive: latest || '—',
      status: statuses.includes('online') ? 'online' : statuses[0],
      count: 0, category: '', camera_type: '', slots
    })
  })
  return cards
}

/** 门级大卡集合 = 8 张（一步聚合 8 门）；标点仍用 wallPoints（32 路）。 */
const gateCards = computed(() => backendGateCards())
function findGateCard(id) { return gateCards.value.find((p) => p.id === id) }

/** 门卡 4 槽位定义（顺序即横贯断面全宽的展示顺序） */
const GATE_SLOTS = [
  { key: 'per0', dim: 'per', kind: 'per', label: (ri) => `${ri.sideText[0] || '北'}侧便道·人流` },
  { key: 'vehA', dim: 'veh', kind: 'veh', label: () => '摄像头A·车道1-2' },
  { key: 'vehB', dim: 'veh', kind: 'veh', label: () => '摄像头B·车道3-4' },
  { key: 'per1', dim: 'per', kind: 'per', label: (ri) => `${ri.sideText[1] || '南'}侧便道·人流` }
]

const LEVEL_RANK = { free: 0, slow: 1, congested: 2, severe: 3, abnormal: 4, offline: 4 }

/**
 * 单槽位统计：口径 = 该设备「进 + 出」双向合计（用户确认口径）。
 * @param {string|undefined} id 该槽位归属设备 id（无设备则不传）
 * @param {'per'|'veh'} dim 人 / 车维度
 * @returns {object|null} null 表示该槽位无设备或无实时数据（显示 —）
 */
function slotStatOf(id, dim) {
  const ws = id ? dev.statsById[id] : null
  if (!ws) return null
  const status = ws.status || 'abnormal'
  if (status !== 'online') {
    return {
      id, dim, status, ok: false, daily: null, hourly: null, current: 0,
      level: status === 'offline' ? 'offline' : 'abnormal'
    }
  }
  const isVeh = dim === 'veh'
  const din = isVeh ? (ws.today_vehicle_in || 0) : (ws.today_person_in || 0)
  const dout = isVeh ? (ws.today_vehicle_out || 0) : (ws.today_person_out || 0)
  const hin = isVeh ? (ws.hour_vehicle_in || 0) : (ws.hour_person_in || 0)
  const hout = isVeh ? (ws.hour_vehicle_out || 0) : (ws.hour_person_out || 0)
  const congScore = ws.congestion_score || 0
  const congested = isVeh ? !!ws.vehicle_congested : !!ws.person_congested
  const level = congested ? 'severe'
    : (congScore >= 0.66 ? 'congested' : (congScore >= 0.33 ? 'slow' : 'free'))
  return {
    id, dim, status, ok: true, daily: din + dout, hourly: hin + hout,
    current: isVeh ? (ws.current_vehicles || 0) : (ws.current_persons || 0),
    level, congScore
  }
}

/** 门卡 4 槽位统计表（key 同 GATE_SLOTS.key） */
function gateSlotStats(p) {
  const out = {}
  for (const s of GATE_SLOTS) out[s.key] = slotStatOf((p.slots || {})[s.key], s.dim)
  return out
}

/** 门卡整体等级 = 4 槽位中最差的一路（任一路离线/异常 → 整卡随之降级） */
function gateCardLevel(p) {
  const stats = gateSlotStats(p)
  const list = GATE_SLOTS.map((s) => stats[s.key]).filter(Boolean)
  if (!list.length) return 'abnormal'
  let lvl = 'free'
  for (const s of list) if ((LEVEL_RANK[s.level] || 0) > (LEVEL_RANK[lvl] || 0)) lvl = s.level
  return lvl
}

function statusTextOf(level, isVeh) {
  if (level === 'offline') return '离线'
  if (level === 'abnormal') return '异常'
  if (level === 'severe') return isVeh ? '严重拥堵' : '严重拥挤'
  if (level === 'congested') return '拥堵'
  if (level === 'slow' || level === 'crowd') return isVeh ? '缓行' : '拥挤'
  return '畅通'
}

/* --------------------------------------------------------------------------
 * 单路设备数据详情弹窗
 *
 * 交互：点大卡内某一张槽位小数据卡（左便道 / 摄像头A / 摄像头B / 右便道）
 *       → 弹窗列出「这一路设备」接口返回的全部字段。
 * 数据源（两接口合并，与地图取数同源）：
 *   - /api/devices（含 WS 合并回写）→ dev.devices：名称 / 类型 / 分类 / 状态 / 经纬度 / 心跳
 *   - ws stats.devices（v0.11.0 主通道）→ dev.statsById：计数 + 拥挤度全字段
 * 弹窗内容走 computed → WS 每次推送自动刷新，无需手动重开。
 * ------------------------------------------------------------------------ */
const camDetailKey = ref(null)   // { gateId, slotKey } | null

const STATUS_LABEL = { online: '在线', offline: '离线', abnormal: '异常', syncing: '同步中' }

const camDetail = computed(() => {
  const k = camDetailKey.value
  if (!k) return null
  const p = findGateCard(k.gateId)
  const slot = GATE_SLOTS.find((s) => s.key === k.slotKey)
  if (!p || !slot) return null
  const devId = (p.slots || {})[k.slotKey] || ''
  const ri = roadInfoOf(p)
  const ws = devId ? (dev.statsById[devId] || null) : null
  const d = devId ? (dev.devices.find((x) => x.id === devId) || null) : null
  const isVeh = slot.dim === 'veh'
  const f = (v) => (v == null || v === '' ? '—' : String(v))
  const n = (v) => (v == null || v === '' ? '—' : Number(v).toLocaleString())
  const yesNo = (v) => (v ? '是' : '否')
  const cnt = (v) => n(v) + (isVeh ? ' 辆' : ' 人次')
  // 拥挤阈值: 0 / null 都表示该维度未开启拥挤判断, 展示为「未设置」比 0 更准确。
  // 取值优先 statsById（WS 侧保留 null），其次 /api/devices（空值已被合并成 0）。
  const maxOf = (v) => (v == null || Number(v) <= 0 ? '未设置' : String(v))
  // 后端 last_heartbeat 是 UTC ISO8601（datetime.now(timezone.utc)），必须转本地时区再展示
  const fmtLocalTime = (s) => {
    if (!s) return '—'
    const t = new Date(s)
    if (Number.isNaN(t.getTime())) return String(s)
    const p2 = (x) => String(x).padStart(2, '0')
    return `${t.getFullYear()}-${p2(t.getMonth() + 1)}-${p2(t.getDate())} ` +
      `${p2(t.getHours())}:${p2(t.getMinutes())}:${p2(t.getSeconds())}`
  }

  const groups = [{
    title: '设备信息（/api/devices）',
    items: [
      { k: '槽位', v: `${slot.label(ri)}（${isVeh ? '车辆卡口' : '便道'}）` },
      { k: '所属断面', v: f(p.desc) },
      { k: '设备编号', v: f(devId) },
      { k: '设备名称', v: f((d && d.name) || (ws && ws.name)) },
      { k: '设备类型', v: isVeh ? '车辆' : '人流' },
      { k: '设备分类', v: f(d && d.category) },
      { k: '运行状态', v: STATUS_LABEL[(d && d.status) || (ws && ws.status)] || f((d && d.status) || (ws && ws.status)) },
      { k: '最大容量', v: maxOf(ws ? (isVeh ? ws.max_vehicles : ws.max_persons) : (d && (isVeh ? d.max_vehicles : d.max_persons))) },
      { k: '经纬度', v: d && d.longitude != null && d.latitude != null ? `${d.longitude}, ${d.latitude}` : '—' },
      { k: '最近心跳', v: fmtLocalTime(d && d.last_heartbeat) }
    ]
  }]

  if (!ws) {
    groups.push({ title: '实时计数（ws stats.devices）', items: [{ k: '数据', v: '该路暂无实时推送数据' }] })
    return { title: slot.label(ri), subtitle: `${p.desc} · ${devId || '未匹配设备'}`, groups }
  }

  groups.push({
    title: isVeh ? '车辆计数（进 + 出）' : '人流计数（进 + 出）',
    items: [
      { k: '当前在区', v: cnt(isVeh ? ws.current_vehicles : ws.current_persons) },
      { k: '今日进入', v: cnt(isVeh ? ws.today_vehicle_in : ws.today_person_in) },
      { k: '今日离开', v: cnt(isVeh ? ws.today_vehicle_out : ws.today_person_out) },
      { k: '今日合计', v: cnt((isVeh ? ws.today_vehicle_in + ws.today_vehicle_out : ws.today_person_in + ws.today_person_out)) },
      { k: '当前小时', v: f(ws.hour) === '—' ? '—' : `${ws.hour} 时` },
      { k: '小时进入', v: cnt(isVeh ? ws.hour_vehicle_in : ws.hour_person_in) },
      { k: '小时离开', v: cnt(isVeh ? ws.hour_vehicle_out : ws.hour_person_out) },
      { k: '小时合计', v: cnt((isVeh ? ws.hour_vehicle_in + ws.hour_vehicle_out : ws.hour_person_in + ws.hour_person_out)) }
    ]
  })

  groups.push({
    title: '拥挤度指标',
    items: [
      { k: '拥堵判定', v: yesNo(ws.congested) },
      { k: '综合拥挤分', v: n(ws.congestion_score) },
      { k: '车流拥挤', v: yesNo(ws.vehicle_congested) },
      { k: '人流拥挤', v: yesNo(ws.person_congested) },
      { k: '车流评分', v: n(ws.vehicle_score) },
      { k: '人流评分', v: n(ws.person_score) },
      { k: '车流/分钟', v: n(ws.vehicle_flow_per_min) },
      { k: '人流/分钟', v: n(ws.person_flow_per_min) },
      { k: 'ROI 车辆', v: n(ws.roi_vehicles) },
      { k: 'ROI 人数', v: n(ws.roi_persons) }
    ]
  })

  return {
    title: slot.label(ri),
    subtitle: `${p.desc} · ${f((d && d.name) || ws.name)}（${f(devId)}）`,
    groups
  }
})

function openCamDetail(gateId, slotKey) {
  camDetailKey.value = { gateId, slotKey }
}
function closeCamDetail() {
  camDetailKey.value = null
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
    // 落点严格以后端经纬度为准：后端未匹配到坐标（null）时不落点，不再回退到城市中心
    const pos = dev.positions[d.id]
    if (!pos ||
      typeof pos.lng !== 'number' || typeof pos.lat !== 'number' ||
      !Number.isFinite(pos.lng) || !Number.isFinite(pos.lat)) {
      console.warn('[renderDevices] 设备无有效坐标（后端未匹配到经纬度），已跳过：', d.id)
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
/** 容器可用尺寸（叶子地图容器） */
function mapBox() {
  const el = document.getElementById('container')
  return { w: (el && el.clientWidth) || 1800, h: (el && el.clientHeight) || 1000 }
}

/** 定位到当前城市的区域范围（大同时连带把「大卡挂靠位 + 大卡本体」纳入取景）。 */
function fitCity() {
  const b = map && CITY[current] && CITY[current].bounds
  if (!map || !b) return
  const { w, h } = mapBox()
  const padX = Math.max(FLOW_ZONE_MIN_PAD_X, Math.min(380, Math.round(w * 0.21)))
  const padY = Math.max(60, Math.min(120, Math.round(h * 0.075)))
  let sw = [b[0][0], b[0][1]]
  let ne = [b[1][0], b[1][1]]
  // 大同：把「卡片挂靠位（框线外一点）+ 大卡本体」一起纳入取景。卡片随地图等比缩放，
  // 其地理占幅恒定（内层卡 ~500×289px，屏幕米/像素 × 缩放系数抵消后 ≈ 883×510m），
  // 外扩后 fitBounds 自动选到"8 张大卡全部落在两侧信息板之间"的缩放级。
  if (current === 'datong') {
    const midLat = (b[0][1] + b[1][1]) / 2
    // 挂靠外推量按卡片基准缩放级换算（与 badgeAnchorOf 同一口径），再加上卡宽/卡高
    const base = CITY[current].cardBaseZoom || CITY[current].zoom
    const clearM = CARD_BADGE_CLEAR_PX * metersPerPixelAt(base, midLat)
    const padXm = clearM + 883 + 30   // 徽标间距 + 卡宽 883m + 余量
    const padYm = clearM + 510 + 30   // 徽标间距 + 卡高 510m + 余量
    const mx = padXm / (111320 * Math.cos((midLat * Math.PI) / 180))
    const my = padYm / 110540
    sw = [sw[0] - mx, sw[1] - my]
    ne = [ne[0] + mx, ne[1] + my]
  }
  const bounds = L.latLngBounds(L.latLng(sw[1], sw[0]), L.latLng(ne[1], ne[0]))
  // 大同用信息板实际宽度做留白（左右各 ~23.5% 窗宽），保证卡环不被信息板遮挡
  const padX2 = current === 'datong' ? Math.max(370, Math.round(w * 0.235)) : padX
  map.fitBounds(bounds, {
    paddingTopLeft: [padX2, padY],
    paddingBottomRight: [padX2, padY]
  })
}

/* --------------------------------------------------------------------------
 * 框线「进出标识」
 *
 *   把 8 个城门点位垂直投影到最近一条古城框线边上的交会点（坐标在
 *   cityWallPoints.js 的 cross 字段，几何算定，非手工估点），在交会点渲染
 *   进 / 出 徽标；徽标两侧各有一段高亮短边，使框线在该处呈现"闸口断开"的
 *   观感 —— 即原先框线与点位交叉的地方改由进出标识表达。
 *
 *   进 = 箭头指向城内，出 = 箭头指向城外；箭头朝向由 edge（东/西/南/北墙）决定。
 *   配色直接复用主题令牌：进 → --c-flow-veh-*（绿）/ 出 → --c-flow-per-*（蓝）/
 *   待定 → --c-rp-muted，深浅主题切换纯由 CSS 完成，无需重绘地图。
 * ------------------------------------------------------------------------ */
function renderIoBadges() {
  if (ioLayer) { try { map.removeLayer(ioLayer) } catch (e) { } ioLayer = null }
  if (!map) return

  // 一门一个进出徽标（走门级集合）：32 路设备投影后 4 路交会点几乎重合，
  // 若按设备逐个画会叠成 4 层，故此处与门卡同源、一门只画一个。
  const pts = (gateCards.value || []).filter((p) => p && p.cross && p.cross.length >= 2)
  if (!pts.length) return

  ioLayer = L.layerGroup()
  pts.forEach((p) => {
    const kind = ioKindOf(p)                       // 'in' | 'out' | null（direction='其他' → 待定）
    const label = kind ? IO_LABEL[kind] : '待定'
    const axis = (p.edge === '北墙' || p.edge === '南墙') ? 'h' : 'v'
    const deg = kind ? ioArrowDeg(p.edge, kind) : 0
    const html =
      `<span class="io-badge" data-kind="${kind || 'tbd'}" data-axis="${axis}"` +
      ` title="${p.desc} · ${p.direction || '未指定'}">` +
      `<i class="io-jamb io-jamb-a"></i><i class="io-jamb io-jamb-b"></i>` +
      `<span class="io-body">` +
      (kind ? `<b class="io-arrow" style="transform:rotate(${deg}deg)">▶</b>` : '') +
      `<em class="io-txt">${label}</em>` +
      `</span>` +
      `</span>`
    L.marker(toLatLng(p.cross), {
      icon: L.divIcon({ className: 'io-badge-icon', html, iconSize: [0, 0], iconAnchor: [0, 0] }),
      interactive: false,
      zIndexOffset: 120
    }).addTo(ioLayer)
  })
  ioLayer.addTo(map)
}

function renderRegionOverlay() {
  if (!map) return
  ;[regionBorder, regionGlow, regionMask, regionLabel, regionHit, ioLayer].forEach((m) => {
    if (m) try { map.removeLayer(m) } catch (e) { }
  })
  regionBorder = regionGlow = regionMask = regionLabel = regionHit = ioLayer = null

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

  // 6) 框线「进出标识」：框线与点位交会处 → 进 / 出 闸口
  renderIoBadges()
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

/**
 * 初始化 tip：完整复刻参考页 public/FourLaneRoadPlan.html 的「大卡」版式
 * —— 卡头：点位名（h1）+ 属性副标题 + 编号/坐标 + 进出标签 + 状态胶囊
 * —— 主体：方位罗盘（上北下南）+ 双侧便道（斜纹条）· 四车道（2 实线 + 3 虚线）
 *          · 2 组摄像头 + 覆盖虚线 · 4 根车辆行驶方向指示线 · 两端方位标
 * —— 4 张小数据卡一排横贯断面全宽、贴下游侧：车流量 ×2（绿框）/ 人流量 ×2（蓝框）
 * —— 脚注图例不再逐卡渲染：集中展示于右侧信息栏 FlowLegendPanel（样式与时序预测卡一致）
 * 结构与参考页一一对应，尺寸按 tip 尺度重排（卡宽 380px，正文 7.5–15px）；
 * 颜色统一走 --c-rp-* / --c-flow-* 主题令牌 → 浅色 / 深色主题自动适配。
 *
 * 断面朝向：断面区（.rp-rot：便道 + 车道 + 方位标）按 roadInfoOf().rot 旋转，使图中箭头 = 真实行驶方向；
 *           方位标元素自身反旋（rotate(-rot)）→ 位置随图走、文字永远水平；
 *           小数据卡是 .rp-cross 直属子元素（不随断面旋转），坐标经 miniPos 映射到可视位置 → 天然水平。
 * 数据取自该门 4 路设备的实时统计（WS 主通道，每槽 = 该路设备「进 + 出」合计），
 * 数值随推送实时刷新（updateFlowTipContent）。
 * 入参 p 是门级大卡（gateCards，8 张），不是单路设备点位。
 */
function buildFlowTipHtml(p) {
  const ri = roadInfoOf(p)          // 走向 / 行驶方向 / 旋转角 / 城墙内外落位
  const stats = gateSlotStats(p)     // 4 槽位（左便道 / 摄像头A / 摄像头B / 右便道）
  const level = gateCardLevel(p)
  // 各槽位口径：该设备「进 + 出」合计；无设备 / 无实时数据 → —
  const num = (v) => (v == null ? '—' : Number(v).toLocaleString())
  const sv = (k) => stats[k] || {}

  // 车道：底部一根「车辆行驶方向指示线」（竖线 + 箭头）。它随断面整体一起旋转，
  // 所以 4 根箭头自动指向该门真实的车辆行驶方向（方向由 roadInfoOf 依城墙四至算出）。
  const lane = (n, name) =>
    `<div class="rp-lane" title="第 ${n} 车道 · 行驶方向${ri.flowPhrase}">` +
    `<span class="rp-lane-tag">${name}</span>` +
    `<span class="rp-lane-num">${n}</span>` +
    `<span class="rp-lane-flow"><i class="rp-fl-tail"></i><i class="rp-fl-head"></i></span>` +
    `</div>`
  const dash = `<div class="rp-line rp-dash"></div>`
  const cam = (x) =>
    `<div class="rp-anchor" style="--x:${x}%; --y:26%">` +
    `<span class="rp-cam"><i class="rp-cam-body"></i><i class="rp-cam-pole"></i></span>` +
    `</div>`
  const mini = (cls, x, y, title, rows, kind, slotKey) => {
    // 便道 / 车卡 类型角标（绿=车卡、蓝=便道，沿用原有高亮色令牌）
    const tag = kind === 'veh'
      ? `<span class="rp-kind rp-kind-veh">车卡</span>`
      : `<span class="rp-kind rp-kind-per">便道</span>`
    return (
      // data-slot = 该槽位归属（per0 / vehA / vehB / per1）；点击时按此定位到唯一一路设备
      `<div class="rp-mini ${cls}" data-slot="${slotKey}" title="点击查看该路设备接口数据详情"` +
      ` style="${x != null ? `--x:${x}%; ` : ''}--y:${y}%">` +
      `<div class="rp-mini-t"><span class="rp-dot"></span>${title}${tag}</div>` +
      `<div class="rp-rows">${rows}</div>` +
      `</div>`
    )
  }
  const cell = (fld, v, unit) => `<span><b data-fld="${fld}">${v}</b>${unit}</span>`

  // 小数据卡槽位（断面预旋坐标系，% of cross 盒）：y=85 为下游侧小卡带（原 87 贴近断面边缘，
  // 数值换行撑高后会被挤出卡片，故内收 2%）；四槽横贯全宽：左便道 12.5 → 摄像头A 37.5 → 摄像头B 62.5
  // → 右便道 87.5（均布，中心距 91.7px，故 .rp-mini 限宽 88px）。
  // 小卡是 .rp-cross 直属子元素（不随 .rp-rot 旋转），坐标需按断面 rot 旋转映射：
  // rot 0 → (x, y)；180 → (100−x, 100−y)；90 → (100−y, x)；−90 → (y, 100−x)
  const miniPos = (sx, sy) => {
    const r = ri.rot
    if (r === 180) return [100 - sx, 100 - sy]
    if (r === 90) return [100 - sy, sx]
    if (r === -90) return [sy, 100 - sx]
    return [sx, sy]
  }
  const [s0x, s0y] = miniPos(12.5, 85)   // 左便道（sideText[0]）
  const [vax, vay] = miniPos(37.5, 85)   // 摄像头A · 车道1-2
  const [vbx, vby] = miniPos(62.5, 85)   // 摄像头B · 车道3-4
  const [s1x, s1y] = miniPos(87.5, 85)   // 右便道（sideText[1]）

  return (
    `<div class="rp-card" data-id="${p.id}" data-level="${level}" data-kind="${ri.kind}" ` +
    `data-axis="${ri.axis}" data-rot="${ri.rot}" data-place="${ri.place}" style="--rot:${ri.rot}deg">` +
    // —— 卡头：名称 + 进出标签 + 状态 ——
    `<div class="rp-head">` +
    `<div class="rp-head-l">` +
    `<h1>${p.desc}</h1>` +
    `<div class="rp-sub">${p.typeLabel || ''}${p.type || ''} · 单向四车道（${ri.flowPhrase}）· 双侧便道</div>` +
    `</div>` +
    `<div class="rp-head-r">` +
    `<span class="rp-io" data-kind="${ri.kind}" title="${ri.kindLabel} · ${ri.dirLabel} · ${ri.intoCity ? '驶入城内' : '驶离城外'}">${ri.dirLabel}</span>` +
    `<button type="button" class="rp-status" data-level="${level}" title="查看该门 4 路视频设备详情"><i></i>详情</button>` +
    `</div>` +
    `</div>` +
    // —— 主体：道路断面（整体旋转到真实行驶方向）——
    `<div class="rp-body">` +
    `<div class="rp-cross" data-axis="${ri.axis}" data-rot="${ri.rot}">` +
    `<div class="rp-rot">` +
    `<div class="rp-sw rp-sw-l"><span class="rp-sw-label">便道</span></div>` +
    `<div class="rp-road">` +
    `<div class="rp-line rp-solid"></div>` +
    lane(1, '车道一') + dash + lane(2, '车道二') + dash + lane(3, '车道三') + dash + lane(4, '车道四') +
    `<div class="rp-line rp-solid"></div>` +
    cam(26) + cam(74) +
    `<div class="rp-cov rp-cov-1"></div><div class="rp-cov rp-cov-2"></div>` +
    `</div>` +
    `<div class="rp-sw rp-sw-r"><span class="rp-sw-label">便道</span></div>` +
    `</div>` +
    // —— 小数据卡：cross 直属子元素（不随断面旋转），一排 4 张横贯断面全宽、贴下游侧；
    //    槽位经 miniPos 映射后，卡的左右顺序与真实地理方位一致（绿=车卡、蓝=便道）——
    mini('rp-per', s0x, s0y, GATE_SLOTS[0].label(ri),
      cell('per-in-daily', num(sv('per0').daily), '人次/日') +
      cell('per-in-hourly', num(sv('per0').hourly), '人次/h'), 'per', 'per0') +
    mini('rp-veh', vax, vay, GATE_SLOTS[1].label(ri),
      cell('veh-in-daily', num(sv('vehA').daily), '辆/日') +
      cell('veh-in-hourly', num(sv('vehA').hourly), '辆/h'), 'veh', 'vehA') +
    mini('rp-veh', vbx, vby, GATE_SLOTS[2].label(ri),
      cell('veh-out-daily', num(sv('vehB').daily), '辆/日') +
      cell('veh-out-hourly', num(sv('vehB').hourly), '辆/h'), 'veh', 'vehB') +
    mini('rp-per', s1x, s1y, GATE_SLOTS[3].label(ri),
      cell('per-out-daily', num(sv('per1').daily), '人次/日') +
      cell('per-out-hourly', num(sv('per1').hourly), '人次/h'), 'per', 'per1') +
    `</div>` +
    `</div>`
  )
}

// —— 点位标记：Leaflet DivIcon ——
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
  clickTipLayer.setCardScale(cardScaleNow())
  clickTipLayer.update([{ id: p.id, x: pt.x, y: pt.y, html: buildTipHtml(p) }])
}
function closeClickTip() {
  if (clickTipLayer) clickTipLayer.update([])
}

// —— 默认车流提示框 ——
/**
 * 大卡锚点 = 框线「进 / 出」徽标（交会点 cross）沿所在墙边垂直法向、向城外偏
 * CARD_BADGE_CLEAR_PX 像素的位置。
 *
 * 为什么用「像素 → 米」换算而不是直接写死米数：
 *   卡片随地图等比缩放（cardScale = 2^(zoom−base)），徽标是 Leaflet marker 尺寸固定。
 *   若间距写死米数，缩小地图时间距会被压成 0，卡片会糊在徽标上；
 *   换算成"当前缩放级下的 N 像素"，则任意缩放级下卡片边缘与徽标都保持同一视觉间距。
 *
 * @param {object} p 点位（需带 cross、edge）
 * @returns {[number, number]|null} 锚点 [经度, 纬度]
 */
function badgeAnchorOf(p) {
  if (!map || !p) return null
  const lat = Array.isArray(p.cross) ? p.cross[1] : null
  if (!isFinite(lat)) return null
  // 缩放级取「卡片基准缩放」而非当前 zoom：这样外推量不随滚轮变化，
  // 卡片相对徽标的位置恒定（间距的视觉缩放天然由 cardScale 完成）
  const base = CITY[current].cardBaseZoom || CITY[current].zoom
  const offsetM = CARD_BADGE_CLEAR_PX * metersPerPixelAt(base, lat)
  return anchorOutOfCross(p, offsetM)
}

/**
 * 大数据卡 → 框线进出口 的虚线连接。
 * 两端都取自地图几何：起点 = 交会点 cross（进出徽标所在处），终点 = 大卡锚点
 * （徽标向城外偏 CARD_BADGE_CLEAR_PX 像素）。虚线为 Leaflet 图层，随地图平移/缩放自动重投影。
 */
function renderFlowConnLines() {
  if (flowConnLayer) { try { map.removeLayer(flowConnLayer) } catch (e) {} flowConnLayer = null }
  if (!map || current !== 'datong') return
  const pts = gateCards.value
  if (!pts || !pts.length) return
  const lines = []
  for (const p of pts) {
    if (!p || !p.cross) continue
    if (!isFinite(p.cross[0]) || !isFinite(p.cross[1])) continue
    const anchor = badgeAnchorOf(p)
    if (!anchor || !isFinite(anchor[0]) || !isFinite(anchor[1])) continue
    lines.push(L.polyline(
      [[p.cross[1], p.cross[0]], [anchor[1], anchor[0]]],
      { color: 'rgba(0, 225, 255, .75)', weight: 1.6, dashArray: '3 6', opacity: .85, interactive: false }
    ))
  }
  if (lines.length) flowConnLayer = L.layerGroup(lines).addTo(map)
}
function openFlowTips() {
  closeFlowTips()
  if (!map || current !== 'datong') return
  const pts = gateCards.value
  if (!pts || pts.length === 0) return
  if (!flowTipLayer) return
  updateFlowTips()
  renderFlowConnLines()
}
function closeFlowTips() {
  if (flowTipLayer) flowTipLayer.update([])
}
function updateFlowTips() {
  if (!map || !flowTipLayer) return
  if (current !== 'datong') { flowTipLayer.update([]); return }
  // 大卡走门级集合（8 门 × 4 路聚合 = 8 张）；标点仍是 32 路（renderDefaultMarkers）
  const pts = gateCards.value
  if (!pts || pts.length === 0) { flowTipLayer.update([]); return }
  const center = CITY.datong.center
  // 「古城四面图」式排序：同城门 → 南/北(西/东)两段 → 车卡在前便道在后 → 段内按坐标推进
  const sorted = sortPointsForZoneLayout(pts, center)
  const items = []
  for (const p of sorted) {
    // 锚点挂靠在框线「进/出」徽标上（cross 向城外偏 CARD_BADGE_CLEAR_PX 像素），
    // 不再使用点位自身 coord：卡片与框线进出口建立固定位置关系，跟着进出口走
    const anchor = badgeAnchorOf(p)
    if (!anchor || !isFinite(anchor[0]) || !isFinite(anchor[1])) continue
    const pt = map.latLngToContainerPoint(L.latLng(anchor[1], anchor[0]))
    const ri = roadInfoOf(p)
    items.push({
      id: p.id, x: pt.x, y: pt.y, html: buildFlowTipHtml(p),
      // 大卡落位：锚点已在徽标外侧，卡片主体再朝城外一侧展开（WALL_OUT_SIDE）
      place: WALL_OUT_SIDE[ri.wall] || 'top',
      zone: zoneOfPoint(p, center)   // 清远门→left / 和阳门→right / 永泰门→bottom / 武定门→top
    })
  }
  // 卡片缩放随地图 zoom 等比变化（与框线屏幕尺寸成正比）
  flowTipLayer.setCardScale(cardScaleNow())
  flowTipLayer.update(items)
}
function updateFlowTipContent() {
  if (!flowTipLayer) return
  flowTipLayer.updateFlowText((card, id) => {
    const p = findGateCard(id)
    if (!p) return
    const stats = gateSlotStats(p)
    const level = gateCardLevel(p)
    card.setAttribute('data-level', level)
    const setFld = (k, v) => { const el = card.querySelector('[data-fld="' + k + '"]'); if (el) el.textContent = v }
    const num = (v) => (v == null ? '—' : Number(v).toLocaleString())
    const s = (k) => stats[k] || {}
    // 4 槽位：口径 = 该路设备「进 + 出」合计（left 便道 / 摄像头A / 摄像头B / right 便道）
    setFld('per-in-daily', num(s('per0').daily))
    setFld('per-in-hourly', num(s('per0').hourly))
    setFld('veh-in-daily', num(s('vehA').daily))
    setFld('veh-in-hourly', num(s('vehA').hourly))
    setFld('veh-out-daily', num(s('vehB').daily))
    setFld('veh-out-hourly', num(s('vehB').hourly))
    setFld('per-out-daily', num(s('per1').daily))
    setFld('per-out-hourly', num(s('per1').hourly))
    // 「详情」按钮（点击展开左栏视频详情列表）：
    // 仅同步 data-level 驱动指示灯圆点颜色，文案保持「详情」不变
    const btn = card.querySelector('.rp-status')
    if (btn) btn.setAttribute('data-level', level)
  })
}

// —— 大卡「详情」→ 左栏视频详情列表数据 ——
// 4 路设备与大卡断面上的 4 张小数据卡一一对应（左便道 / 摄像头A / 摄像头B / 右便道），
// 名称按设备归属、数值口径与 mini 卡完全一致（该路设备「进 + 出」合计）
function buildVideoDevices(p) {
  const ri = roadInfoOf(p)
  const stats = gateSlotStats(p)
  return GATE_SLOTS.map((slot, i) => {
    const info = stats[slot.key]
    const isVeh = slot.dim === 'veh'
    const level = info ? info.level : 'abnormal'
    const num = (v) => (v == null ? '—' : Number(v).toLocaleString())
    const unit = isVeh ? ' 辆' : ' 人次'
    const unitH = isVeh ? ' 辆/h' : ' 人次/h'
    return {
      ch: String(i + 1).padStart(2, '0'),
      name: slot.label(ri), kind: slot.kind,
      status: statusTextOf(level, isVeh), level,
      active: p.lastActive || '—',
      rows: [
        [isVeh ? '今日车流' : '今日人流', num(info && info.daily) + unit],
        [isVeh ? '小时车流' : '小时人流', num(info && info.hourly) + unitH]
      ]
    }
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
  // 顺序关键：先 invalidateSize + 取景，让容器尺寸与投影基准定型，再渲染随地图定位的图层。
  // 若先渲染再取景，已按旧基准定位的框线/徽标/大卡会与取景后的地图错开。
  map.invalidateSize()
  fitCity()
  renderStaticLayers()
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
  fitCity()
  reprojectTips()
}

// —— 渲染静态叠加层 ——
/**
 * @param {boolean} [withFit] 是否顺带重新取景。默认 false：
 *   初始取景由 onMounted 的 fitAfterSized() 在容器尺寸稳定后统一负责，
 *   这里再 fit 一次会与它抢时序（两次 fitBounds 之间尺寸已变 → 投影基准不一致 → 标注整体偏移）。
 *   仅在「切换城市」这类确实需要重新取景的场景才传 true。
 */
function renderStaticLayers(withFit = false) {
  if (withFit) { try { fitCity() } catch (e) { console.error('[fitCity] 失败：', e) } }
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
      maxZoom: 18,       // 放开到 z18 给大数据卡留放大空间（2^1.5 ≈ 2.83 → 触顶 2.5 倍 ~950px）；
                         // z18 瓦片仅下载约 80%，缺失块由瓦片层 maxNativeZoom:17 拉伸兜底不露白
      zoomControl: true,
      attributionControl: false,
      zoomEnable: true, dragEnable: true,
      zoomSnap: 0.25
    })

    // 创建瓦片层（浅色/深色各一，按主题切换可见性）
    // maxNativeZoom: 17 → z18 时复用 z17 瓦片放大显示（本地瓦片 z18 覆盖不全，拉伸优于露白）
    tileLayerLight = L.tileLayer(TILE_URL_LIGHT, { attribution: TILE_ATTR, minZoom: 9, maxZoom: 18, maxNativeZoom: 17, errorTileUrl: ERROR_TILE, noWrap: true })
    tileLayerDark = L.tileLayer(TILE_URL_DARK, { attribution: TILE_ATTR, minZoom: 9, maxZoom: 18, maxNativeZoom: 17, errorTileUrl: ERROR_TILE, noWrap: true })
    if (theme.value === 'dark') {
      tileLayerDark.addTo(map)
    } else {
      tileLayerLight.addTo(map)
    }

    // 容器在挂载时可能尚未完成布局，强制重算尺寸，
    // 否则 Leaflet 会按初始 0 尺寸渲染，地图只显示一半 / 四周露白（"没铺满"主因）
    map.invalidateSize()

    // 初始取景必须放在尺寸稳定之后！
    // 早期版本在这里直接 fitCity()、但 250ms 后才 invalidateSize：fitBounds 是按
    // 「尚未定型的容器尺寸」算出中心的，之后尺寸一变，地图中心就不再是 fitBounds 的结果，
    // 表现为「所有地图标注相对框线整体偏移一个恒定值」（徽标/大卡纬度统一 −0.0042°≈466m）。
    // 现在两次 invalidateSize 都完成后再取景，并额外在下一帧做一次尺寸校正。
    const fitAfterSized = () => {
      if (!map) return
      map.invalidateSize()
      if (current === 'datong') fitCity()
      // 取景后重投影所有随地图定位的图层（框线/徽标/大卡锚点）
      try { renderRegionOverlay() } catch (e) { console.error('[renderRegionOverlay] 失败：', e) }
      try { updateFlowTips() } catch (e) { console.error('[updateFlowTips] 失败：', e) }
      try { renderFlowConnLines() } catch (e) { console.error('[renderFlowConnLines] 失败：', e) }
    }
    setTimeout(fitAfterSized, 260)
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
    // 车流提示层走「古城四面图」分区布局：卡片按城门进四条固定通道，彼此不再压叠
    flowTipLayer = new TipLayer({
      lineColor: overlayColors.value.regionStroke,
      showLines: false,
      strokeWidth: 2, lineOpacity: 0.8, gap: 56,
      maxLine: 420, minimumSpacing: 16,
      layoutMode: FLOW_LAYOUT_MODE,
      zoneSpacing: FLOW_ZONE_SPACING,
      // 视口自动裁剪：放大到单个城门时只显示视口内（锚点徽标可见）的卡，
      // 其余隐藏——DOM/数据保留在页面中，缩回总览自动全部恢复
      viewportCull: true,
      cullMargin: 40
    })
    const containerEl = document.getElementById('container')
    if (containerEl) {
      flowTipLayer.mount(containerEl)
      clickTipLayer = new TipLayer({
        lineColor: overlayColors.value.regionStroke,
        showLines: false,
        strokeWidth: 2, lineOpacity: 0.8, gap: 56,
        maxLine: 200, minimumSpacing: 12, zIndex: 9750
      })
      clickTipLayer.mount(containerEl)

      // 大卡内点击（事件委托，卡片 HTML 由 TipLayer 注入无法逐卡绑定）：
      //   ① 点槽位小数据卡（单个摄像头）→ 弹窗展示该路设备接口数据详情
      //   ② 点「详情」按钮 → 展开/收起左栏「警力分配」下方该门 4 路视频设备详情列表
      videoDetailClick = (e) => {
        if (!e.target || !e.target.closest) return
        const miniEl = e.target.closest('.rp-card .rp-mini')
        if (miniEl) {
          const card = miniEl.closest('.rp-card')
          const gateId = card && card.dataset.id
          const slotKey = miniEl.dataset.slot
          if (gateId && slotKey) openCamDetail(gateId, slotKey)
          return
        }
        const pill = e.target.closest('.rp-card .rp-status')
        if (!pill) return
        const card = pill.closest('.rp-card')
        const p = card && findGateCard(card.dataset.id)
        if (!p) return
        if (videoDetailState.gateId === p.id) { closeVideoDetail(); return }
        openVideoDetail(p.id, p.desc || p.id, buildVideoDevices(p))
      }
      containerEl.addEventListener('click', videoDetailClick)
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
    // 注：缩放过程中不再实时改卡片尺寸/位置（冻结在旧屏幕位），缩放结束（zoomend/moveend）
    // 再一次性精确重投影 + 重算 scale。否则「位置仍锚旧坐标、scale 却提前变化」会导致
    // 卡片尺寸变小却没跟着框线移动、松手才突跳的错位乱跑（上一版 bug 根因）。

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
  if (videoDetailClick) {
    const el = document.getElementById('container')
    if (el) el.removeEventListener('click', videoDetailClick)
    videoDetailClick = null
  }
  if (clickTipLayer) { clickTipLayer.destroy(); clickTipLayer = null }
  if (flowTipLayer) { flowTipLayer.destroy(); flowTipLayer = null }
  if (flowConnLayer) { try { map.removeLayer(flowConnLayer) } catch (e) {} flowConnLayer = null }
  if (map) {
    if (regionBorder) try { map.removeLayer(regionBorder) } catch { }
    if (regionGlow) try { map.removeLayer(regionGlow) } catch { }
    if (regionMask) try { map.removeLayer(regionMask) } catch { }
    if (regionLabel) try { map.removeLayer(regionLabel) } catch { }
    if (regionHit) try { map.removeLayer(regionHit) } catch { }
    if (ioLayer) try { map.removeLayer(ioLayer) } catch { }
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

// 标点源变化（32 路设备标点，一台一个）
watch(
  () => wallPoints.value.map((p) => p.id + ':' + p.status + ':' + (p.coord ? p.coord.join(',') : '')).join('|'),
  () => { renderDefaultMarkers() }
)

// 门级大卡源变化（8 门聚合：进出徽标 + 8 张大卡 + 连线）
watch(
  () => gateCards.value.map((p) =>
    p.id + ':' + p.status + ':' + (p.coord ? p.coord.join(',') : '') + ':' + Object.values(p.slots || {}).join(',')
  ).join('|'),
  () => { renderIoBadges(); openFlowTips() }
)

// 主题变化
watch(theme, () => applyTheme())
</script>