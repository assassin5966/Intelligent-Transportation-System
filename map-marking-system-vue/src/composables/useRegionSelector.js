/**
 * useRegionSelector —— 在 Vue3 项目中便捷使用 Canvas 框选工具的组合式封装。
 * 与现有的 useSpotSearch / useTheme 等同级，统一 <script setup> 调用风格。
 */
import { ref, onBeforeUnmount } from 'vue'
import RegionSelector from '../utils/regionSelector'

export function useRegionSelector(options) {
  const regions = ref([])   // 已框选区域（响应式，供 UI 展示）
  const active = ref(false) // 是否处于框选模式
  let selector = null

  function ensure() {
    if (!selector) {
      // options 可传入主题色，例如 { strokeColor: '#00e1ff', fillColor: 'rgba(0,225,255,.12)' }
      selector = new RegionSelector(options)
    }
    return selector
  }

  function start() {
    ensure().enable()
    active.value = true
  }

  function stop() {
    if (selector) selector.disable()
    active.value = false
    regions.value = selector ? selector.getRegions() : []
  }

  function toggle() {
    active.value ? stop() : start()
  }

  function clear() {
    if (selector) selector.clear()
    regions.value = []
  }

  // 组件卸载时关闭框选，避免全局事件泄漏
  onBeforeUnmount(() => {
    if (selector) selector.disable()
  })

  return { regions, active, start, stop, toggle, clear }
}
