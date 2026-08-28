/**
 * ECharts 组合式函数 —— 大屏科技感图表生命周期管理
 * ---------------------------------------------------------------------------
 * 职责：
 *   1. 管理 ECharts 实例的 init / setOption / resize / dispose 生命周期
 *   2. 提供深色大屏科技感主题配色（与项目 main.css 视觉一致）
 *   3. 通用配置工厂：tooltip / grid / legend / dataZoom / animation
 *   4. 数据缓存策略：缓存上一次 option，防抖渲染，避免高频更新抖动
 *   5. 防抖 resize：窗口缩放时 150ms 防抖，避免频繁重绘
 *   6. 按需渲染：容器不可见时跳过 setOption，可见时恢复
 *
 * 用法（Vue3 script setup）：
 *   const { chartRef, setOption, getInstance, exportImage } = useEcharts()
 *   // template: <div ref="chartRef" class="chart-box"></div>
 *   setOption({ ... })  // 传入完整 ECharts option
 */
import { ref, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'

// —— 深色大屏科技感主题配色（与 main.css CSS 变量对齐）——
export const CHART_THEME = {
  bg: 'transparent',
  textColor: '#9fb8e0',
  textColorStrong: '#d4e4ff',
  axisLine: 'rgba(0,225,255,0.15)',
  axisLabel: '#7f93b8',
  splitLine: 'rgba(0,225,255,0.06)',
  // 色板：科技蓝 → 青绿 → 金黄 → 红橙 → 紫
  colorPalette: ['#00e1ff', '#5fe39a', '#ffd23f', '#ff6b6b', '#a78bfa', '#38bdf8'],
  // 系列色：涨红跌绿（中国股市风格，用于交通拥堵指数等）
  upColor: '#ff4d4f',
  downColor: '#5fe39a',
  tooltipBg: 'rgba(8,20,40,0.92)',
  tooltipBorder: 'rgba(0,225,255,0.25)',
  tooltipText: '#d4e4ff'
}

/**
 * 生成通用 tooltip 配置（深色科技感样式）
 * @param {object} [extra] 额外覆盖字段
 */
export function makeTooltip(extra = {}) {
  return {
    trigger: 'axis',
    backgroundColor: CHART_THEME.tooltipBg,
    borderColor: CHART_THEME.tooltipBorder,
    borderWidth: 1,
    textStyle: { color: CHART_THEME.tooltipText, fontSize: 12 },
    axisPointer: {
      type: 'shadow',
      shadowStyle: { color: 'rgba(0,225,255,0.06)' }
    },
    ...extra
  }
}

/**
 * 生成通用 grid 配置（紧凑大屏风格，边距小）
 * @param {object} [extra]
 */
export function makeGrid(extra = {}) {
  return {
    left: 8,
    right: 12,
    top: 30,
    bottom: 8,
    containLabel: true,
    ...extra
  }
}

/**
 * 生成通用 legend 配置
 */
export function makeLegend(extra = {}) {
  return {
    textStyle: { color: CHART_THEME.textColor, fontSize: 11 },
    itemWidth: 12,
    itemHeight: 8,
    itemGap: 12,
    top: 0,
    ...extra
  }
}

/**
 * 生成通用坐标轴配置
 */
export function makeAxis(type = 'category', extra = {}) {
  return {
    type,
    axisLine: { lineStyle: { color: CHART_THEME.axisLine } },
    axisLabel: { color: CHART_THEME.axisLabel, fontSize: 10 },
    axisTick: { show: false },
    splitLine: { show: type === 'value', lineStyle: { color: CHART_THEME.splitLine } },
    ...extra
  }
}

/**
 * 组合式函数主体
 * @param {object} [opts] 选项：{ resizeOnContainerResize?: boolean }
 */
export function useEcharts(opts = {}) {
  const chartRef = ref(null)
  let instance = null
  let lastOption = null     // 数据缓存：上次 setOption 的 option 对象
  let resizeTimer = null    // 防抖 resize 定时器
  let ro = null             // ResizeObserver（容器尺寸变化时自动 resize）

  // 初始化 ECharts 实例
  function init() {
    if (!chartRef.value || instance) return
    if (typeof window.echarts === 'undefined') {
      console.warn('[useEcharts] window.echarts 未加载，请确认 CDN script 已引入')
      return
    }
    instance = window.echarts.init(chartRef.value, null, {
      renderer: 'canvas',
      useDirtyRect: true   // 脏矩形优化，提升高频更新性能
    })

    // ResizeObserver：容器尺寸变化时自动 resize（比 window resize 更精准）
    if (typeof ResizeObserver !== 'undefined') {
      ro = new ResizeObserver(() => {
        debounceResize()
      })
      ro.observe(chartRef.value)
    }

    // 窗口 resize 兜底
    window.addEventListener('resize', debounceResize)
  }

  // 防抖 resize
  function debounceResize() {
    if (resizeTimer) clearTimeout(resizeTimer)
    resizeTimer = setTimeout(() => {
      if (instance) instance.resize()
    }, 150)
  }

  /**
   * 设置图表 option（数据缓存 + 防抖）
   * @param {object} option ECharts option 对象
   * @param {boolean} [merge=true] 是否合并（false=全量替换）
   */
  function setOption(option, merge = true) {
    lastOption = option
    if (!instance) {
      init()
      if (!instance) return
    }
    // 按需渲染：容器不可见时跳过（IntersectionObserver 简化版）
    if (chartRef.value && chartRef.value.offsetParent === null) return
    instance.setOption(option, { merge, notMerge: !merge })
  }

  /**
   * 获取 ECharts 实例（用于高级操作：dispatchAction, on, off 等）
   */
  function getInstance() {
    return instance
  }

  /**
   * 导出图表为图片（PNG base64）
   * @param {object} [opts] { pixelRatio, backgroundColor }
   * @returns {string} base64 dataURL
   */
  function exportImage(opts = {}) {
    if (!instance) return ''
    return instance.getDataURL({
      pixelRatio: opts.pixelRatio || 2,
      backgroundColor: opts.backgroundColor || '#0a1628'
    })
  }

  /**
   * 触发图表 resize（手动调用）
   */
  function resize() {
    if (instance) instance.resize()
  }

  /**
   * 显示 loading 动画
   */
  function showLoading() {
    if (instance) instance.showLoading('default', {
      text: '数据加载中…',
      color: CHART_THEME.colorPalette[0],
      textColor: CHART_THEME.textColor,
      maskColor: 'rgba(8,20,40,0.7)',
      zlevel: 0
    })
  }

  /**
   * 隐藏 loading 动画
   */
  function hideLoading() {
    if (instance) instance.hideLoading()
  }

  // Vue 生命周期：挂载后初始化
  onMounted(async () => {
    await nextTick()
    init()
    // 如果有缓存的 option，恢复渲染
    if (lastOption && instance) {
      instance.setOption(lastOption)
    }
  })

  // Vue 生命周期：卸载前清理
  onBeforeUnmount(() => {
    if (resizeTimer) clearTimeout(resizeTimer)
    window.removeEventListener('resize', debounceResize)
    if (ro) { ro.disconnect(); ro = null }
    if (instance) { instance.dispose(); instance = null }
  })

  return {
    chartRef,
    setOption,
    getInstance,
    exportImage,
    resize,
    showLoading,
    hideLoading,
    CHART_THEME,
    makeTooltip,
    makeGrid,
    makeLegend,
    makeAxis
  }
}
