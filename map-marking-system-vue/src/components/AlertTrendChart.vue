<template>
  <div class="chart-card chart-inline">
    <div class="chart-toolbar">
      <span class="chart-title">🚨 告警分析</span>
      <div class="chart-filters">
        <select v-model="groupBy" @change="renderChart" title="分组方式">
          <option value="level">按级别</option>
          <option value="category">按类别</option>
        </select>
        <button class="tool-btn mini" :class="{ active: chartType === 'pie' }" @click="switchType('pie')">饼图</button>
        <button class="tool-btn mini" :class="{ active: chartType === 'bar' }" @click="switchType('bar')">柱图</button>
        <button class="tool-btn mini" @click="exportImg" title="导出图片">📥</button>
      </div>
    </div>
    <div class="chart-breadcrumb" v-if="drillCategory">
      <span class="crumb" @click="goBack">← 返回总览</span>
      <span class="crumb-sep">/</span>
      <span class="crumb active">{{ drillLabel }}</span>
    </div>
    <div ref="chartRef" class="chart-box"></div>
    <div class="chart-info" v-if="totalAlerts > 0">
      共 {{ totalAlerts }} 条告警
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, computed, watch } from 'vue'
import { useEcharts, makeTooltip, makeGrid, makeAxis, CHART_THEME } from '../composables/useEcharts.js'
import { useRealtime } from '../composables/useRealtime.js'
import { listAlerts } from '../api/endpoints.js'

const rt = useRealtime()
const { chartRef, setOption, getInstance, exportImage } = useEcharts()

const groupBy = ref('level')
const chartType = ref('pie')
const drillCategory = ref(null)

const totalAlerts = computed(() => rt.state.alerts.length)

// 类别中文映射
const CATEGORY_LABELS = {
  vehicle_saturate: '车辆饱和',
  person_saturate: '人员饱和',
  congestion: '拥堵',
  video_anomaly: '视频异常'
}
const LEVEL_LABELS = {
  critical: '严重',
  warning: '警告',
  info: '信息'
}
const LEVEL_COLORS = {
  critical: CHART_THEME.upColor,
  warning: CHART_THEME.colorPalette[2],
  info: CHART_THEME.colorPalette[1]
}

function renderChart() {
  if (drillCategory.value) {
    renderDrill()
    return
  }

  const alerts = rt.state.alerts
  if (!alerts.length) {
    setOption({
      title: { text: '暂无告警', left: 'center', top: 'center', textStyle: { color: CHART_THEME.axisLabel, fontSize: 14 } }
    })
    return
  }

  // 按 groupBy 分组
  const groups = {}
  alerts.forEach(a => {
    const key = groupBy.value === 'level' ? a.level : a.category
    if (!groups[key]) groups[key] = 0
    groups[key]++
  })

  const labels = Object.keys(groups).map(k => groupBy.value === 'level' ? LEVEL_LABELS[k] || k : CATEGORY_LABELS[k] || k)
  const values = Object.values(groups)
  const colors = Object.keys(groups).map(k => groupBy.value === 'level' ? LEVEL_COLORS[k] || CHART_THEME.colorPalette[0] : CHART_THEME.colorPalette[Object.keys(groups).indexOf(k) % CHART_THEME.colorPalette.length])

  if (chartType.value === 'pie') {
    setOption({
      color: colors,
      tooltip: makeTooltip({
        trigger: 'item',
        formatter: '{b}: {c} 条 ({d}%)'
      }),
      legend: { orient: 'vertical', right: 4, top: 'center', textStyle: { color: CHART_THEME.textColor, fontSize: 10 }, itemWidth: 8, itemHeight: 6 },
      series: [{
        type: 'pie',
        radius: ['38%', '62%'],
        center: ['35%', '50%'],
        data: labels.map((l, i) => ({ name: l, value: values[i], itemStyle: { color: colors[i] } })),
        label: { color: CHART_THEME.textColor, fontSize: 10 },
        emphasis: { label: { fontSize: 12, fontWeight: 'bold' } }
      }],
      animation: true,
      animationDuration: 500
    })
  } else {
    setOption({
      color: colors,
      tooltip: makeTooltip({
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: (params) => {
          const p = params[0]
          return `${p.marker} ${p.name}: <b>${p.value}</b> 条`
        }
      }),
      grid: makeGrid({ top: 20, bottom: 22 }),
      xAxis: makeAxis('category', {
        data: labels,
        // 类别/级别标签较少：动态保证间距，标签多/容器窄时旋转避免中文截断
        axisLabel: makeCategoryAxis(labels.length)
      }),
      yAxis: makeAxis('value'),
      series: [{
        type: 'bar',
        data: values.map((v, i) => ({ value: v, itemStyle: { color: colors[i] } })),
        barMaxWidth: 36,
        label: { show: true, position: 'top', color: CHART_THEME.textColor, fontSize: 10, formatter: '{c}' }
      }],
      animation: true,
      animationDuration: 400
    })
  }

  // 点击钻取
  const inst = getInstance()
  if (inst) {
    inst.off('click')
    inst.on('click', (params) => {
      const keys = Object.keys(groups)
      drillCategory.value = keys[params.dataIndex]
      renderDrill()
    })
  }
}

