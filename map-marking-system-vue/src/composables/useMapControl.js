/**
 * 地图控制总线（单例）
 * ---------------------------------------------------------------------------
 * 顶栏(城市/配色/视角/添加设备)与地图(MapPanel)解耦：
 * MapPanel 初始化后将真实方法挂到 mapCtl；TopBar 调用 mapCtl.xxx() 即可，
 * 未就绪时为空操作，避免报错。
 */
import { reactive } from 'vue'

export const mapCtl = reactive({
  ready: false,
  changeCity() {},
  changeStyle() {},
  resetView() {},
  setTopView() {},
  setOblique() {},
  fitRange() {},
  toggleFree3D() {},
  startAdd() {},
  // —— 道路路径高亮（初始化 / 切换区域时自动沿真实道路绘制，无需点击切换） ——
  refreshRoadPaths() {} // 重新计算并高亮当前区域的点位间路径
})

export function useMapControl() {
  return { mapCtl }
}
