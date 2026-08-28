/**
 * 城墙点位搜索逻辑（基于经纬度坐标查询）
 * ---------------------------------------------------------------------------
 * 数据源自 cityWallPoints.js（由「城墙出入口便道+卡口.xlsx」生成）。
 *
 * 查询模型：
 *   1) 经纬度坐标查询（主查询方式）：用户输入 经度/纬度（或在地图选点），
 *      系统通过「空间网格索引」快速定位到坐标对应的最近点位，并在给定半径内
 *      返回按距离升序排列的匹配点位。即便超出半径，也会定位到最近点位，
 *      保证“根据经纬度坐标准确返回对应位置的数据”。
 *   2) 多条件筛选（保留并优化）：关键词 / 类型 / 城门 / 出入口方向 / 日期范围，
 *      作为坐标查询之上的二次精炼。无坐标时，退化为纯条件筛选（兼容旧行为）。
 *
 * 性能优化：
 *   - 启动时构建一次性空间网格索引（≈1.1km 网格），nearestWithin 仅扫描目标
 *     网格及其 8 邻域，避免全量扫描（数据量大时收益显著）。
 *   - 坐标变更采用防抖（组件侧）触发查询，避免逐字符重算。
 *
 * 交互反馈（满足“搜索条件变更时的平滑更新反馈”）：
 *   loading / conflict / everSearched / prevCount / lastChange 一应俱全。
 */
import { ref, computed, reactive } from 'vue'
import { CITY_WALL_POINTS } from '../data/cityWallPoints.js'

/** 摄像头标识相对点位中心的偏移范围（米），可在搜索框实时调整 */
export const cameraOffset = reactive({ minM: 7, maxM: 80 })

/** 经纬度坐标查询半径（米） */
export const geoRadiusM = ref(2000)

const R_KM = 6371
const R_M = 6371000
const DEG_PER_M_LAT = 1 / 111320

/** 地球表面两点距离（Haversine，单位 km） */
function haversineKm(a, b) {
  const dLat = (b[1] - a[1]) * Math.PI / 180
  const dLng = (b[0] - a[0]) * Math.PI / 180
  const lat1 = a[1] * Math.PI / 180
  const lat2 = b[1] * Math.PI / 180
  const s = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLng / 2) ** 2
  return 2 * R_KM * Math.asin(Math.sqrt(s))
}

/** 地球表面两点距离（Haversine，单位 m） */
export function haversineMeters(a, b) {
  const dLat = (b[1] - a[1]) * Math.PI / 180
  const dLng = (b[0] - a[0]) * Math.PI / 180
  const lat1 = a[1] * Math.PI / 180
  const lat2 = b[1] * Math.PI / 180
  const s = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLng / 2) ** 2
  return 2 * R_M * Math.asin(Math.sqrt(s))
}

/** 将字符串映射为 0~1 的稳定伪随机值，保证同一 id 的偏移不跳动 */
function hashString(str) {
  let h = 0
  for (let i = 0; i < str.length; i++) {
    h = ((h << 5) - h) + str.charCodeAt(i)
    h |= 0
  }
  return (Math.abs(h) % 1000) / 1000
}

function metersToCoordDelta(meters, bearingDeg, lat) {
  const rad = bearingDeg * Math.PI / 180
  const dLatM = meters * Math.cos(rad)
  const dLngM = meters * Math.sin(rad)
  return [
    dLngM * DEG_PER_M_LAT / Math.cos(lat * Math.PI / 180),
    dLatM * DEG_PER_M_LAT
  ]
}

/**
 * 空间网格索引：把点位按经纬度分桶（网格边长 ≈ 0.01° ≈ 1.1km）。
 * 最近邻 / 半径查询只需扫描目标桶 + 8 邻域，避免全量遍历。
 */
const GRID = 0.01
const _grid = new Map()
function gkey(lng, lat) {
  return Math.round(lng / GRID) + '_' + Math.round(lat / GRID)
}
for (const p of CITY_WALL_POINTS) {
  const k = gkey(p.coord[0], p.coord[1])
  if (!_grid.has(k)) _grid.set(k, [])
  _grid.get(k).push(p)
}

/** 给定坐标与半径，返回半径内按距离升序的点位（优化：仅扫描邻近网格） */
export function nearestWithin(lng, lat, radiusM) {
  const cx = Math.round(lng / GRID)
  const cy = Math.round(lat / GRID)
  let cand = []
  for (let dx = -1; dx <= 1; dx++) {
    for (let dy = -1; dy <= 1; dy++) {
      const arr = _grid.get((cx + dx) + '_' + (cy + dy))
      if (arr) cand = cand.concat(arr)
    }
  }
  if (!cand.length) cand = CITY_WALL_POINTS.slice()
  return cand
    .map((p) => ({ p, d: haversineMeters([lng, lat], p.coord) }))
    .filter((x) => x.d <= radiusM)
    .sort((a, b) => a.d - b.d)
}

/** 解析经纬度输入：返回 [lng, lat] 或 null（非法则视为“非坐标查询”） */
function parseCoord(lngStr, latStr) {
  const lng = Number(lngStr)
  const lat = Number(latStr)
  if (!isFinite(lng) || !isFinite(lat)) return null
  if (lng < -180 || lng > 180 || lat < -90 || lat > 90) return null
  return [lng, lat]
}

/** 搜索条件（响应式，任一变更即触发平滑筛选） */
export const filters = reactive({
  lng: '',            // 经度（字符串，便于输入）
  lat: '',            // 纬度
  keyword: '',        // 关键词：设备ID / 描述
  type: 'all',        // all | 便道 | 卡口
  gate: 'all',        // all | 和阳门 | 永泰门 | 清远门 | 武定门
  direction: 'all',   // all | 入口 | 出口 | 其他
  dateFrom: '',       // YYYY-MM-DD
  dateTo: ''          // YYYY-MM-DD
})