function renderDrill() {
  const alerts = rt.state.alerts.filter(a => {
    const key = groupBy.value === 'level' ? a.level : a.category
    return key === drillCategory.value
  })
  if (!alerts.length) { goBack(); return }

  const drillLabel = groupBy.value === 'level' ? LEVEL_LABELS[drillCategory.value] : CATEGORY_LABELS[drillCategory.value]
  // 按时间排序，取最近 20 条
  const sorted = alerts.slice(0, 20).reverse()
  const labels = sorted.map(a => fmt(a.created_at))
  const values = sorted.map(a => a.value || 0)

  setOption({
    color: [CHART_THEME.colorPalette[0]],
    tooltip: makeTooltip({
      trigger: 'axis',
      formatter: (params) => {
        const p = params[0]
        const a = sorted[p.dataIndex]
        if (!a) return ''
        let html = `<div style="font-weight:600">${p.axisValueLabel}</div>`
        html += `<div>${a.message}</div>`
        html += `<div>规则: ${a.rule_id}</div>`
        html += `<div>级别: ${LEVEL_LABELS[a.level] || a.level}</div>`
        if (a.device_id) html += `<div>设备: ${a.device_id}</div>`
        if (a.phase) html += `<div>阶段: ${a.phase}</div>`
        if (a.vehicle_flow_per_min != null) html += `<div>车流速度: ${a.vehicle_flow_per_min.toFixed(1)}/min</div>`
        if (a.threshold) html += `<div>阈值: ${a.threshold}</div>`
        return html
      }
    }),
    grid: makeGrid({ top: 20, bottom: 22 }),
    xAxis: makeAxis('category', {
      data: labels,
      // 钻取图 20 条时间标签较多：按容器宽度动态抽稀 + 间隔足够时保持水平、不足时旋转
      axisLabel: makeTimeAxis(labels.length)
    }),
    yAxis: makeAxis('value'),
    series: [{
      type: 'bar',
      data: values,
      barMaxWidth: 24,
      itemStyle: { color: LEVEL_COLORS[drillCategory.value] || CHART_THEME.colorPalette[0] },
      label: { show: true, position: 'top', color: CHART_THEME.textColor, fontSize: 9, formatter: '{c}' }
    }],
    animation: true,
    animationDuration: 400
  })
}

const drillLabel = computed(() => {
  if (!drillCategory.value) return ''
  return groupBy.value === 'level' ? LEVEL_LABELS[drillCategory.value] : CATEGORY_LABELS[drillCategory.value]
})

/**
 * 钻取图 X 轴时间标签自适应配置：
 * 按容器宽度计算抽稀间隔；宽容器水平展示，
 * 窄容器/标签多时旋转 30° 并预留更多底部空间，避免重叠与截断
 * @param {number} n 标签总数
 */
function makeTimeAxis(n) {
  if (!n) return { fontSize: 10, color: CHART_THEME.axisLabel, hideOverlap: true }
  const w = chartRef.value ? chartRef.value.clientWidth : 0
  const usable = Math.max(w - 40, 60)
  // 时间标签 "HH:mm" 约 40px 宽（含旋转后投影），每标签至少 40px
  const perLabel = 40
  const step = Math.max(1, Math.ceil((n * perLabel) / usable))
  // 空间充足时水平展示（字号 10 更清晰）；不足时才旋转，旋转后按高度方向容纳更多
  const rotate = step > 1 ? 30 : 0
  return {
    fontSize: 10,
    color: CHART_THEME.axisLabel,
    interval: step - 1,
    rotate,
    hideOverlap: true,   // 兜底：仍重叠的标签自动隐藏，保证文本可读
    margin: 10
  }
}

/**
 * 概述柱图 X 轴类别/级别标签配置：
 * 中文标签（≤4 字）按宽度动态保证每标签间距；
 * 标签数超过容器可容纳数时旋转 30°，保证文本完整不截断
 * @param {number} n 标签总数
 */
function makeCategoryAxis(n) {
  const w = chartRef.value ? chartRef.value.clientWidth : 0
  const usable = Math.max(w - 40, 80)
  // 4 字中文约 48px，每标签至少 46px
  const perLabel = 46
  const step = Math.max(1, Math.ceil((n * perLabel) / usable))
  return {
    fontSize: 10,
    color: CHART_THEME.axisLabel,
    interval: step - 1,
    rotate: step > 1 ? 30 : 0,
    hideOverlap: true,
    margin: 10
  }
}

function fmt(t) {
  if (!t) return ''
  return String(t).replace('T', ' ').slice(11, 16)  // 仅保留 HH:mm，缩短 X 轴标签宽度
}

function switchType(t) {
  chartType.value = t
  renderChart()
}

function goBack() {
  drillCategory.value = null
  renderChart()
}

function exportImg() {
  const url = exportImage()
  if (!url) return
  const a = document.createElement('a')
  a.href = url
  a.download = `alert-analysis-${Date.now()}.png`
  a.click()
}

watch(() => rt.state.alerts, () => {
  if (drillCategory.value) renderDrill()
  else renderChart()
}, { deep: true })

onMounted(async () => {
  try { rt.state.alerts = await listAlerts(50) } catch {}
  renderChart()
})
</script>
