<template>
  <div class="chart-card prediction-chart">
    <div class="chart-toolbar">
      <span class="chart-title">📈 时序预测</span>
      <div class="chart-filters">
        <button class="tool-btn mini" @click="run">🔮 触发预测</button>
        <button class="tool-btn mini" @click="health">💓 健康检查</button>
        <button class="tool-btn mini" @click="exportImg" title="导出图片">📥</button>
      </div>
    </div>
    <div ref="chartRef" class="chart-box"></div>
    <div class="chart-info" v-if="prediction">
      <span>预测总人数: <b :class="prediction.degraded ? 'warn' : 'good'">{{ prediction.predicted_total }}</b></span>
      <span>区间: {{ prediction.interval_minutes }}min</span>
      <span v-if="prediction.degraded" class="warn">⚠ 模型已降级</span>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onBeforeUnmount, watch } from 'vue'
import { useEcharts, makeTooltip, makeGrid, makeAxis, CHART_THEME } from '../composables/useEcharts.js'
import { useRealtime } from '../composables/useRealtime.js'
import { useToast } from '../composables/useToast.js'
import { predict, getLatestPrediction, getPredictionHealth } from '../api/endpoints.js'

const rt = useRealtime()
const { push } = useToast()
const { chartRef, setOption, getInstance, exportImage, showLoading, hideLoading } = useEcharts()

const prediction = ref(rt.state.prediction)
let resizeRerenderTimer = null

// 容器宽度变化时（不同屏幕/窗口缩放）重算 X 轴抽稀密度，保证标签始终清晰
function onResizeRerender() {
  if (resizeRerenderTimer) clearTimeout(resizeRerenderTimer)
  resizeRerenderTimer = setTimeout(() => { if (prediction.value) render() }, 200)
}

/**
 * X 轴自适应标签配置：
 *  - 使用安全宽度下限（360px，即标准卡片宽度），即使图表在 flex 卡片内
 *    初次渲染时容器宽度尚未布局（clientWidth=0），也不会错算出过大的
 *    抽稀步长导致中间标签"全部消失"。
 *  - 用数值 interval（而非返回 NaN 的函数 interval），配合 hideOverlap
 *    兜底，任何情况下都不会让整批标签消失。
 * @param {number} n 标签总数
 */
function makeAdaptiveAxis(n) {
  if (!n) return { fontSize: 10, color: '#9fc6e8', hideOverlap: true, interval: 0 }
  // 实际宽度取不到（未布局/为 0）时回退到标准卡片宽度 360px
  const rawW = chartRef.value ? chartRef.value.clientWidth : 0
  const w = rawW > 0 ? rawW : 360
  const usable = Math.max(w - 40, 80)      // 扣减左右边距的可用宽度
  const step = Math.max(1, Math.ceil((n * 42) / usable))  // 每个标签至少 42px
  return {
    fontSize: 10,
    color: '#9fc6e8',        // 提亮 X 轴标签色，提升深色卡片上的对比度与可读性
    interval: step - 1,      // 数值间隔：仅抽稀、绝不隐藏整批标签
    hideOverlap: true,       // 兜底：仅隐藏极小概率仍重叠的个别标签
    margin: 10               // X 轴标签距轴线间距
  }
}

