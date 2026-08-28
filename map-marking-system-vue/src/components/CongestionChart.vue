<template>
  <div class="chart-card">
    <div class="chart-toolbar">
      <span class="chart-title">🚦 拥堵状态</span>
      <div class="chart-filters">
        <button class="tool-btn mini" :class="{ active: view === 'gauge' }" @click="switchView('gauge')">仪表</button>
        <button class="tool-btn mini" :class="{ active: view === 'bar' }" @click="switchView('bar')">柱图</button>
        <button class="tool-btn mini" @click="exportImg" title="导出图片">📥</button>
      </div>
    </div>
    <div ref="chartRef" class="chart-box"></div>
    <div class="congestion-list" v-if="view === 'gauge' && items.length > 0">
      <div class="cong-item" v-for="it in items" :key="it.device_id" :class="{ congested: it.congested }">
        <span class="cong-name">{{ it.name || it.device_id }}</span>
        <span class="cong-stat">
          <b :class="it.congested ? 'bad' : 'good'">{{ it.congested ? '拥堵' : '正常' }}</b>
          <span class="cong-meta">{{ it.roi_vehicles }}/{{ it.max_vehicles }} · {{ it.flow.toFixed(1) }}/min</span>
        </span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, computed, watch } from 'vue'
import { useEcharts, makeTooltip, makeGrid, makeAxis, CHART_THEME } from '../composables/useEcharts.js'
import { useDevices } from '../composables/useDevices.js'

const { state: dev } = useDevices()
const { chartRef, setOption, getInstance, exportImage } = useEcharts()

const view = ref('gauge')

// 设备统计 + 拥挤数据（v0.9.0：均来自 §4.2 设备统计，原 GET /api/stats/congestion 已移除）
const items = computed(() => {
  return dev.devices
    .filter(d => d.camera_type !== 'person' && d.max_vehicles > 0)
    .map(d => {
      const s = dev.statsById[d.id] || {}
      return {
        device_id: d.id,
        name: d.name,
        max_vehicles: d.max_vehicles || 0,
        roi_vehicles: s.roi_vehicles || 0,
        flow: s.vehicle_flow_per_min || 0,
        congested: s.congested || false
      }
    })
})

function renderGauge() {
  // 取第一个拥堵设备作为仪表盘目标（或第一个设备）
  const list = items.value
  if (!list.length) {
    setOption({
      title: { text: '暂无拥堵监控设备', left: 'center', top: 'center', textStyle: { color: CHART_THEME.axisLabel, fontSize: 12 } }
    })
    return
  }

  // 多设备场景：用仪表盘组（每个设备一个小仪表）
  const gauges = list.slice(0, 4).map((it, i) => {
    const ratio = it.max_vehicles > 0 ? Math.min(1, it.roi_vehicles / it.max_vehicles) : 0
    const colCount = Math.min(2, list.length)
    const rowCount = Math.ceil(Math.min(4, list.length) / colCount)
    const col = i % colCount
    const row = Math.floor(i / colCount)
    const cx = (col + 0.5) / colCount * 100
    const cy = (row + 0.5) / rowCount * 100

    return {
      type: 'gauge',
      center: [cx + '%', cy + '%'],
      radius: Math.min(28, 100 / Math.max(colCount, rowCount)) + '%',
      min: 0,
      max: it.max_vehicles > 0 ? Math.max(it.max_vehicles * 1.5, 10) : 20,
      startAngle: 200,
      endAngle: -20,
      title: {
        text: it.name || it.device_id,
        textStyle: { color: CHART_THEME.textColor, fontSize: 10 },
        offsetCenter: [0, '70%']
      },
      detail: {
        valueAnimation: true,
        formatter: (v) => v.toFixed(0),
        color: it.congested ? CHART_THEME.upColor : CHART_THEME.colorPalette[0],
        fontSize: 14,
        offsetCenter: [0, '35%']
      },
      data: [{ value: it.roi_vehicles, name: it.congested ? '拥堵' : '正常' }],
      axisLine: {
        lineStyle: {
          width: 6,
          color: [
            [ratio, it.congested ? CHART_THEME.upColor : CHART_THEME.colorPalette[0]],
            [1, 'rgba(0,225,255,0.08)']
          ]
        }
      },
      pointer: {
        itemStyle: { color: it.congested ? CHART_THEME.upColor : CHART_THEME.colorPalette[0] },
        length: '60%', width: 3
      },
      axisTick: { show: false },
      splitLine: { show: false },
      axisLabel: { show: false }
    }
  })

  setOption({
    tooltip: makeTooltip({ trigger: 'item', formatter: '{b}: {c}' }),
    series: gauges,
    animation: true,
    animationDuration: 600
  })
}

