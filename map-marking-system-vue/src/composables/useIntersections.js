/**
 * 路口卡片配置（intersection_info）—— 大屏门级大卡的数据源
 * ---------------------------------------------------------------------------
 * 大卡原本把「车道数 / 行驶方向 / 卡口类型 / 双侧便道 / 徽章」写死在代码里，
 * 现改为读取 MySQL intersection_info（经 GET /api/intersections 暴露）：
 *   - lane_count    → 渲染几条车道
 *   - lane_type     → 单向 / 双向
 *   - direction     → 行驶方向文案
 *   - badge         → 入口 / 出口 / 出入口
 *   - card_style.kakou / sidewalk_left / sidewalk_right / lanes[] → 卡口名、便道、每车道标签
 *   - card_style.slots = { per0, vehA, vehB, per1 } → 四槽位显式绑定设备（视频播放路径）
 *
 * 接口不可用 / 某门未配置时，调用方回退到原有按设备名聚合的逻辑，渲染不中断。
 */
import { reactive } from 'vue'
import { get } from '../api/client.js'

const state = reactive({
  list: [],
  byName: {},   // 路口名（如「清远南门」）→ 记录
  byId: {},     // intersection_info.id → 记录
  loaded: false,
  error: ''
})

let pending = null

function reindex(list) {
  const byName = {}
  const byId = {}
  for (const r of list) {
    if (!r) continue
    if (r.name) byName[r.name] = r
    if (r.id != null) byId[r.id] = r
  }
  state.byName = byName
  state.byId = byId
}

/** 拉取路口列表（幂等；force=true 强制刷新）。 */
function load(force = false) {
  if (pending && !force) return pending
  pending = get('/api/intersections')
    .then((data) => {
      const list = Array.isArray(data) ? data : []
      state.list = list
      reindex(list)
      state.error = ''
    })
    .catch((e) => {
      // 接口不可用时保留空表：调用方全部走原有回退逻辑，不阻塞大屏
      state.error = (e && (e.detail || e.message)) || '路口配置加载失败'
    })
    .finally(() => { state.loaded = true })
  return pending
}

/** 按路口名取配置（未配置返回 null）。 */
function byName(name) {
  return state.byName[name] || null
}

/** 按 id 取配置（未配置返回 null）。 */
function byId(id) {
  return state.byId[id] || null
}

export function useIntersections() {
  return { state, load, byName, byId }
}
