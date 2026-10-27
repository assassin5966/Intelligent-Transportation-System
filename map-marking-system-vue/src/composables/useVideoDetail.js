import { reactive } from 'vue'

// 视频详情面板全局状态（module 级单例，跨组件读写）：
// MapPanel 大卡「详情」点击 → open() 写入门名 + 4 路设备数据；
// 左栏 VideoDetailPanel 响应式渲染，close() 收起。
const state = reactive({
  gateId: '',
  gateName: '',
  devices: [] // [{ ch, name, kind: 'veh'|'per', status, level, active, rows: [[k, v], ...] }]
})

export function useVideoDetail() {
  function open(gateId, gateName, devices) {
    state.gateId = gateId
    state.gateName = gateName
    state.devices = devices
  }
  function close() {
    state.gateId = ''
    state.gateName = ''
    state.devices = []
  }
  return { state, open, close }
}