function render() {
  const p = prediction.value
  if (!p) return

  const history = p.history || {}
  const total = history.total || []
  const person = history.person || []
  const vehicle = history.vehicle || []

  // X 轴标签：历史区间 → 预测区间
  const labels = total.map((_, i) => `T-${total.length - i - 1}`)
  labels.push(`+${p.interval_minutes}min`)

  // 历史数据 + 预测点（最后一个点是预测值）
  const totalData = [...total, p.predicted_total]
  const personData = [...person, null]  // 预测点只画 total
  const vehicleData = [...vehicle, null]

  setOption({
    color: [CHART_THEME.colorPalette[0], CHART_THEME.colorPalette[1], CHART_THEME.colorPalette[2]],
    tooltip: makeTooltip({
      trigger: 'axis',
      formatter: (params) => {
        let html = `<div style="font-weight:600;margin-bottom:4px">${params[0].axisValueLabel}</div>`
        params.forEach(p => {
          if (p.value == null) return
          const unit = p.seriesName.includes('车') ? '辆' : '人'
          html += `<div>${p.marker} ${p.seriesName}: <b style="color:${p.color}">${p.value}</b> ${unit}</div>`
        })
        // 如果是预测点
        if (params[0].dataIndex === totalData.length - 1) {
          html += `<div style="color:#ffd23f;margin-top:4px">⚡ 预测值（Chronos-2）</div>`
        }
        return html
      }
    }),
    legend: {
      data: ['总人数', '人流', '车流'],
      textStyle: { color: CHART_THEME.textColor, fontSize: 10 },
      top: 0, itemWidth: 10, itemHeight: 6
    },
    grid: makeGrid({ top: 28, bottom: 0 }),
    xAxis: makeAxis('category', {
      data: labels,
      // 自适应刻度密度：按容器实际宽度决定抽稀间隔，避免窄屏重叠
      axisLabel: makeAdaptiveAxis(labels.length)
    }),
    yAxis: makeAxis('value', { name: '人数', nameTextStyle: { fontSize: 9, color: CHART_THEME.axisLabel } }),
    series: [
      {
        name: '总人数',
        type: 'line',
        data: totalData,
        smooth: true,
        showSymbol: true,
        symbolSize: (val, params) => params.dataIndex === totalData.length - 1 ? 12 : 4,
        itemStyle: (params) => ({
          color: params.dataIndex === totalData.length - 1 ? CHART_THEME.upColor : CHART_THEME.colorPalette[0],
          borderColor: params.dataIndex === totalData.length - 1 ? '#fff' : 'transparent',
          borderWidth: params.dataIndex === totalData.length - 1 ? 2 : 0
        }),
        areaStyle: { opacity: 0.12 },
        markLine: p.degraded ? {
          symbol: 'none',
          lineStyle: { color: '#ffd23f', type: 'dashed', width: 1 },
          data: [{ xAxis: totalData.length - 1.5 }],
          label: { formatter: '降级区', color: '#ffd23f', fontSize: 9 }
        } : undefined
      },
      { name: '人流', type: 'line', data: personData, smooth: true, showSymbol: false, areaStyle: { opacity: 0.06 } },
      { name: '车流', type: 'line', data: vehicleData, smooth: true, showSymbol: false, areaStyle: { opacity: 0.04 } }
    ],
    animation: true,
    animationDuration: 600
  })
}

async function run() {
  try {
    showLoading()
    const r = await predict()
    prediction.value = r
    rt.state.prediction = r
    render()
    push('预测完成：' + r.predicted_total + ' 人' + (r.degraded ? '（已降级）' : ''), 'success')
  } catch (e) {
    push('预测失败：' + (e.detail || e.message), 'error')
  } finally {
    hideLoading()
  }
}

async function health() {
  try {
    const r = await getPredictionHealth()
    push('预测服务：' + r.status + (r.degraded ? '（已降级）' : ''), 'info')
  } catch (e) {
    push('健康检查失败：' + (e.detail || e.message), 'error')
  }
}

function exportImg() {
  const url = exportImage()
  if (!url) return
  const a = document.createElement('a')
  a.href = url
  a.download = `prediction-${Date.now()}.png`
  a.click()
}

watch(() => rt.state.prediction, (p) => {
  prediction.value = p
  if (p) render()
})

onMounted(async () => {
  window.addEventListener('resize', onResizeRerender)
  try {
    const r = await getLatestPrediction()
    prediction.value = r
    rt.state.prediction = r
    render()
  } catch { /* 无缓存预测 */ }
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', onResizeRerender)
  if (resizeRerenderTimer) clearTimeout(resizeRerenderTimer)
})
</script>