function renderBar() {
  const list = items.value
  if (!list.length) {
    setOption({
      title: { text: '暂无拥堵监控设备', left: 'center', top: 'center', textStyle: { color: CHART_THEME.axisLabel, fontSize: 12 } }
    })
    return
  }

  const names = list.map(it => it.name || it.device_id)
  const roi = list.map(it => it.roi_vehicles)
  const maxs = list.map(it => it.max_vehicles)
  const flows = list.map(it => it.flow)
  const congestedFlags = list.map(it => it.congested)

  setOption({
    color: [CHART_THEME.colorPalette[0], CHART_THEME.colorPalette[2], CHART_THEME.colorPalette[1]],
    tooltip: makeTooltip({
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (params) => {
        const it = list[params[0].dataIndex]
        let html = `<div style="font-weight:600;margin-bottom:4px">${it.name || it.device_id}</div>`
        html += `<div>ROI 车辆: <b style="color:#00e1ff">${it.roi_vehicles}</b> / 阈值 ${it.max_vehicles}</div>`
        html += `<div>车流速度: <b style="color:#5fe39a">${it.flow.toFixed(1)}</b> 辆/min</div>`
        html += it.congested ? `<div style="color:#ff6b6b">⚠ 状态：拥挤</div>` : `<div style="color:#5fe39a">状态：正常</div>`
        return html
      }
    }),
    legend: {
      data: ['ROI 车辆', '阈值', '车流速度'],
      textStyle: { color: CHART_THEME.textColor, fontSize: 10 },
      top: 0, itemWidth: 10, itemHeight: 6
    },
    grid: makeGrid({ top: 28, bottom: 4 }),
    xAxis: makeAxis('category', { data: names, axisLabel: { fontSize: 9, interval: 0, formatter: v => v.length > 4 ? v.slice(0, 4) + '…' : v } }),
    yAxis: [
      makeAxis('value', { name: '车辆', nameTextStyle: { fontSize: 9, color: CHART_THEME.axisLabel } }),
      makeAxis('value', { name: '辆/min', nameTextStyle: { fontSize: 9, color: CHART_THEME.axisLabel }, splitLine: { show: false } })
    ],
    series: [
      {
        name: 'ROI 车辆',
        type: 'bar',
        data: roi.map((v, i) => ({ value: v, itemStyle: { color: congestedFlags[i] ? CHART_THEME.upColor : CHART_THEME.colorPalette[0] } })),
        barMaxWidth: 24
      },
      {
        name: '阈值',
        type: 'line',
        data: maxs,
        lineStyle: { color: CHART_THEME.colorPalette[2], type: 'dashed', width: 2 },
        showSymbol: true, symbolSize: 6
      },
      {
        name: '车流速度',
        type: 'line',
        yAxisIndex: 1,
        data: flows,
        lineStyle: { color: CHART_THEME.colorPalette[1], width: 2 },
        showSymbol: false, smooth: true,
        areaStyle: { opacity: 0.06 }
      }
    ],
    animation: true,
    animationDuration: 400
  })
}

function switchView(v) {
  view.value = v
  if (v === 'gauge') renderGauge()
  else renderBar()
}

function exportImg() {
  const url = exportImage()
  if (!url) return
  const a = document.createElement('a')
  a.href = url
  a.download = `congestion-${Date.now()}.png`
  a.click()
}

watch(() => [dev.devices, dev.statsById], () => {
  if (view.value === 'gauge') renderGauge()
  else renderBar()
}, { deep: true })

onMounted(() => {
  renderGauge()
})
</script>
