<template>
  <div class="chart-card">
    <div class="chart-toolbar">
      <span class="chart-title">🚗 各设备计数</span>
      <div class="chart-filters">
        <select v-model="filterCat" @change="onFilter" title="类别筛选">
          <option value="all">全部</option>
          <option value="vehicle">机动车</option>
          <option value="person">人流</option>
        </select>
        <button class="tool-btn mini" :class="{ active: chartType === 'bar' }" @click="switchType('bar')">柱状</button>
        <button class="tool-btn mini" :class="{ active: chartType === 'line' }" @click="switchType('line')">折线</button>
        <button class="tool-btn mini" @click="exportImg" title="导出图片">📥</button>
      </div>
    </div>
    <!-- 面包屑（钻取导航） -->
    <div class="chart-breadcrumb" v-if="drillDevice">
      <span class="crumb" @click="goBack">← 返回总览</span>
      <span class="crumb-sep">/</span>
      <span class="crumb active">{{ drillDevice }}</span>
    </div>
    <div ref="chartRef" class="chart-box"></div>
  </div>
</template>

<script setup>
import { ref, onMounted, watch } from 'vue'
import { useEcharts, makeTooltip, makeGrid, makeLegend, makeAxis, CHART_THEME } from '../composables/useEcharts.js'
import { useDevices } from '../composables/useDevices.js'
import { getDeviceStat } from '../api/endpoints.js'

const { state: dev } = useDevices()
const {
  chartRef, setOption, getInstance, exportImage
} = useEcharts()

const filterCat = ref('all')
const chartType = ref('bar')
const drillDevice = ref(null) // 钻取至单设备详情

function getFilteredDevices() {
  let list = dev.devices
  if (filterCat.value !== 'all') {
    list = list.filter(d => d.camera_type === filterCat.value)
  }
  return list
}

function renderOverview() {
  const devices = getFilteredDevices()
  const names = devices.map(d => d.name || d.id)
  const stats = devices.map(d => {
    const s = dev.statsById[d.id]
    return s ? (d.camera_type === 'person' ? s.current_persons : s.current_vehicles) : 0
  })
  const congestionFlags = devices.map(d => {
    const s = dev.statsById[d.id]
    return s ? s.congested : false
  })

  setOption({
    color: CHART_THEME.colorPalette,
    tooltip: makeTooltip({
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (params) => {
        const p = params[0]
        const d = devices[p.dataIndex]
        const s = dev.statsById[d?.id]
        let html = `<div style="font-weight:600;margin-bottom:4px">${p.axisValueLabel}</div>`
        if (s) {
          html += `<div>当前车辆: <b style="color:#00e1ff">${s.current_vehicles || 0}</b> 辆</div>`
          html += `<div>当前人员: <b style="color:#5fe39a">${s.current_persons || 0}</b> 人</div>`
          html += `<div>今日车进/出: ${s.today_vehicle_in || 0} / ${s.today_vehicle_out || 0}</div>`
          html += `<div>今日人进/出: ${s.today_person_in || 0} / ${s.today_person_out || 0}</div>`
          if (s.roi_vehicles != null) html += `<div>ROI 车辆: ${s.roi_vehicles}</div>`
          if (s.vehicle_flow_per_min != null) html += `<div>车流速度: ${s.vehicle_flow_per_min?.toFixed(1)} 辆/min</div>`
          if (s.congested) html += `<div style="color:#ff6b6b">⚠ 状态：拥挤</div>`
        }
        html += `<div style="color:#7f93b8;margin-top:4px;font-size:10px">点击查看详情</div>`
        return html
      }
    }),
    legend: makeLegend({ show: false }),
    grid: makeGrid({ top: 20, bottom: 4 }),
    xAxis: makeAxis('category', {
      data: names,
      axisLabel: {
        color: CHART_THEME.axisLabel, fontSize: 9,
        formatter: (v) => v.length > 4 ? v.slice(0, 4) + '…' : v,
        interval: 0, rotate: names.length > 4 ? 15 : 0
      }
    }),
    yAxis: makeAxis('value'),
    series: [{
      type: chartType.value,
      data: stats.map((v, i) => ({
        value: v,
        itemStyle: congestionFlags[i]
          ? { color: CHART_THEME.upColor }
          : { color: CHART_THEME.colorPalette[0] }
      })),
      barMaxWidth: 32,
      smooth: chartType.value === 'line',
      label: { show: true, position: 'top', color: CHART_THEME.textColor, fontSize: 10 }
    }],
    animation: true,
    animationDuration: 400
  })

  // 点击钻取
  const inst = getInstance()
  if (inst) {
    inst.off('click')
    inst.on('click', (params) => {
      const d = devices[params.dataIndex]
      if (d) drillDown(d.id)
    })
  }
}

function renderDeviceDetail() {
  const s = dev.statsById[drillDevice.value]
  const d = dev.devices.find(x => x.id === drillDevice.value)
  if (!s || !d) { goBack(); return }

  const labels = ['当前车辆', '当前人员', '今日车进', '今日车出', '今日人进', '今日人出']
  const values = [s.current_vehicles, s.current_persons, s.today_vehicle_in, s.today_vehicle_out, s.today_person_in, s.today_person_out]
  const colors = [CHART_THEME.colorPalette[0], CHART_THEME.colorPalette[1], CHART_THEME.colorPalette[2], CHART_THEME.colorPalette[3], CHART_THEME.colorPalette[4], CHART_THEME.colorPalette[5]]

  setOption({
    color: colors,
    tooltip: makeTooltip({
      trigger: 'item',
      formatter: (p) => {
        const unit = p.name.includes('车辆') || p.name.includes('车') ? '辆' : '人'
        return `${p.marker} ${p.name}: <b>${p.value}</b> ${unit}`
      }
    }),
    grid: makeGrid({ top: 20, bottom: 4, left: 10, right: 10 }),
    xAxis: makeAxis('category', { data: labels, axisLabel: { fontSize: 9, interval: 0, rotate: 20 } }),
    yAxis: makeAxis('value'),
    series: [{
      type: chartType.value,
      data: values.map((v, i) => ({ value: v, itemStyle: { color: colors[i] } })),
      barMaxWidth: 36,
      label: { show: true, position: 'top', color: CHART_THEME.textColor, fontSize: 10 }
    }],
    animation: true,
    animationDuration: 400
  })
}

async function drillDown(deviceId) {
  drillDevice.value = deviceId
  // 拉取单设备最新计数（§4.3）
  try {
    const stat = await getDeviceStat(deviceId)
    if (stat) dev.statsById[deviceId] = stat
  } catch {}
  renderDeviceDetail()
}

function goBack() {
  drillDevice.value = null
  renderOverview()
}

function onFilter() {
  if (drillDevice.value) goBack()
  else renderOverview()
}

function switchType(t) {
  chartType.value = t
  if (drillDevice.value) renderDeviceDetail()
  else renderOverview()
}

function exportImg() {
  const url = exportImage()
  if (!url) return
  const a = document.createElement('a')
  a.href = url
  a.download = `device-count-${Date.now()}.png`
  a.click()
}

// 设备列表或统计变化时重绘
watch(() => [dev.devices, dev.statsById], () => {
  if (drillDevice.value) renderDeviceDetail()
  else renderOverview()
}, { deep: true })

onMounted(() => {
  renderOverview()
})
</script>
