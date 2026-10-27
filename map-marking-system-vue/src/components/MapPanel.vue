<template>
  <div>
    <div id="container"></div>

    <!-- 路口数据卡 · 一键全展开 / 收起（默认只在地图上显示「路口名称缩略牌」） -->
    <button
      type="button"
      class="gate-cards-toggle"
      :class="{ on: allGatesOpen }"
      :title="allGatesOpen ? '收起全部路口数据卡' : `展开全部路口数据卡（${gateCards.length} 张）`"
      @click="toggleAllGateCards"
    >
      <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
        <rect x="1.5" y="2.5" width="5" height="4" rx="1" fill="none" stroke="currentColor" stroke-width="1.3" />
        <rect x="9.5" y="2.5" width="5" height="4" rx="1" fill="none" stroke="currentColor" stroke-width="1.3" />
        <rect x="1.5" y="9.5" width="5" height="4" rx="1" fill="none" stroke="currentColor" stroke-width="1.3" />
        <rect x="9.5" y="9.5" width="5" height="4" rx="1" fill="none" stroke="currentColor" stroke-width="1.3" />
      </svg>
      <span>{{ allGatesOpen ? '收起全部路口卡' : '展开全部路口卡' }}</span>
    </button>

    <!-- 景区空态：所选景区既没有登记设备、也没有「路口数据卡」配置时，给出明确引导 -->
    <div v-if="scenic.state.scenic && !dev.loading && !dev.devices.length && !gateCards.length" class="scenic-empty">
      <span class="se-ic">🏞</span>
      <div class="se-tx">
        <b>景区「{{ scenic.state.scenic }}」暂无设备与路口配置</b>
        <i>请先在「设备管理」登记设备，或在「路口数据卡配置」中新增该景区的路口</i>
      </div>
    </div>

    <!-- 单路摄像头 · 实时数据详情弹窗（点大卡内某张槽位小卡打开） -->
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
          <div class="cdm-ft">数据随实时推送自动刷新 · 详情口径：该路设备「进 + 出」合计</div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup>
import { onMounted, onBeforeUnmount, ref, computed, watch } from 'vue'
import L from 'leaflet'
import { useDevices } from '../composables/useDevices.js'
import { useScenic } from '../composables/useScenic.js'
import { useIntersections } from '../composables/useIntersections.js'
import { useMapControl } from '../composables/useMapControl.js'
import { useToast } from '../composables/useToast.js'
import { useTheme } from '../composables/useTheme.js'
import { useVideoDetail } from '../composables/useVideoDetail.js'
import { sortPointsByPriority, sortPointsForZoneLayout, zoneOfPoint, ioKindOf, ioArrowDeg, IO_LABEL, roadInfoOf, anchorOutOfCross, metersPerPixelAt, CARD_BADGE_CLEAR_PX, WALL_OUT_SIDE, projectToWall } from '../data/cityWallPoints.js'
import { SCENIC_AOI } from '../data/scenicAoi.js'
import { TipLayer } from '../utils/tipLayer.js'

// 大屏为只读视图：设备的新增 / 配置 / 删除在运维页完成，此处只读设备与实时计数
const { state: dev, load, findById } = useDevices()
const { mapCtl } = useMapControl()
const { push } = useToast()
const { theme, overlayColors } = useTheme()
const { state: videoDetailState, open: openVideoDetail, close: closeVideoDetail } = useVideoDetail()
// 全局景区：dev.devices 已由 useDevices 按景区过滤，地图标点/门卡随之收敛
const scenic = useScenic()
// 路口卡片配置（intersection_info）：大卡的车道数/方向/便道/徽章与四槽设备绑定改由 DB 驱动
const inters = useIntersections()

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
  },
  xuankongsi: {
    center: [113.738, 39.672], zoom: 15,
    bounds: [[113.726, 39.660], [113.750, 39.684]],
    polygon: boundsToPolygon([[113.726, 39.660], [113.750, 39.684]])
  }
}
const CITY_NAME = { datong: '大同 · 古城', yungangshiku: '大同 · 云冈石窟', xian: '西安 · 雁塔', xuankongsi: '大同 · 悬空寺' }
const DEFAULT_CITY = 'datong'
let current = DEFAULT_CITY
// 地图城市 key → 景区名（intersection_info.scenic / device_info.region 的取值域），
// 用于「路口配置驱动」时按当前底图收敛可见路口，避免把远处路口的名牌画到当前视图外。
const SCENIC_OF_CITY = { datong: '大同古城', yungangshiku: '云冈石窟', xuankongsi: '悬空寺' }
// 城市切换计数器：current 是普通变量（非响应式），依赖 current 的 computed 靠它失效重算。
// 切换城市后必须 +1，否则 gateCards / wallPoints 会返回上一个城市的缓存结果。
const cityRev = ref(0)

// —— 景区 AOI（高德兴趣面边界，tools/fetch_scenic_aoi.py 生成 scenicAoi.js）——
/** 当前城市 AOI 环列表；rings 为空（未拉到数据）时返回空数组，由调用方回退 CITY.polygon */
function aoiRingsOf(key) {
  const a = SCENIC_AOI[key]
  return (a && Array.isArray(a.rings) ? a.rings.filter((r) => Array.isArray(r) && r.length >= 3) : [])
}
/** 环的近似面积（鞋带公式，仅用于比较大小选主环） */
function ringArea(r) {
  let s = 0
  for (let i = 0; i < r.length; i++) {
    const a = r[i], b = r[(i + 1) % r.length]
    s += a[0] * b[1] - b[0] * a[1]
  }
  return Math.abs(s / 2)
}
/** 显示/投影用主环：AOI 面积最大环；无 AOI 回退手工 polygon（单环） */
function displayPolyOf(key) {
  const rings = aoiRingsOf(key)
  if (rings.length) return rings.reduce((best, r) => (ringArea(r) > ringArea(best) ? r : best), rings[0])
  return CITY[key].polygon
}
/** 取景外包框：AOI 时由全部环的点集算 bounds；无 AOI 回退手工 bounds */
function displayBoundsOf(key) {
  const rings = aoiRingsOf(key)
  if (rings.length) {
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
    rings.forEach((r) => r.forEach((p) => {
      if (p[0] < minX) minX = p[0]; if (p[0] > maxX) maxX = p[0]
      if (p[1] < minY) minY = p[1]; if (p[1] > maxY) maxY = p[1]
    }))
    return [[minX, minY], [maxX, maxY]]
  }
  return CITY[key].bounds
}

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
let videoDetailClick = null // 大卡「视频」点击委托（挂 #container，卸载时移除）

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

