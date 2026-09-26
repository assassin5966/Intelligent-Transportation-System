/**
 * 地图控制总线（单例）
 * ---------------------------------------------------------------------------
 * 顶栏(城市/配色/视角/搜索定位)与卡片栏(告警定位)与地图(MapPanel)解耦：
 * MapPanel 初始化后将真实方法挂到 mapCtl；调用方调 mapCtl.xxx() 即可，
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
  focusDevice() {}, // 定位到指定设备（跳转 + 展开信息卡）
  focusGate() {},   // 定位到指定门（跳转到该门大卡 + 闪烁一次）
  gates: []         // 门级大卡列表 [{id,gate,edge,direction,status,coord}]，由 MapPanel 发布
})

export function useMapControl() {
  return { mapCtl }
}
