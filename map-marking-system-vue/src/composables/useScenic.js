/**
 * 首页（大屏）景区选择 —— 与 /static 运维页完全解耦，使用独立存储键
 * ---------------------------------------------------------------------------
 * 景区是首页贯穿全屏的「数据范围」条件：
 *   - 大屏：地图标点 / 门级大卡 / 交通指标 / 按天统计 / 告警提示 只统计所选景区
 *
 * 选项来自 GET /api/intersections/scenic-options：
 *   基线（大同古城 / 云冈石窟 / 悬空寺）∪ device_info.region ∪ intersection_info.scenic
 *
 * ⚠️ 与 /static 运维页解耦：运维页各自使用 global-scenic.js 的 localStorage('ops.globalScenic')，
 *    首页单独使用 localStorage('ops.homeScenic')，两者互不读写、互不监听，
 *    因此首页改景区不会污染运维页、运维页改景区也不会影响首页。
 * 空字符串 '' = 全部景区。
 */
import { reactive } from 'vue'
import { get } from '../api/client.js'

// 首页（大屏）专用键：与 /static 运维页的 ops.globalScenic 完全独立
const STORAGE_KEY = 'ops.homeScenic'
const FALLBACK = ['大同古城', '云冈石窟', '悬空寺']

function read() {
  try { return localStorage.getItem(STORAGE_KEY) || '' } catch { return '' }
}

const state = reactive({
  scenic: read(),              // '' = 全部景区
  options: FALLBACK.slice(),   // 下拉选项
  loaded: false,
  error: ''
})

/** 设置当前景区（同时持久化，供其它页面带入） */
function setScenic(v) {
  const val = String(v || '')
  state.scenic = val
  try { localStorage.setItem(STORAGE_KEY, val) } catch { /* 隐私模式忽略 */ }
}

let pending = null
/** 拉取景区选项（幂等；force=true 强制刷新） */
function loadOptions(force = false) {
  if (pending && !force) return pending
  pending = get('/api/intersections/scenic-options')
    .then((data) => {
      const list = (data && Array.isArray(data.options) ? data.options : []).filter(Boolean)
      if (list.length) state.options = list
      state.error = ''
    })
    .catch((e) => {
      // 接口不可用时保留内置基线，页面仍可切换
      state.error = (e && (e.detail || e.message)) || '景区选项加载失败'
    })
    .finally(() => { state.loaded = true })
  return pending
}

/** 设备是否属于当前景区（未登记景区的设备只在「全部」下出现） */
export function deviceInScenic(d, scenic) {
  if (!scenic) return true
  return String((d && d.region) || '').trim() === scenic
}

// —— 跨标签页同步（仅大屏之间：监听 own homeScenic 键，与 /static 运维页无关）——
if (typeof window !== 'undefined') {
  window.addEventListener('storage', (e) => {
    if (e.key === STORAGE_KEY) state.scenic = e.newValue || ''
  })
  // 从别的标签页切回来时重新读取（storage 事件在同页写入时不触发）
  window.addEventListener('focus', () => {
    const v = read()
    if (v !== state.scenic) state.scenic = v
  })
}

export function useScenic() {
  return { state, setScenic, loadOptions }
}
