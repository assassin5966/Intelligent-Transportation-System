<template>
  <div class="chart-card">
    <div class="chart-toolbar">
      <span class="chart-title">📊 实时统计趋势</span>
      <div class="chart-filters">
        <select v-model="filterRange" @change="onFilter" title="时间范围">
          <option value="60">最近 10 分钟</option>
          <option value="180">最近 30 分钟</option>
          <option value="360">最近 1 小时</option>
        </select>
        <button class="tool-btn mini" :class="{ active: chartType === 'line' }" @click="switchType('line')">折线</button>
        <button class="tool-btn mini" :class="{ active: chartType === 'bar' }" @click="switchType('bar')">柱状</button>
        <button class="tool-btn mini" @click="exportImg" title="导出图片">📥</button>
      </div>
    </div>
    <div ref="chartRef" class="chart-box"></div>
    <div class="chart-loading" v-if="loading">数据加载中…</div>
  </div>
</template>

<script setup>
import { ref, onMounted, onBeforeUnmount, watch } from 'vue'
import { useEcharts, makeTooltip, makeGrid, makeLegend, makeAxis, CHART_THEME } from '../composables/useEcharts.js'
import { useRealtime } from '../composables/useRealtime.js'

const rt = useRealtime()
const {
  chartRef, setOption, getInstance, exportImage, showLoading, hideLoading
} = useEcharts()

const filterRange = ref(60)
const chartType = ref('line')
const loading = ref(false)

// 数据缓存：环形缓冲区（时间序列）
const history = {
  labels: [],
  vehicles: [],
  persons: [],
  vIn: [],
  vOut: [],
  maxLen: 360
}

function pushPoint() {
  const s = rt.state.stats
  if (!s) return
  const t = new Date()
  const label = `${pad(t.getHours())}:${pad(t.getMinutes())}:${pad(t.getSeconds())}`
  history.labels.push(label)
  history.vehicles.push(s.current_vehicles || 0)
  history.persons.push(s.current_persons || 0)
  history.vIn.push(s.today_vehicle_in || 0)
  history.vOut.push(s.today_vehicle_out || 0)
  if (history.labels.length > history.maxLen) {
    history.labels.shift()
    history.vehicles.shift()
    history.persons.shift()
    history.vIn.shift()
    history.vOut.shift()
  }
}

function pad(n) { return n < 10 ? '0' + n : '' + n }

function render() {
  const start = Math.max(0, history.labels.length - filterRange.value)
  const labels = history.labels.slice(start)
  const vehicles = history.vehicles.slice(start)
  const persons = history.persons.slice(start)

  const baseSeries = {
    type: chartType.value,
    smooth: chartType.value === 'line',
    showSymbol: false,
    emphasis: { focus: 'series' }
  }

  setOption({
    color: [CHART_THEME.colorPalette[0], CHART_THEME.colorPalette[1]],
    tooltip: makeTooltip({
      formatter: (params) => {
        let html = `<div style="font-weight:600;margin-bottom:4px">${params[0].axisValueLabel || params[0].name}</div>`
        params.forEach(p => {
          const unit = p.seriesName.includes('车辆') ? '辆' : '人'
          html += `<div>${p.marker} ${p.seriesName}: <b style="color:${p.color}">${p.value}</b> ${unit}</div>`
        })
        return html
      }
    }),
    legend: makeLegend({ data: ['在场车辆', '在场人员'] }),
    grid: makeGrid({ top: 28, bottom: 4 }),
    xAxis: makeAxis('category', {
      data: labels,
      axisLabel: {
        color: CHART_THEME.axisLabel, fontSize: 9,
        formatter: (v) => v.slice(0, 5), // 只显示 分:秒
        interval: Math.max(0, Math.floor(labels.length / 6))
      }
    }),
    yAxis: [
      makeAxis('value', { name: '车辆', nameTextStyle: { color: CHART_THEME.axisLabel, fontSize: 9 }, min: 0 }),
      makeAxis('value', { name: '人员', nameTextStyle: { color: CHART_THEME.axisLabel, fontSize: 9 }, min: 0, splitLine: { show: false } })
    ],
    dataZoom: labels.length > 30 ? [{ type: 'inside', start: 0, end: 100 }] : undefined,
    series: [
      { ...baseSeries, name: '在场车辆', data: vehicles, yAxisIndex: 0, areaStyle: chartType.value === 'line' ? { opacity: 0.12 } : undefined },
      { ...baseSeries, name: '在场人员', data: persons, yAxisIndex: 1, areaStyle: chartType.value === 'line' ? { opacity: 0.08 } : undefined }
    ],
    animation: true,
    animationDuration: 300,
    animationDurationUpdate: 300
  })
}

// 定时采集数据点
let pollTimer = null
function startPoll() {
  pollTimer = setInterval(() => {
    pushPoint()
    render()
  }, 5000) // 5 秒一个数据点（对齐 stats 轮询频率）
}

function onFilter() { render() }

function switchType(t) { chartType.value = t; render() }

function exportImg() {
  const url = exportImage()
  if (!url) return
  const a = document.createElement('a')
  a.href = url
  a.download = `stats-trend-${Date.now()}.png`
  a.click()
}

watch(() => rt.state.stats, () => {
  // WS 推送时也立即更新（不等定时器）
  pushPoint()
  render()
}, { deep: true })

onMounted(() => {
  // 用当前 stats 填充初始数据
  loading.value = true
  showLoading()
  pushPoint()
  // 模拟 6 个历史数据点
  for (let i = 1; i <= 5; i++) {
    const s = rt.state.stats
    if (s) {
      history.labels.push(`-${(5 - i) * 5}s`)
      history.vehicles.push(Math.max(0, (s.current_vehicles || 0) - i * 2))
      history.persons.push(Math.max(0, (s.current_persons || 0) - i * 5))
    }
  }
  pushPoint()
  render()
  hideLoading()
  loading.value = false
  startPoll()
})

onBeforeUnmount(() => {
  if (pollTimer) clearInterval(pollTimer)
})
</script>