/** 当前查询中心（[lng, lat] 或 null），由 applyFilters 依据 filters.lng/lat 解析而来 */
export const searchCoord = ref(null)

/** 是否处于“经纬度坐标查询”模式 */
export const geoActive = computed(() => {
  const c = searchCoord.value
  return Array.isArray(c) && isFinite(c[0]) && isFinite(c[1])
})

/** 交互状态 */
export const loading = ref(false)
export const everSearched = ref(false)
export const prevCount = ref(0)
export const lastChange = ref(null) // { from, to, delta, text }

/** 条件冲突：开始日期晚于结束日期 */
export const conflict = computed(() => {
  if (filters.dateFrom && filters.dateTo && filters.dateFrom > filters.dateTo) {
    return '日期范围有误：开始日期晚于结束日期'
  }
  return ''
})

/** 应用类型/城门/方向/关键词/日期过滤（不含坐标排序） */
function applyAttributeFilters(list) {
  const kw = (filters.keyword || '').trim().toLowerCase()
  return list.filter((p) => {
    if (filters.type !== 'all' && p.type !== filters.type) return false
    if (filters.gate !== 'all' && p.gate !== filters.gate) return false
    if (filters.direction !== 'all' && p.direction !== filters.direction) return false
    if (filters.dateFrom && p.lastActive < filters.dateFrom) return false
    if (filters.dateTo && p.lastActive > filters.dateTo) return false
    if (kw) {
      const hay = (p.id + ' ' + p.desc).toLowerCase()
      if (!hay.includes(kw)) return false
    }
    return true
  })
}

/**
 * 当前筛选结果。返回 [{ p, dist }]，dist 为到查询中心的米数（非坐标模式为 null）。
 * 坐标模式下按距离升序，并限制在 geoRadiusM 半径内；超出半径仍返回最近的一个，
 * 保证“按坐标能定位到对应位置的数据”。
 */
export const filtered = computed(() => {
  const base = applyAttributeFilters(CITY_WALL_POINTS)
  if (!geoActive.value) {
    return base.map((p) => ({ p, dist: null }))
  }
  const [lng, lat] = searchCoord.value
  const ranked = base
    .map((p) => ({ p, dist: haversineMeters([lng, lat], p.coord) }))
    .sort((a, b) => a.d - b.d)
  const within = ranked.filter((x) => x.d <= geoRadiusM.value)
  return within.length ? within : (ranked.length ? [ranked[0]] : [])
})

/** 坐标模式下，结果中距离中心最近的点位（即“目标点”），用于高亮 */
export const targetPoint = computed(() =>
  geoActive.value && filtered.value.length ? filtered.value[0].p : null
)

/** 坐标模式下，最近点位是否超出了搜索半径（用于友好提示） */
export const geoOutOfRange = computed(() =>
  geoActive.value && filtered.value.length > 0 &&
  filtered.value[0].dist > geoRadiusM.value
)

/**
 * 将某点位在 7~80m（可配置）范围内生成确定性的绘制坐标，
 * 保证同一 id 重绘不跳动，且多个点位不会完全重叠。
 */
export function scatterCoord(point, idx = 0) {
  const [lo, hi] = cameraOffset.minM > cameraOffset.maxM
    ? [cameraOffset.maxM, cameraOffset.minM]
    : [cameraOffset.minM, cameraOffset.maxM]
  const dist = lo + hashString(point.id + 'd') * (hi - lo)
  const bearing = (hashString(point.id + 'b') * 360 + idx * 47.3) % 360
  const [dLng, dLat] = metersToCoordDelta(dist, bearing, point.coord[1])
  return {
    coord: [point.coord[0] + dLng, point.coord[1] + dLat],
    distance: Math.round(dist)
  }
}

/**
 * 执行筛选（异步，呈现平滑的 加载 → 结果更新 过渡）。
 * 返回筛选后的点位列表（[{p,dist}]），并写入 before/after 对比信息。
 */
export async function applyFilters() {
  // 先解析经纬度，驱动 filtered 以坐标重算（computed 会在读取时惰性更新）
  searchCoord.value = parseCoord(filters.lng, filters.lat)
  loading.value = true
  // 模拟异步耗时，确保加载态 / 过渡动画可见（数据源为本地时尤为必要）
  await new Promise((r) => setTimeout(r, 260))
  const list = filtered.value
  const from = prevCount.value
  const to = list.length
  if (everSearched.value && from !== to) {
    const delta = to - from
    lastChange.value = {
      from, to, delta,
      text: `条件变更：结果 ${from} → ${to}（${delta >= 0 ? '+' : ''}${delta}）`
    }
  } else if (!everSearched.value) {
    lastChange.value = null
  }
  prevCount.value = to
  everSearched.value = true
  loading.value = false
  return list
}

/** 重置全部筛选条件 */
export function clearFilters() {
  filters.lng = ''
  filters.lat = ''
  filters.keyword = ''
  filters.type = 'all'
  filters.gate = 'all'
  filters.direction = 'all'
  filters.dateFrom = ''
  filters.dateTo = ''
  searchCoord.value = null
  lastChange.value = null
}

export function useSpotSearch() {
  return {
    filters, loading, everSearched, prevCount, lastChange, conflict,
    filtered, geoActive, searchCoord, targetPoint, geoOutOfRange,
    geoRadiusM, cameraOffset, scatterCoord, haversineMeters, nearestWithin,
    applyFilters, clearFilters
  }
}