// —— 工具函数 ——
function statusColor(s) {
  return s === 'online' ? 'g' : (s === 'offline' ? 'r' : 'y')
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
/**
 * 路口是否在配置里标记「大屏隐藏」（intersection_info.hidden）。
 * 该开关贯穿标点与大卡两条渲染链路：隐藏后该门的路口整卡、门级标点、
 * 进/出徽标、覆盖线、流媒体提示卡一并不再渲染。
 * 配置未加载 / 该门未配置时返回 false（不受影响，走原回退逻辑）。
 */
function isGateHidden(key) {
  const c = inters.state.byName[key]
  return !!(c && c.hidden)
}

/**
 * 当前底图景区内的「路口数据卡」配置记录（intersection_info）。
 * 这是大屏路口卡的【第一数据源】：只要配置里有这个路口（名称 + 经纬度），
 * 即便它一台设备都没登记，也要出名称牌 + 完整数据卡（设备只用来填实时数据）。
 * 景区为空（xian 等无基线景区）时按「未填景区」的记录取。
 */
function scenicCfgs() {
  void cityRev.value
  const sn = currentScenicName()
  return (inters.state.list || []).filter((r) => {
    if (!r || !r.name) return false
    const sc = String(r.scenic || '').trim()
    return sn ? sc === sn : !sc
  })
}

/**
 * 设备 → 断面（路口）归组键。
 * 先拿设备名 / 分类去匹配配置里的路口名（取最长命中，如「和阳北门」优先于「和阳」），
 * 匹配不到再退回 GATE_KEY_DEFS 的四大城门 —— 配置漏录的路口也不会丢设备。
 * @param {object} d 设备
 * @param {string[]} cfgNames 当前景区的路口名列表
 */
function gateKeyOfCfg(d, cfgNames) {
  const txt = `${(d && d.name) || ''} ${(d && d.category) || ''}`
  let hit = ''
  for (const n of cfgNames || []) {
    if (n && n.length >= 3 && txt.includes(n) && n.length > hit.length) hit = n
  }
  return hit || gateKeyOf(d).key
}

/** 当前底图城市对应的景区名（'xian' 等无对应景区 → ''，表示按「未填景区」的记录取） */
function currentScenicName() { return SCENIC_OF_CITY[current] || '' }
/** 当前底图城市的框线（优先高德 AOI 主环，无数据回退手工 polygon） */
function currentPoly() { return displayPolyOf(current) }
/** 路段名 → 大城门（和阳门 / 永泰门 / 清远门 / 武定门）；非古城路由口名原样返回 */
function majorGateOf(name) {
  if (!name) return ''
  for (const [g, kw] of [['和阳门', '和阳'], ['永泰门', '永泰'], ['清远门', '清远'], ['武定门', '武定']]) {
    if (name.includes(kw)) return g
  }
  return name
}

function backendWallPoints() {
  void cityRev.value
  const poly = currentPoly()
  const cfgNames = scenicCfgs().map((r) => r.name)
  const list = []
  dev.devices.forEach((d) => {
    if (!d || !d.id) return
    const pos = dev.positions[d.id]
    if (!pos || !Number.isFinite(pos.lng) || !Number.isFinite(pos.lat)) return
    // 断面归组键：优先匹配「路口数据卡」配置里的路口名（云冈南门等非古城路由口也能对上）
    const key = gateKeyOfCfg(d, cfgNames)
    // 大屏隐藏开关：隐藏路口的 32 路设备标点一并隐藏（与大卡保持一致）
    if (isGateHidden(key)) return
    // 设备级大屏显示开关（device_info.show_on_map，运维页「大屏显示」滑块）：
    // 关闭的设备不绘制经纬度点位（仅影响点位，门级大卡/小数据卡不受影响）。
    // 后端未返回该字段（未登记设备）时按显示处理。
    if (d.show_on_map === false) return
    const isKakou = isVehDevice(d)
    const coord = [pos.lng, pos.lat]
    const proj = projectToWall(coord, poly) || {}
    list.push({
      id: d.id, desc: d.name || d.id,
      type: isKakou ? '卡口' : '便道',
      gate: majorGateOf(key), direction: dirOf(d.name),
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
  // 槽位归属以设备名 / 点位分类为准: 便道(人流) → 左/右便道, 摄像头 / 卡口 → 车道 A/B。
  // camera_type 是 AI 检测目标过滤配置(测试时可能改配), 不能作为槽位归属依据。
  if (/摄像头A|车道1-2|车道1·2/.test(txt)) return 'vehA'
  if (/摄像头B|车道3-4|车道3·4/.test(txt)) return 'vehB'
  const side = /北侧/.test(txt) ? '北'
    : (/南侧/.test(txt) ? '南' : (/东侧/.test(txt) ? '东' : (/西侧/.test(txt) ? '西' : '')))
  if (/便道|人流/.test(txt)) {
    if (!side) return 'per0'
    return side === sideText[0] ? 'per0' : 'per1'
  }
  // 名称 / 分类都认不出时, 再按 camera_type + 出入口兜底
  if (isVehDevice(d)) return /出口/.test(txt) ? 'vehB' : 'vehA'
  if (!side) return 'per0'
  return side === sideText[0] ? 'per0' : 'per1'
}

/**
 * 单个「断面组」→ 一张大卡。
 * @param {{key:string, cfg:object|null, items:Array<{d:object,pos:object}>}} g 断面组
 * @param {object|null} cfg 该路口的路口数据卡配置（intersection_info 记录）
 * @param {Array} poly 当前底图城市的框线（用于把锚点投影成 cross/edge）
 * @returns {object|null} null = 该路口被隐藏 / 既无坐标又无设备（无法落点）
 */
function makeGateCard(g, cfg, poly) {
  const key = g.key
  // 大屏显示开关（intersection_info.hidden）：配置里标记隐藏的路口整卡不渲染
  // （含门级标点、流媒体提示卡与覆盖线）。接口未加载/未配置时不受影响。
  if (isGateHidden(key)) return null
  const istCfg = cfg || null
  const items = g.items || []
  // 锚点：intersection_info 配置经纬度优先（路口数据卡的 longitude/latitude）；
  // 未配置或非法值时回退该组卡口设备经纬度中点。配置坐标同样投影框线得 cross/edge，
  // 保证进/出徽章、大卡→框线连接线的锚定逻辑不变。
  let coord = null
  if (istCfg && istCfg.longitude != null && istCfg.latitude != null) {
    const lng = Number(istCfg.longitude)
    const lat = Number(istCfg.latitude)
    if (Number.isFinite(lng) && Number.isFinite(lat) && lng !== 0 && lat !== 0) coord = [lng, lat]
  }
  const vehItems = items.filter((x) => isVehDevice(x.d))
  if (!coord) {
    const anchorItems = vehItems.length ? vehItems : items
    if (!anchorItems.length) return null
    coord = [
      anchorItems.reduce((s, x) => s + x.pos.lng, 0) / anchorItems.length,
      anchorItems.reduce((s, x) => s + x.pos.lat, 0) / anchorItems.length
    ]
  }
  const proj = projectToWall(coord, poly) || {}
  // 方向必须扫全组：卡口设备名不含「入口/出口」，只看卡口会判成「其他」，
  // 进而 roadInfoOf 的 sideText 失真、便道左右认领出错。
  const firstItem = vehItems[0] || items[0]
  const direction = gateDirectionOf(items) || dirOf(firstItem && firstItem.d && firstItem.d.name)
  const ri = roadInfoOf({ edge: proj.edge || '东墙', direction })

  const slots = {}
  items.forEach((x) => {
    const slot = slotOfDevice(x.d, ri.sideText)
    if (!slots[slot]) slots[slot] = x.d.id
  })
  const statuses = items.map((x) => x.d.status || 'syncing')
  const latest = items.reduce((acc, x) => {
    const t = (x.d.last_heartbeat && String(x.d.last_heartbeat).slice(0, 10)) || ''
    return t > acc ? t : acc
  }, '')
  return {
    id: key, desc: key,
    type: '卡口', typeLabel: '车行',
    gate: majorGateOf(key), direction,
    coord, cross: proj.cross || null, edge: proj.edge || '东墙',
    lastActive: latest || '—',
    // 无设备的配置路口：状态记「离线」（槽位数据一律显示 —，不伪造 0）
    status: statuses.length ? (statuses.includes('online') ? 'online' : statuses[0]) : 'offline',
    count: 0, category: '', camera_type: '', slots,
    // 路口卡片配置（intersection_info）：存在则由 DB 驱动车道数/方向/便道/徽章/槽位绑定
    ist: istCfg
  }
}

/**
 * 大卡集合 = 「路口数据卡」配置驱动 + 设备聚合兜底。
 *
 * 1) 配置优先：当前景区里每一条 intersection_info 记录 ⇒ 一张卡（名称牌同名）。
 *    即使该路口一台设备都没登记（如云冈南门），也照常出名称牌与完整数据卡，
 *    槽位无设备 → 显示「—」，不再是「配置了却在大屏上看不见」。
 * 2) 设备兜底：配置里查不到的路口，仍按设备名聚合（配置漏录/接口不可用时不丢设备）。
 */
function backendGateCards() {
  void cityRev.value
  const poly = currentPoly()
  const cfgs = scenicCfgs()
  const cfgByName = new Map()
  cfgs.forEach((r) => { if (r && r.name) cfgByName.set(r.name, r) })
  const cfgNames = cfgs.map((r) => r.name)

  const groups = new Map()
  dev.devices.forEach((d) => {
    if (!d || !d.id) return
    const pos = dev.positions[d.id]
    if (!pos || !Number.isFinite(pos.lng) || !Number.isFinite(pos.lat)) return
    const key = gateKeyOfCfg(d, cfgNames)
    let g = groups.get(key)
    if (!g) { g = { key, cfg: cfgByName.get(key) || null, items: [] }; groups.set(key, g) }
    g.items.push({ d, pos })
  })

  const cards = []
  // 1) 配置驱动：每个「路口数据卡」配置一条（设备只用来填实时数据）
  cfgs.forEach((r) => {
    const g = groups.get(r.name) || { key: r.name, cfg: r, items: [] }
    const card = makeGateCard(g, r, poly)
    if (card) cards.push(card)
  })
  // 2) 设备驱动兜底：配置表里没有的断面（未录入「路口数据卡」）仍按设备聚合
  groups.forEach((g, key) => {
    if (cfgByName.has(key)) return
    const card = makeGateCard(g, cfgByName.get(key) || null, poly)
    if (card) cards.push(card)
  })
  return cards
}

/** 门级大卡集合 = 8 张（一步聚合 8 门）；标点仍用 wallPoints（32 路）。 */
const gateCards = computed(() => backendGateCards())
function findGateCard(id) { return gateCards.value.find((p) => p.id === id) }

/* --------------------------------------------------------------------------
 * 路口名称缩略牌（地图上的「路口卡入口」）
 *
 * 落点 = 门级大卡的锚点坐标 p.coord，即 intersection_info 里配置的
 * longitude / latitude（未配置时回退该门卡口设备坐标中点）——与「路口数据卡」
 * 保持同一数据源，配置改坐标 → 牌子随之移动。
 *
 * 交互：
 *   · 默认只显示 8 个名称缩略牌，8 张完整数据卡收起（大屏画面清爽）；
 *   · 点击某个牌 → 只展开该路口的完整数据卡（再次点击收起，点地图空白也收起）；
 *   · 顶部「展开全部路口卡」按钮 → 8 张卡按原徽标锚点布局全展开 / 全收起。
 * 隐藏开关（intersection_info.hidden）已经在 gateCards 里过滤，牌与卡同步消失。
 * ------------------------------------------------------------------------ */
const selectedGateId = ref(null)   // 当前展开的路口卡（null = 全部收起）
const allGatesOpen = ref(false)    // 一键全展开（与单卡展开互斥）
const gateNameMarkers = []         // [{ id, marker }]
const GATE_NAME_LIFT = 26          // 名称牌在坐标上方的视觉高度（px）：单卡展开时锚点抬到牌顶，避免卡片压住名称牌

/** 名称牌显示文案：路口名（超过 6 字截断），文案与「路口数据卡」名称完全一致 */
function gateNameText(p) {
  const t = String((p && (p.desc || p.id)) || '')
  return t.length > 6 ? t.slice(0, 6) + '…' : t
}
/** 名称牌 DOM：状态点 + 路口名 + 展开箭头；in/out 决定左侧色条（入口绿 / 出口蓝） */
function buildGateNameHtml(p) {
  const ri = roadInfoOf(p)
  const online = p.status === 'online'
  const open = isGateCardOpen(p.id)
  return `<div class="gn-pin ${ri.kind === 'in' ? 'in' : 'out'}${open ? ' active' : ''}" data-id="${p.id}">` +
    `<span class="gn-dot${online ? ' on' : ''}"></span>` +
    `<span class="gn-name">${gateNameText(p)}</span>` +
    `<span class="gn-ar">${open ? '▴' : '▾'}</span></div>`
}
/** 某路口卡是否处于展开态（单卡选中 或 全展开） */
function isGateCardOpen(id) {
  return allGatesOpen.value || selectedGateId.value === id
}
function clearGateNamePins() {
  if (!map) return
  gateNameMarkers.forEach((it) => map.removeLayer(it.marker))
  gateNameMarkers.length = 0
}
/** 渲染路口名称缩略牌（配置有几个路口就是几个；不限大同古城，云冈 / 悬空寺同样渲染） */
function renderGateNamePins() {
  clearGateNamePins()
  if (!map) return
  gateCards.value.forEach((p) => {
    if (!p || !p.coord || !isFinite(p.coord[0]) || !isFinite(p.coord[1])) return
    const mk = L.marker(toLatLng(p.coord), {
      icon: L.divIcon({
        className: 'gate-name-icon',
        html: buildGateNameHtml(p),
        iconSize: [0, 0],
        iconAnchor: [0, 0]
      }),
      // 高于设备标点（250/280+状态）与框线徽标，名称牌始终可点
      zIndexOffset: 600,
      title: `${p.desc || p.id} · 点击查看该路口数据卡`
    })
    mk.on('click', (e) => {
      if (e && e.originalEvent) L.DomEvent.stopPropagation(e.originalEvent)
      selectGateCard(p.id)
    })
    mk.addTo(map)
    gateNameMarkers.push({ id: p.id, marker: mk })
  })
}
/** 只同步名称牌的展开态 / 在线点（不重建 marker，避免 WS 每 2s 刷新时牌子闪动） */
function syncGateNamePinState() {
  gateNameMarkers.forEach((it) => {
    const p = findGateCard(it.id)
    const el = it.marker.getElement && it.marker.getElement()
    const pin = el && el.querySelector('.gn-pin')
    if (!p || !pin) return
    pin.classList.toggle('active', isGateCardOpen(it.id))
    pin.classList.toggle('in', roadInfoOf(p).kind === 'in')
    pin.classList.toggle('out', roadInfoOf(p).kind !== 'in')
    const dot = pin.querySelector('.gn-dot')
    if (dot) dot.classList.toggle('on', p.status === 'online')
    const ar = pin.querySelector('.gn-ar')
    if (ar) ar.textContent = isGateCardOpen(it.id) ? '▴' : '▾'
  })
}
/** 当前应展示的数据卡集合：全展开 → 8 张；单卡选中 → 1 张；否则 0 张 */
function visibleGateCards() {
  const all = gateCards.value
  if (!all.length) return all
  if (allGatesOpen.value) return all
  if (!selectedGateId.value) return []
  return all.filter((p) => p.id === selectedGateId.value)
}
/** 点击名称牌：展开该路口卡（全展开态下点牌 = 聚焦该卡），再点同一牌收起 */
function selectGateCard(id) {
  if (id == null) { closeGateCard(); return }
  if (!allGatesOpen.value && selectedGateId.value === id) { closeGateCard(); return }
  allGatesOpen.value = false
  selectedGateId.value = id
  syncGateNamePinState()
  openFlowTips()
}
function closeGateCard() {
  if (!selectedGateId.value && !allGatesOpen.value) return
  selectedGateId.value = null
  allGatesOpen.value = false
  syncGateNamePinState()
  openFlowTips()
}
/** 一键全展开 / 全收起 */
function toggleAllGateCards() {
  allGatesOpen.value = !allGatesOpen.value
  selectedGateId.value = null
  syncGateNamePinState()
  openFlowTips()
}

/** 摄像头组覆盖的车道范围（如 1-3）：按 card_style.lanes[].camera 分组取首末车道序号；
 *  未配置分组时按车道数对半分（前一半 A / 后一半 B），与运维页 defaultCamGroup 同规则 —— 车道数改动即联动。 */
function camLaneRangeOf(p, group) {
  const n = Math.min(10, Math.max(1, Number(p && p.ist && p.ist.lane_count) || 4))
  const lanes = (p && p.ist && p.ist.card_style && Array.isArray(p.ist.card_style.lanes)) ? p.ist.card_style.lanes : []
  const idx = []
  lanes.forEach((l, i) => { if (((l && l.camera) || '').trim() === group) idx.push(i + 1) })
  if (idx.length) return `${idx[0]}-${idx[idx.length - 1]}`
  const half = Math.floor(n / 2)
  return group === '摄像头A' ? (half > 0 ? `1-${half}` : '1') : `${half + 1}-${n}`
}

/** 门卡 4 槽位定义（顺序即横贯断面全宽的展示顺序）；vehA/vehB 覆盖范围随车道数/摄像头分组联动 */
const GATE_SLOTS = [
  { key: 'per0', dim: 'per', kind: 'per', label: (ri) => `${ri.sideText[0] || '北'}侧便道·人流` },
  { key: 'vehA', dim: 'veh', kind: 'veh', label: (ri, p) => `摄像头A·车道${camLaneRangeOf(p, '摄像头A')}` },
  { key: 'vehB', dim: 'veh', kind: 'veh', label: (ri, p) => `摄像头B·车道${camLaneRangeOf(p, '摄像头B')}` },
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

/* —— 槽位（可增删，数量不限）——
 * card_style.slots 支持两种形态：
 *   数组（新版）: [{ label, kind: 'veh'|'per', device }] —— 运维页可增删改，大屏按序渲染 N 张小卡
 *   对象（旧版）: { per0, vehA, vehB, per1 } → 自动转换为等价的 4 条
 * 未配置时回退：按设备名/分类自动认领的 4 槽（保持历史行为，未配置的路口渲染不中断）。
 */

/** 旧版对象槽位 / 其他形态 → 数组 */
function slotsToArray(slots) {
  if (Array.isArray(slots)) return slots
  if (slots && typeof slots === 'object') {
    const legacyLabel = { per0: '左侧便道·人流', vehA: '摄像头A', vehB: '摄像头B', per1: '右侧便道·人流' }
    return Object.entries(slots).map(([k, v]) => ({
      label: legacyLabel[k] || k,
      kind: k.startsWith('per') ? 'per' : 'veh',
      device: v || ''
    }))
  }
  return []
}

/** 门卡槽位列表：[{ key, label, kind, device }]（key 用于小卡 data-slot 与统计字段定位） */
function slotsOf(p, ri) {
  const cs = (p && p.ist && p.ist.card_style) || {}
  const arr = slotsToArray(cs.slots)
  if (arr.length) {
    return arr.map((s, i) => ({
      key: 's' + i,
      label: (s && s.label) || ('槽位' + (i + 1)),
      kind: (s && s.kind) === 'per' ? 'per' : 'veh',
      device: (s && s.device) || ''
    }))
  }
  // 未配置槽位 → 自动认领 4 槽（历史行为：左便道 / 摄像头A / 摄像头B / 右便道）
  return GATE_SLOTS.map((s) => ({
    key: s.key, label: s.label(ri, p), kind: s.kind,
    device: (!Array.isArray(cs.slots) && cs.slots && cs.slots[s.key]) || ''
  }))
}

/** 槽位 → 生效设备 id：显式绑定（device = 设备 id 或名称）优先；
 *  未绑定（或绑定的设备已不存在）则回退到按设备名/分类自动认领的 p.slots。 */
function slotDeviceOf(p, slot) {
  if (slot.device) {
    const d = dev.devices.find((x) => x.id === slot.device || x.name === slot.device)
    if (d) return d.id
  }
  return (p && p.slots && p.slots[slot.key]) || ''
}

/** 门卡槽位统计表（key = slotsOf 的 key）；槽位设备解析统一走 slotDeviceOf。 */
function gateSlotStats(p, ri) {
  const out = {}
  for (const s of slotsOf(p, ri || roadInfoOf(p))) {
    out[s.key] = slotStatOf(slotDeviceOf(p, s), s.kind)
  }
  return out
}

/** 门卡整体等级 = 全部槽位中最差的一路（任一路离线/异常 → 整卡随之降级）
 *  槽位一个数据都没有（该路口还没绑定任何设备）→ 记为离线，不显示成「异常」误导。 */
function gateCardLevel(p) {
  const stats = gateSlotStats(p)
  const list = Object.values(stats).filter(Boolean)
  if (!list.length) return 'offline'
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
  // 槽位：从路口配置的槽位列表解析（可增删）；设备绑定优先，未绑定回退自动认领
  const slot = slotsOf(p, roadInfoOf(p)).find((s) => s.key === k.slotKey)
  if (!p || !slot) return null
  const devId = slotDeviceOf(p, slot)
  const bindName = slot.device || ''
  const ri = roadInfoOf(p)
  const ws = devId ? (dev.statsById[devId] || null) : null
  const d = devId ? (dev.devices.find((x) => x.id === devId) || null) : null
  const isVeh = slot.kind === 'veh'
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
      { k: '槽位', v: `${slot.label}${bindName ? ' · 已绑定设备' : ' · 自动认领'}（${slot.kind === 'per' ? '便道' : '车辆卡口'}）` },
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
    return { title: slot.label, subtitle: `${p.desc} · ${devId || '未匹配设备'}`, groups }
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
    title: slot.label,
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

// —— 地图空白处点击：收起已展开的信息卡 ——
function onMapClick() {
  if (selectedPointId.value) selectPoint(null)
  // 点击地图空白：收起当前展开的路口数据卡（名称牌常驻）
  closeGateCard()
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
  const b = map && displayBoundsOf(current)
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
  } else {
    // 非古城底图：把该景区的「路口数据卡」配置经纬度一并纳入取景，
    // 避免路口落在 AOI 边界外（如云冈南门）时被取景框裁掉、看不见名牌。
    const sn = currentScenicName()
    ;(inters.state.list || []).forEach((r) => {
      if (!r || !r.name) return
      if (sn && String(r.scenic || '').trim() !== sn) return
      const lng = Number(r.longitude)
      const lat = Number(r.latitude)
      if (!Number.isFinite(lng) || !Number.isFinite(lat) || lng === 0 || lat === 0) return
      sw = [Math.min(sw[0], lng), Math.min(sw[1], lat)]
      ne = [Math.max(ne[0], lng), Math.max(ne[1], lat)]
    })
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
  // 进/出徽标是「古城框线闸口」语义：只有大同古城有城墙框线，其余景区不画
  if (current !== 'datong') return

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

  // 区域多环：优先高德 AOI（scenicAoi.js），无数据回退手工四角框线
  let rings = aoiRingsOf(current)
  if (!rings.length) rings = [CITY[current].polygon]
  if (!rings.length || rings[0].length < 3) return
  const c = overlayColors.value
  const center = CITY[current].center

  // 1) 圈外遮罩层：外大环 + AOI 各环做洞（Leaflet 原生支持多环镂空）
  const d = 3
  const outer = [
    toLatLng([center[0] - d, center[1] - d]),
    toLatLng([center[0] + d, center[1] - d]),
    toLatLng([center[0] + d, center[1] + d]),
    toLatLng([center[0] - d, center[1] + d])
  ]
  regionMask = L.polygon([outer, ...rings.map(toLatLngArr)], {
    color: 'transparent', fillColor: c.maskFill, fillOpacity: c.maskOp,
    interactive: false, pane: 'overlayPane'
  }).addTo(map)

  // 2) 边界外发光层（逐环）
  regionGlow = L.featureGroup(rings.map((r) => L.polygon(toLatLngArr(r), {
    color: c.regionStroke, weight: 7, opacity: 0.28, fill: false,
    interactive: false, pane: 'overlayPane'
  }))).addTo(map)

  // 3) 主边界（逐环）
  regionBorder = L.featureGroup(rings.map((r) => L.polygon(toLatLngArr(r), {
    color: c.regionStroke, weight: 2.5, opacity: c.regionStrokeOp,
    fillColor: c.polyFill, fillOpacity: c.polyFillOp,
    interactive: false, pane: 'overlayPane'
  }))).addTo(map)

  // 4) 区域标签（贴主环顶部，无 AOI 时与原行为一致）
  const mainPoly = displayPolyOf(current)
  const cy = mainPoly.reduce((s, p) => s + p[1], 0) / mainPoly.length
  regionLabel = L.marker(toLatLng([center[0], cy + 0.0009]), {
    icon: L.divIcon({
      className: 'region-label-icon',
      html: `<div class="region-label">🏯 ${CITY_NAME[current] || current} · 监控区域</div>`,
      iconSize: [0, 0], iconAnchor: [0, 0]
    }),
    interactive: false, zIndexOffset: 200
  }).addTo(map)

  // 5) 区域选中交互层（逐环命中）
  const isSel = selectedRegion.value === current
  regionHit = L.featureGroup(rings.map((r) => L.polygon(toLatLngArr(r), {
    color: 'transparent', weight: 0,
    fillColor: isSel ? c.regionStroke : 'transparent',
    fillOpacity: isSel ? 0.06 : 0,
    interactive: true
  }))).addTo(map)
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
 * 初始化 tip：「大卡」版式（2026-10-26 起断面与运维页 intersection-config.html 预览卡全量同构）
 * —— 卡头：点位名（h1）+ 属性副标题 + 进出徽章 + 状态/视频胶囊（大屏特有，保留）
 * —— 主体：配置驱动断面 —— .rc-road（南北向行车=竖放断面/东西向=横放断面）
 *          + 每车道 .rc-lane（箭头 .rc-arrow + 摄像头 .rc-cam + 车道名 .rc-lane-label）
 *          + 双侧便道 .rc-sw（斜纹条）+ 计数线（.rc-lane::before，count_line=false 时隐藏）
 * —— 小数据卡：.rc-stat-row 一排 .rc-stat（按 card_style.slots 数量增删），
 *          便道=蓝框 / 车卡=绿框（边框严格按槽位类型），带 data-slot / data-fld 供交互与刷新
 * 布局与配色全部走运维页同款规则（rc-* 类，见 main.css 末尾「配置页同构断面」段）；
 * 颜色统一映射 --c-rp-* / --c-flow-* 主题令牌 → 浅色 / 深色主题自动适配。
 *
 * 方向：roadInfoOf（配置 rotation 罗盘角优先 → flowDeg；未配置回退城墙边+进出几何推导）。
 *       断面正置渲染不旋转，靠 roadCls（ew/ns）与 bodyMode（小卡带上/下/左/右）表达朝向。
 * 车道宽度：配置 lane_width（米）→ px 基准（×26，钳制并收敛到卡宽），未配置等分。
 * 数据取自该门各路设备的实时统计（WS 主通道，每槽 = 该路设备「进 + 出」合计），
 * 数值随推送实时刷新（updateFlowTipContent）。
 * 入参 p 是门级大卡（gateCards，8 张），不是单路设备点位。
 */
function buildFlowTipHtml(p) {
  const ri = roadInfoOf(p)          // 走向 / 行驶方向 / 旋转角 / 城墙内外落位
  const stats = gateSlotStats(p, ri)  // 槽位统计（数量 = 配置的槽位数；未配置回退自动认领 4 槽）
  const level = gateCardLevel(p)
  // —— 路口卡片配置（intersection_info 驱动；未配置时全部回退到原写死值）——
  const cs = (p.ist && p.ist.card_style) || {}
  const CN_NUM = ['', '一', '二', '三', '四', '五', '六', '七', '八', '九', '十']
  const DEFAULT_LANE_NAMES = ['车道一', '车道二', '车道三', '车道四', '车道五', '车道六', '车道七', '车道八', '车道九', '车道十']
  const laneCount = Math.min(10, Math.max(1, Number(p.ist && p.ist.lane_count) || 4))
  const laneText = (p.ist && p.ist.lane_type === 'dual') ? '双向' : '单向'
  // 方向文案：配置 rotation 优先（与箭头方向严格一致），未配置才用自由文本 direction
  const dirText = (p.ist && p.ist.rotation != null && p.ist.rotation !== '')
    ? ri.flowPhrase : ((p.ist && p.ist.direction) || ri.flowPhrase)
  const kakouText = cs.kakou || ((p.typeLabel || '') + (p.type || '')) || '车行卡口'
  const swLeft = cs.sidewalk_left !== false
  const swRight = cs.sidewalk_right !== false
  const swText = swLeft && swRight ? '双侧便道' : (swLeft ? '左侧便道' : (swRight ? '右侧便道' : '无便道'))
  // 各槽位口径：该设备「进 + 出」合计；无设备 / 无实时数据 → —
  const num = (v) => (v == null ? '—' : Number(v).toLocaleString())
  const sv = (k) => stats[k] || {}

  // —— 断面 + 小数据卡：与运维页 intersection-config.html 预览卡同款 DOM/CSS（rc-* 类）——
  // 断面正置渲染不旋转（同运维页 buildRoad）：南北向行车 → 竖放断面(.rc-road.ew，车道竖条并排、
  // 便道左右)；东西向行车 → 横放断面(.rc-road.ns，车道横条堆叠、便道上下)。
  // 方向取值链：roadInfoOf（配置 rotation 优先 → flowDeg 罗盘角；回退城墙几何推导）。
  const ARROW_ROT = { 0: 180, 90: 270, 180: 0, 270: 90 }   // 罗盘角 → 箭头旋转角（箭头基准朝下）
  const rotN = ((Number(ri.flowDeg) % 360) + 360) % 360      // 归一化罗盘角（0北/90东/180南/270西）
  const arrowBase = ARROW_ROT[rotN] != null ? ARROW_ROT[rotN] : 180
  // 布局类名与 orientation 相反（同运维页）：南北向行车（rotN 0/180）→ 'ew'（row 横排车道条）
  const roadCls = (rotN === 90 || rotN === 270) ? 'ns' : 'ew'
  // 小数据卡带落位（与运维页 bodyMode 一致）：0北→下 / 180南→上 / 90东→左列 / 270西→右列
  const bodyMode = rotN === 0 ? 'col minis-last'
    : rotN === 180 ? 'col minis-first'
      : rotN === 90 ? 'row minis-first' : 'row minis-last'
  // 车道宽度：配置 lane_width（米）→ 基准 px（×26，钳制 24~400，同运维页 lanePx），
  // 再按大卡可容纳段数收敛（防 380px 卡溢出）；未配置 → 空（CSS flex:1 等分，行为同旧版）。
  const nSeg = laneCount + (swLeft ? 1 : 0) + (swRight ? 1 : 0)
  const lwNum = Number(p.ist && p.ist.lane_width)
  const lanePxRaw = Number.isFinite(lwNum) && lwNum > 0
    ? Math.min(Math.max(Math.round(lwNum * 26), 24), 400)
    : null
  // 竖放断面(ns)按高度收敛（避免卡过高），横放断面(ew)按卡宽收敛（避免横向溢出）
  const lanePx = lanePxRaw
    ? (roadCls === 'ew' ? Math.min(lanePxRaw, Math.floor(340 / Math.max(2, nSeg))) : Math.min(lanePxRaw, 64))
    : null
  const laneSize = lanePx
    ? `flex:0 0 ${lanePx}px;${roadCls === 'ew' ? `width:${lanePx}px;` : `height:${lanePx}px;`}`
    : ''
  // 车道列表：优先 card_style.lanes（逐车道 label/camera/dir），回退按 lane_count 生成同向车道
  const cfgLanes = (Array.isArray(cs.lanes) && cs.lanes.length)
    ? cs.lanes.slice(0, 10).map((x) => ({
      label: (x && x.label) || '', camera: (x && x.camera) || '',
      dir: (x && x.dir) === 'backward' ? 'backward' : 'forward'
    }))
    : Array.from({ length: laneCount }, (_, i) => ({
      label: DEFAULT_LANE_NAMES[i] || ('车道' + (i + 1)), camera: '', dir: 'forward'
    }))
  // 车道名挂箭尖一侧（tail-*，同运维页 tailClassOf）
  const tailCls = (deg) => {
    const n = ((deg % 360) + 360) % 360
    const head = n === 0 ? 'down' : n === 90 ? 'left' : n === 180 ? 'up' : 'right'
    return 'tail-' + ({ down: 'bottom', up: 'top', left: 'left', right: 'right' }[head])
  }
  // 摄像头：与运维页 CAM_SVG 同款 SVG，位于 .rc-arrow-wrap 旋转组内箭头尾部一侧
  const camSvg =
    '<svg class="rc-cam" width="36" height="26" viewBox="0 0 18 13" xmlns="http://www.w3.org/2000/svg">' +
    '<rect x="0" y="2" width="11" height="9" rx="1.5" fill="#ff5252"/>' +
    '<path d="M12 5 L17 2.5 V10.5 L12 8 Z" fill="#ff5252"/>' +
    '<circle cx="5.5" cy="6.5" r="2.2" fill="var(--c-rp-road-bg)"/></svg>'
  const lanesHtml = cfgLanes.map((l) => {
    const deg = arrowBase + (l.dir === 'backward' ? 180 : 0)
    return `<div class="rc-lane ${tailCls(deg)}"${laneSize ? ` style="${laneSize}"` : ''}>` +
      `<span class="rc-arrow-wrap" style="--arrow-rot:${deg}deg">` +
      `<span class="rc-arrow"></span>${l.camera ? camSvg : ''}</span>` +
      `<span class="rc-lane-label">${l.label}</span></div>`
  }).join('')
  const swHtml =
    `<div class="rc-sw"${laneSize ? ` style="${laneSize}"` : ''}>` +
    `<span class="rc-sw-label"><i class="rc-btd">便道</i></span></div>`
  const noCount = cs.count_line === false ? ' no-count' : ''
  // rc-fluid：未配置 lane_width 时断面铺满卡宽（否则 max-content + 空车道无固有宽度会塌成 0）
  const roadHtml =
    `<div class="rc-road ${roadCls}${noCount}${lanePx ? '' : ' rc-fluid'}">` +
    (swLeft ? swHtml : '') + lanesHtml + (swRight ? swHtml : '') +
    `</div>`
  // 小数据卡：与运维页 .stat-box 同款（person=蓝框 / vehicle=绿框，边框严格按槽位类型）。
  // data-slot / data-fld 与旧 .rp-mini 完全同名 —— 点击弹窗与 WS 增量刷新逻辑不变。
  const slotList = slotsOf(p, ri)
  const statsHtml = `<div class="rc-stat-row">` + slotList.map((s) => {
    const st = sv(s.key)
    const per = s.kind === 'per'
    return `<div class="rc-stat ${per ? 'person' : 'vehicle'}" data-slot="${s.key}" title="点击查看该路设备实时数据">` +
      `<div class="rc-stat-t"><span class="rc-dot"></span><i class="rc-btd">${s.label}</i>` +
      `${String(s.label || '').includes(per ? '便道' : '车卡') ? '' : `<span class="rc-stat-tag">${per ? '便道' : '车卡'}</span>`}</div>` +
      `<div class="rc-metric"><b data-fld="${s.key}-daily">${num(st.daily)}</b> ${per ? '人次/日' : '辆/日'}</div>` +
      `<div class="rc-metric"><b data-fld="${s.key}-hourly">${num(st.hourly)}</b> ${per ? '人次/h' : '辆/h'}</div>` +
      `</div>`
  }).join('') + `</div>`

  return (
    `<div class="rp-card" data-id="${p.id}" data-level="${level}" data-kind="${ri.kind}" ` +
    `data-axis="${ri.axis}" data-rot="${ri.rot}" data-place="${ri.place}" style="--rot:${ri.rot}deg">` +
    // —— 卡头：名称 + 进出标签 + 状态 ——
    `<div class="rp-head">` +
    `<div class="rp-head-l">` +
    `<h1>${p.desc}</h1>` +
    `<div class="rp-sub">${kakouText} · ${laneText}${CN_NUM[laneCount] || laneCount}车道（${dirText}）· ${swText}</div>` +
    `</div>` +
    `<div class="rp-head-r">` +
    `<span class="rp-io" data-kind="${ri.kind}" title="${ri.kindLabel} · ${ri.dirLabel} · ${ri.intoCity ? '驶入城内' : '驶离城外'}">${(p.ist && p.ist.badge) || ri.dirLabel}</span>` +
    // 文案「视频」：与槽位小卡的「数据详情」区分 —— 此处展开的是该门 4 路视频设备
    `<button type="button" class="rp-status" data-level="${level}" title="查看该门 4 路视频设备"><i></i>视频</button>` +
    `</div>` +
    `</div>` +
    // —— 主体：配置驱动断面 + 小数据卡带（与运维页预览卡完全同构，正置渲染不旋转）——
    `<div class="rp-body">` +
    `<div class="rc-body ${bodyMode}">` +
    statsHtml +
    `<div class="rc-stage">${roadHtml}</div>` +
    `</div>` +
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

// 标点状态类名（与 buildPinContent 产出的一一对应）
const PIN_STATUS_CLS = ['s-online', 's-offline', 's-abnormal', 's-syncing']
const PIN_FLOW_CLS = ['flow-red', 'flow-yellow', 'flow-green']

/**
 * 同步单个标点的「状态 / 拥挤色 / 选中悬停」表现。
 *
 * 只改已有 DOM 的 class，不再 setIcon 重建图标：
 *   - setIcon 会整体替换 icon 元素，导致 hover / active 高亮闪断（WS 每 2s 推一次就闪一次），
 *     且每次 32 个标点全量重建，带来无谓的 DOM/GC 开销；
 *   - 标点图标内部只有 class 会变（状态色点 + 拥挤色角标），改 class 即可。
 */
function setMarkerMode(mk, p, mode) {
  if (!mk) return
  const root = mk.getElement && mk.getElement()
  const pin = root && root.querySelector('.cam-pin')
  if (!pin) return
  const info = resolvePointInfo(p)
  const stCls = info.status === 'online' ? 's-online'
    : (info.status === 'offline' ? 's-offline'
      : (info.status === 'abnormal' ? 's-abnormal' : 's-syncing'))
  PIN_STATUS_CLS.forEach((c) => pin.classList.toggle(c, c === stCls))
  pin.classList.toggle('active', mode === 'active')
  pin.classList.toggle('hover', mode === 'hover')
  const badge = pin.querySelector('.badge')
  if (badge) {
    const colorCls = info.congCls === 'r' ? 'flow-red' : (info.congCls === 'y' ? 'flow-yellow' : 'flow-green')
    PIN_FLOW_CLS.forEach((c) => badge.classList.toggle(c, c === colorCls))
  }
}

// —— 点击交互提示框 ——
function updateClickTip() {
  if (!map || !clickTipLayer) return
  if (!selectedPointId.value) { clickTipLayer.update([]); return }
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
  // 「徽标 → 大卡」虚线只为全展开布局服务（卡片贴徽标外侧）；
  // 单卡聚焦时卡片贴名称牌，画这条线会指向别处，故不绘制。
  if (!allGatesOpen.value) return
  const pts = visibleGateCards()
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
  if (!map) return
  const pts = visibleGateCards()
  if (!pts || pts.length === 0) return
  if (!flowTipLayer) return
  updateFlowTips()
  renderFlowConnLines()
}
function closeFlowTips() {
  if (flowTipLayer) flowTipLayer.update([])
}
/**
 * 贴名称牌展开时，按「屏幕剩余空白」决定卡片朝哪一侧展开。
 * 左右各有 288px 信息板、顶部 73px 顶栏不算可用空间；优先选「放得下」的方向里
 * 空间最大的（都不放得下时选空间最大的，TipLayer 会再做视口钳制兜底）。
 * 云冈南门这类落在 AOI 东缘的路口，若固定朝右展开会被右侧信息板压住。
 * @param {{x:number,y:number}} pt 锚点屏幕坐标（已在名称牌上方）
 * @param {number} cardW 卡片缩放后估算宽度(px)
 * @param {number} cardH 卡片缩放后估算高度(px)
 */
function freeSidePlace(pt, cardW, cardH) {
  const el = document.getElementById('container')
  const W = (el && el.clientWidth) || 1600
  const H = (el && el.clientHeight) || 900
  const PAD = 34
  const L = 288 + 16, R = 288 + 16, T = 73 + 16, B = 48
  const cands = [
    ['left', pt.x - L - PAD, cardH],
    ['right', W - R - pt.x - PAD, cardH],
    ['top', pt.y - T - PAD, cardW],
    ['bottom', H - B - pt.y - PAD, cardW]
  ]
  let best = null
  for (const c of cands) {
    const fits = c[1] >= c[2]
    if (!best || (fits && !best.fits) || (fits === best.fits && c[1] > best.free)) {
      best = { side: c[0], free: c[1], fits }
    }
  }
  return best.side
}
function updateFlowTips() {
  if (!map || !flowTipLayer) return
  // 展开集合：全展开 = 全部路口卡（原徽标锚点布局）；单卡聚焦 = 1 张（贴路口名称缩略牌）；
  // 未展开 = 0 张（地图上只留名称牌）。标点始终按设备逐台绘（renderDefaultMarkers）。
  const pts = visibleGateCards()
  if (!pts || pts.length === 0) { flowTipLayer.update([]); return }
  const singleFocus = !allGatesOpen.value && !!selectedGateId.value
  // 非古城底图（云冈 / 悬空寺）没有「城墙进出徽标」这套几何，卡片一律贴名称牌锚点展开
  const coordAnchor = singleFocus || current !== 'datong'
  const sc = Math.max(cardScaleNow(), 1)   // 贴名称牌展开时的缩放（不低于 1，保证可读）
  const center = CITY[current].center
  // 「古城四面图」式排序：同城门 → 南/北(西/东)两段 → 车卡在前便道在后 → 段内按坐标推进
  const sorted = sortPointsForZoneLayout(pts, center)
  const items = []
  for (const p of sorted) {
    // 单卡聚焦 / 非古城底图：锚点 = 名称牌正上方（名称牌落点即路口配置经纬度），卡片贴着牌子展开；
    // 全展开（古城）：沿用框线「进/出」徽标外侧锚点，8 张卡沿框线外环排开（保持原布局）。
    let pt = null
    if (coordAnchor && p.coord && isFinite(p.coord[0]) && isFinite(p.coord[1])) {
      const cpt = map.latLngToContainerPoint(L.latLng(p.coord[1], p.coord[0]))
      pt = L.point(cpt.x, cpt.y - GATE_NAME_LIFT)
    } else {
      const anchor = badgeAnchorOf(p)
      if (!anchor || !isFinite(anchor[0]) || !isFinite(anchor[1])) continue
      pt = map.latLngToContainerPoint(L.latLng(anchor[1], anchor[0]))
    }
    const ri = roadInfoOf(p)
    let html = ''
    // 单张卡渲染异常只跳过该卡，不影响其余门卡
    try { html = buildFlowTipHtml(p) } catch (e) { continue }
    // 落位：古城徽标锚点 → 朝城外一侧；贴名称牌（云冈等）→ 按屏幕剩余空白选展开侧
    const place = coordAnchor
      ? (current === 'datong'
        ? (WALL_OUT_SIDE[ri.wall] || 'top')
        : freeSidePlace(pt, 420 * sc, 340 * sc))
      : (WALL_OUT_SIDE[ri.wall] || 'top')
    items.push({
      id: p.id, x: pt.x, y: pt.y, html,
      place,
      zone: zoneOfPoint(p, center)   // 清远门→left / 和阳门→right / 永泰门→bottom / 武定门→top
    })
  }
  // 卡片缩放随地图 zoom 等比变化（与框线屏幕尺寸成正比）；
  // 贴名称牌展开时不低于 1（否则默认视图 0.38 倍下 7.5px 设计字号会被缩到看不清）。
  flowTipLayer.setCardScale(coordAnchor ? sc : cardScaleNow())
  flowTipLayer.update(items)
}
function updateFlowTipContent() {
  if (!flowTipLayer) return
  flowTipLayer.updateFlowText((card, id) => {
    const p = findGateCard(id)
    if (!p) return
    const ri = roadInfoOf(p)
    const stats = gateSlotStats(p, ri)
    const level = gateCardLevel(p)
    card.setAttribute('data-level', level)
    const setFld = (k, v) => { const el = card.querySelector('[data-fld="' + k + '"]'); if (el) el.textContent = v }
    const num = (v) => (v == null ? '—' : Number(v).toLocaleString())
    // 槽位增量刷新：按配置的槽位列表逐槽更新（数量可增删，字段 key = 槽位 key）
    for (const s of slotsOf(p, ri)) {
      const st = stats[s.key] || {}
      setFld(s.key + '-daily', num(st.daily))
      setFld(s.key + '-hourly', num(st.hourly))
    }
    // 「视频」按钮（点击展开右栏视频详情列表）：
    // 仅同步 data-level 驱动指示灯圆点颜色，文案保持「视频」不变
    const btn = card.querySelector('.rp-status')
    if (btn) btn.setAttribute('data-level', level)
  })
}

// —— 大卡「视频」→ 右栏视频详情列表数据 ——
// 4 路设备与大卡断面上的 4 张小数据卡一一对应（左便道 / 摄像头A / 摄像头B / 右便道），
// 名称按设备归属、数值口径与 mini 卡完全一致（该路设备「进 + 出」合计）
//
// 可播放判定：WS stats.devices 会为 device_info 里所有点位补行，未注册点位用「名称」当
// device_id；这些只是点位展示，不是真实设备（无国标 id / 流地址），点播必然 404。
// 只有真正注册过（有国标通道或流地址）的设备才允许点击播放。
function isPlayableDevice(devId) {
  const d = devId ? dev.devices.find((x) => x.id === devId) : null
  return !!(d && (d.gb_device_id || d.gb_channel_id || d.stream_url))
}
function buildVideoDevices(p) {
  const ri = roadInfoOf(p)
  const stats = gateSlotStats(p, ri)   // 槽位设备解析已含「绑定优先」（slotDeviceOf）
  return slotsOf(p, ri).map((slot, i) => {
    const info = stats[slot.key]
    const isVeh = slot.kind === 'veh'
    const level = info ? info.level : 'abnormal'
    const num = (v) => (v == null ? '—' : Number(v).toLocaleString())
    const unit = isVeh ? ' 辆' : ' 人次'
    const unitH = isVeh ? ' 辆/h' : ' 人次/h'
    const devId = slotDeviceOf(p, slot)
    return {
      ch: String(i + 1).padStart(2, '0'),
      id: isPlayableDevice(devId) ? devId : '', // 仅真实注册设备可播放
      name: slot.label, kind: slot.kind,
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
  const pts = wallPoints.value
  if (!pts || pts.length === 0) {
    // 云冈 / 悬空寺等尚无设备登记的景区：路口卡由配置驱动照常显示，
    // 这里不再弹「暂无监控点位」，避免每次重绘都弹一次提示
    if (current === 'datong') push('该区域暂无监控点位', 'warn')
    return
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
  // current 非响应式：+1 让依赖它的 computed（gateCards / wallPoints）失效重算
  cityRev.value++
  selectedPointId.value = null
  selectedGateId.value = null
  allGatesOpen.value = false
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

/**
 * 定位到指定设备（顶栏搜索结果 / 告警条目点击 → 地图跳转并展开该设备信息卡）。
 * 无地图坐标的设备给出明确提示，不做静默失败。
 * @param {string} deviceId 设备编号（= 地图标点 id）
 */
function focusDevice(deviceId) {
  if (!deviceId) return
  closeCamDetail()
  // 设备所属景区 → 对应底图城市（云冈 / 悬空寺的设备不再被强行拉回大同古城）
  const dev0 = dev.devices.find((x) => x.id === deviceId)
  const cityOfDev = scenicToCity(String((dev0 && dev0.region) || '').trim())
  if (cityOfDev && current !== cityOfDev) changeCity(cityOfDev)
  const pos = dev.positions[deviceId]
  if (!map || !pos || !Number.isFinite(pos.lng) || !Number.isFinite(pos.lat)) {
    push('设备「' + deviceId + '」暂无地图坐标，无法定位', 'warn')
    return
  }
  map.flyTo([pos.lat, pos.lng], Math.max(map.getZoom(), 17), { duration: 0.6 })
  // 跳转后展开信息卡；投影随 moveend 由 updateClickTip 重新计算
  if (findPoint(deviceId)) selectPoint(deviceId)
}

/**
 * 定位到指定门（顶栏检索「门」→ 飞到该门大卡 + 闪烁一次）。
 * 大卡由 TipLayer 注入 DOM，按 data-id 找到后加一次性动画 class；
 * 飞行结束时再闪一次（大卡随 moveend 重投影，确保到位后仍能看见）。
 * @param {string} gateId 门断面名（= 大卡 id，如「和阳北门」）
 */
function focusGate(gateId) {
  if (!gateId) return
  closeCamDetail()
  // 该路口所属景区 → 对应底图城市（云冈南门定位时不会切回大同古城）
  const rec = inters.state.byName[gateId]
  const cityOfGate = scenicToCity(String((rec && rec.scenic) || '').trim())
  if (cityOfGate && current !== cityOfGate) changeCity(cityOfGate)
  const p = findGateCard(gateId)
  if (!map || !p || !p.coord || !Number.isFinite(p.coord[0]) || !Number.isFinite(p.coord[1])) {
    push('门「' + gateId + '」暂无地图坐标，无法定位', 'warn')
    return
  }
  map.flyTo(toLatLng(p.coord), Math.max(map.getZoom(), 17), { duration: 0.6 })
  // 检索定位一个路口：该卡处于收起态时自动展开（否则「定位到空处」）；
  // 已展开（单卡或全展开）则只飞行 + 闪烁，不改变展开集合。
  if (!isGateCardOpen(p.id)) {
    allGatesOpen.value = false
    selectedGateId.value = p.id
    syncGateNamePinState()
    openFlowTips()
  }
  // 卡片 DOM 由 TipLayer 在下一帧注入，延后两帧再闪烁
  requestAnimationFrame(() => requestAnimationFrame(() => flashGateCard(gateId)))
  map.once('moveend', () => flashGateCard(gateId))
}

/** 门级大卡一次性闪烁（重放动画：先移除 class 再强制重排） */
function flashGateCard(gateId) {
  const el = document.getElementById('container')
  const card = el && el.querySelector('.rp-card[data-id="' + gateId + '"]')
  if (!card) return
  card.classList.remove('rp-flash')
  void card.offsetWidth
  card.classList.add('rp-flash')
  if (gateFlashTimer) clearTimeout(gateFlashTimer)
  gateFlashTimer = setTimeout(() => {
    card.classList.remove('rp-flash')
    gateFlashTimer = null
  }, 1800)
}

/** Esc：按「最上层先关」的顺序收起单路详情弹窗 → 视频详情面板 */
function onKeyDown(e) {
  if (e.key !== 'Escape') return
  if (camDetailKey.value) { closeCamDetail(); return }
  if (videoDetailState.gateId) closeVideoDetail()
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
  try { renderGateNamePins() } catch (e) { console.error('[renderGateNamePins] 失败：', e) }
  try { openFlowTips() } catch (e) { console.error('[openFlowTips] 失败：', e) }
}

// —— 待清理的定时器 / 监听（卸载时统一释放，避免 HMR 或路由切换后残留回调）——
let fitTimer = null      // 容器尺寸稳定后的首次取景
let staticTimer = null   // 首次静态图层渲染
let gateFlashTimer = null // 门级大卡定位闪烁的复位定时器
function onWindowResize() {
  if (map) map.invalidateSize()
}

// —— 生命周期 ——
onMounted(async () => {
  try {
    current = scenicToCity(scenic.state.scenic) || DEFAULT_CITY

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
      if (CITY[current] && CITY[current].bounds) fitCity()
      // 取景后重投影所有随地图定位的图层（框线/徽标/大卡锚点）
      try { renderRegionOverlay() } catch (e) { console.error('[renderRegionOverlay] 失败：', e) }
      try { updateFlowTips() } catch (e) { console.error('[updateFlowTips] 失败：', e) }
      try { renderFlowConnLines() } catch (e) { console.error('[renderFlowConnLines] 失败：', e) }
    }
    fitTimer = setTimeout(fitAfterSized, 260)
    window.addEventListener('resize', onWindowResize)

    // 比例尺控件
    L.control.scale({ position: 'bottomleft', imperial: false }).addTo(map)

    map.on('click', onMapClick)
    window.addEventListener('keydown', onKeyDown)

    // 注册到控制总线
    mapCtl.ready = true
    mapCtl.changeCity = changeCity
    mapCtl.changeStyle = changeStyle
    mapCtl.resetView = resetView
    mapCtl.setTopView = setTopView
    mapCtl.setOblique = setOblique
    mapCtl.fitRange = fitRange
    mapCtl.toggleFree3D = toggleFree3D
    mapCtl.focusDevice = focusDevice
    mapCtl.focusGate = focusGate
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
      //   ① 点槽位小数据卡（单个摄像头）→ 弹窗展示该路设备实时数据
      //   ② 点「视频」按钮 → 展开/收起右栏该门 4 路视频设备列表
      //   ③ 点大卡其余区域 → 飞到该门并放大到合适比例（z17 ≈ 卡片 1.4 倍，门区占屏适中）
      videoDetailClick = (e) => {
        if (!e.target || !e.target.closest) return
        // 槽位小数据卡（新类 .rc-stat，旧类 .rp-mini 兼容保留）
        const miniEl = e.target.closest('.rp-card .rc-stat, .rp-card .rp-mini')
        if (miniEl) {
          const card = miniEl.closest('.rp-card')
          const gateId = card && card.dataset.id
          const slotKey = miniEl.dataset.slot
          if (gateId && slotKey) openCamDetail(gateId, slotKey)
          return
        }
        const pill = e.target.closest('.rp-card .rp-status')
        if (pill) {
          const card = pill.closest('.rp-card')
          const p = card && findGateCard(card.dataset.id)
          if (!p) return
          if (videoDetailState.gateId === p.id) { closeVideoDetail(); return }
          openVideoDetail(p.id, p.desc || p.id, buildVideoDevices(p))
          return
        }
        const cardEl = e.target.closest('.rp-card')
        if (cardEl && cardEl.dataset.id) focusGate(cardEl.dataset.id)
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
    staticTimer = setTimeout(renderStaticOnce, 500)

    // 容器尺寸修正
    requestAnimationFrame(() => {
      try { if (map) map.invalidateSize() } catch (e) { }
    })

    // 加载设备（标点由 wallPoints 变化驱动渲染）
    load().catch(() => { })
    // 加载路口卡片配置（intersection_info）：大卡车道数/方向/便道/徽章 + 四槽设备绑定
    inters.load().catch(() => { })

    // 地图事件：重投影 TipLayer
    map.whenReady(() => { renderStaticOnce(); updateFlowTips(); updateClickTip() })
    map.on('moveend', () => { updateFlowTips(); updateClickTip() })
    map.on('zoomend', () => { updateFlowTips(); updateClickTip() })
    map.on('resize', () => { updateFlowTips(); updateClickTip() })
    // 注：缩放过程中不再实时改卡片尺寸/位置（冻结在旧屏幕位），缩放结束（zoomend/moveend）
    // 再一次性精确重投影 + 重算 scale。否则「位置仍锚旧坐标、scale 却提前变化」会导致
    // 卡片尺寸变小却没跟着框线移动、松手才突跳的错位乱跑（上一版 bug 根因）。
  } catch (e) {
    console.error(e)
    push('⚠ 地图初始化失败：' + (e && e.message ? e.message : e), 'error')
  }
})

onBeforeUnmount(() => {
  // 释放定时器与全局监听：否则卸载后仍会回调已销毁的地图实例（invalidateSize 报错）
  if (fitTimer) { clearTimeout(fitTimer); fitTimer = null }
  if (staticTimer) { clearTimeout(staticTimer); staticTimer = null }
  if (gateFlashTimer) { clearTimeout(gateFlashTimer); gateFlashTimer = null }
  window.removeEventListener('resize', onWindowResize)
  window.removeEventListener('keydown', onKeyDown)
  clearPointMarkers()
  clearGateNamePins()
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

// 实时统计变化 → 只做「状态同步 + 文本改写」的增量更新（不重建标点 DOM）
// statsRev 由 useDevices 在每次写入统计后自增，替代对 statsById 的 deep watch（省去全量深度遍历）
watch(() => dev.statsRev, () => {
  refreshWallPointPins()
  updateFlowTipContent()
  if (selectedPointId.value) updateClickTip()
})

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
  () => {
    renderIoBadges(); renderGateNamePins(); openFlowTips()
    // 顶栏「设备检索」按门检索需要门级列表（搜索门名 → 定位到该门大卡）
    mapCtl.gates = gateCards.value.map((p) => ({
      id: p.id, gate: p.gate, edge: p.edge, direction: p.direction,
      status: p.status, coord: p.coord
    }))
  }
)

// 路口配置晚到时重画大卡：上方门卡 watcher 的 key 只含 id/status/coord/slots，
// 不含 intersection_info 字段——若 inters.load() 晚于设备渲染完成（两者并发竞速），
// 配置到达不会触发任何 key 变化 → 大卡停留在回退值（写死的「单向四车道·双侧便道」等），
// 直到下一次状态变化。byName 每次 reindex 都换新对象（引用变化必触发），补上这次重绘。
watch(() => inters.state.byName, () => { renderIoBadges(); renderGateNamePins(); openFlowTips(); renderDefaultMarkers() })

// 主题变化
watch(theme, () => applyTheme())

// 景区名称 → 地图城市 key（与后端 scenic-options 基线、CITY 配置保持一致）
function scenicToCity(name) {
  if (name === '云冈石窟') return 'yungangshiku'
  if (name === '悬空寺') return 'xuankongsi'
  if (name === '大同古城') return 'datong'
  return '' // 全部景区 / 未知景区 → 回落默认全景
}
// 切换景区：联动地图取景（大同古城 / 云冈石窟 / 悬空寺 各自飞到对应底图区域）+ 收起残留详情卡
function applyScenicToMap() {
  const city = scenicToCity(scenic.state.scenic)
  if (!city) { if (current !== 'datong') changeCity('datong'); return }
  if (current !== city) changeCity(city)
}
watch(() => scenic.state.scenic, () => {
  if (selectedPointId.value && !findPoint(selectedPointId.value)) selectPoint(null)
  if (camDetailKey.value) closeCamDetail()
  applyScenicToMap()
})
</script>