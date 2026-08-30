/**
 * 全局主题（浅色 / 深色）单例
 * ---------------------------------------------------------------------------
 * - theme: 'dark' | 'light'，默认 'dark'（沿用原大屏深色科技风）
 * - 通过 document.documentElement[data-theme] 驱动 <style> 中的 CSS 变量切换
 * - 同步透视出：
 *     · mapStyle      —— 高德底图样式（深色底图 / 浅色底图）
 *     · overlayColors —— 叠加层在 JS 侧需要的主题色
 *       （CSS 变量无法作用于 AMap canvas/svg 内联属性，故由 JS 读取后 setOptions）
 * - 持久化到 localStorage，刷新后保持用户选择
 *
 * 用法（任意组件）：
 *   const { theme, mapStyle, overlayColors, toggleTheme, setTheme } = useTheme()
 */
import { ref, computed } from 'vue'

const STORAGE_KEY = 'mms-theme'

// 默认深色，保持与原大屏一致的观感；localStorage 可覆盖
const theme = ref((typeof localStorage !== 'undefined' && localStorage.getItem(STORAGE_KEY)) || 'dark')

function applyAttr() {
  if (typeof document !== 'undefined') {
    document.documentElement.setAttribute('data-theme', theme.value)
  }
}
applyAttr()

/**
 * 地图底图样式（内网 Leaflet 离线瓦片）：
 *  - 由 MapPanel 根据主题读取 VITE_TILE_URL 环境变量加载对应瓦片层
 *  - 此处仅返回主题标识，供 MapPanel 切换瓦片源
 */
const mapStyle = computed(() =>
  theme.value === 'dark' ? 'dark' : 'light'
)

/**
 * 叠加层 JS 侧主题色（范围框 stroke/fill 等无法用 CSS 变量控制的 AMap 内联属性）。
 * 深色用青蓝辉光；选中区域用实线蓝边 + 半透明蓝填充，匹配高德区域搜索效果。
 */
const overlayColors = computed(() =>
  theme.value === 'dark'
    ? {
      rangeStroke: '#00e1ff', rangeFill: '#00b4ff', rangeStrokeOp: 0.95, rangeFillOp: 0.05,
      // 区域主边界描边（清晰可见）
      regionStroke: '#2f9bff', regionStrokeOp: 0.95,
      // 区域内淡填充（凸显区域，不遮挡底图）
      polyFill: '#2f9bff', polyFillOp: 0.06,
      // 圈外遮罩层（圈外变暗、圈内清晰，高德原生圈地质感）
      maskFill: '#000000', maskOp: 0.40,
      // 圈地动画线（边界脉冲）
      pulseStroke: '#00e1ff', pulseOp: 0.55
    }
    : {
      rangeStroke: '#0077c8', rangeFill: '#2f9bff', rangeStrokeOp: 0.9, rangeFillOp: 0.08,
      regionStroke: '#1f6fff', regionStrokeOp: 0.92,
      polyFill: '#1f6fff', polyFillOp: 0.04,
      maskFill: '#1a3a5c', maskOp: 0.16,
      pulseStroke: '#0077c8', pulseOp: 0.45
    }
)

function setTheme(t) {
  theme.value = t === 'light' ? 'light' : 'dark'
  applyAttr()
  try {
    localStorage.setItem(STORAGE_KEY, theme.value)
  } catch (e) {
    /* 隐私模式等场景忽略 */
  }
}

function toggleTheme() {
  setTheme(theme.value === 'dark' ? 'light' : 'dark')
}

export function useTheme() {
  return { theme, mapStyle, overlayColors, setTheme, toggleTheme }
}
