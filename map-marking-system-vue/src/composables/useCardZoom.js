/**
 * 卡片双击放大 —— 全局共享状态
 * ---------------------------------------------------------------------------
 * 用一个模块级 ref 保存「当前放大的卡片」描述，StatBoard 等处的卡片通过
 * openCardZoom({ title, component, props }) 触发，App 中挂载的 <CardZoomModal>
 * 读取该状态并把对应组件以大尺寸重新渲染到全屏遮罩里。
 *
 * 为什么是「重新渲染」而非克隆 DOM：
 *   - 卡片多依赖共享组合式（useRealtime / useDevices），重渲染即实时数据，互不影响；
 *   - ECharts 由 useEcharts 的 ResizeObserver 在容器变大时自动 resize，图表放大不糊；
 *   - 关闭遮罩即卸载放大副本，ECharts 实例随组件销毁，无内存泄漏。
 */
import { ref } from 'vue'

// 模块级单例：所有调用方共享同一份放大状态
const zoomState = ref(null) // null | { title: string, component: Component, props?: object }

export function useCardZoom() {
  function openCardZoom(payload) {
    if (!payload || !payload.component) return
    zoomState.value = {
      title: payload.title || '数据卡片',
      component: payload.component,
      props: payload.props || {}
    }
  }
  function closeCardZoom() {
    zoomState.value = null
  }
  return { zoomState, openCardZoom, closeCardZoom }
}
