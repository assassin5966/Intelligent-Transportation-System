<template>
  <div class="chart-card">
    <div class="chart-toolbar">
      <span class="chart-title">📅 按天人数/车流统计</span>
      <div class="chart-filters">
        <button class="tool-btn mini" :class="{ active: days === 7 }" @click="setDays(7)">近7天</button>
        <button class="tool-btn mini" :class="{ active: days === 14 }" @click="setDays(14)">近14天</button>
        <button class="tool-btn mini" :class="{ active: days === 30 }" @click="setDays(30)">近30天</button>
        <button class="tool-btn mini" @click="exportImg" title="导出图片">📥</button>
      </div>
    </div>
    <div ref="chartRef" class="chart-box"></div>
    <div class="chart-loading" v-if="loading">按天统计数据加载中…</div>
    <div class="hh-error" v-if="error">
      <span>⚠ {{ error }}</span>
      <button class="tool-btn mini" @click="query">重试</button>
    </div>
    <div class="hh-meta" v-if="labels.length && !loading">
      {{ summary }}
    </div>
  </div>
</template>

<script setup>
/**
 * 按天人数/车流统计折线图
 * ---------------------------------------------------------------------------
 * 数据源: GET /api/stats/hourly/history（全局合计, 不传 device_id）
 * 后端返回逐小时 records（stat_date + hour + 四项计数）, 前端按 stat_date
 * 聚合成每日总量: 车辆进/出、人员进/出, X 轴为日期, 双 Y 轴（车辆/人员）。
 * 错误处理: 422 时间非法 / 503 MySQL 未启用 / 网络超时, 均展示并可重试。
 */
import { ref, computed, onMounted } from 'vue'
import { useEcharts, makeTooltip, makeGrid, makeLegend, makeAxis, CHART_THEME } from '../composables/useEcharts.js'
import { getHourlyHistory } from '../api/endpoints.js'
import { useToast } from '../composables/useToast.js'

// 注意：加载态不使用 useEcharts 的 showLoading/hideLoading（其依赖 ECharts 实例，
// 实例未就绪时空操作，会造成"一直加载中"假象），统一由 .chart-loading 文案承载。
const { chartRef, setOption, exportImage } = useEcharts()
const { push } = useToast()

const days = ref(14)
const loading = ref(false)
const error = ref('')
const records = ref([])

const labels = computed(() => Object.keys(aggregate()))
const summary = computed(() => {
  const agg = aggregate()
  const daysCount = Object.keys(agg).length
  let vin = 0, vout = 0, pin = 0, pout = 0
  Object.values(agg).forEach((d) => {
    vin += d.vin; vout += d.vout; pin += d.pin; pout += d.pout
  })
  return `近 ${daysCount} 天合计 · 车流 进/出 ${vin}/${vout} · 人流 进/出 ${pin}/${pout}`
})

function fmtDate(d) {
  const p = (n) => (n < 10 ? '0' + n : '' + n)
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate())
}

/** 超时兜底：即使底层请求挂起（如 MySQL 慢/网络异常），也强制结束并给出可读错误 */
function withTimeout(promise, ms) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error(`请求超时(${ms / 1000}s)`)), ms)
    promise.then(
      (v) => { clearTimeout(t); resolve(v) },
      (e) => { clearTimeout(t); reject(e) }
    )
  })
}

function setDays(n) {
  if (days.value === n) return
  days.value = n
  query()
}

/** 按 stat_date 聚合逐小时记录为每日总量（key 为 ISO 日期, 字典序即时间序） */
function aggregate() {
  const byDay = {}
  for (const r of records.value) {
    const d = String(r.stat_date || '').slice(0, 10)
    if (!d) continue
    if (!byDay[d]) byDay[d] = { vin: 0, vout: 0, pin: 0, pout: 0 }
    byDay[d].vin += r.vehicle_in || 0
    byDay[d].vout += r.vehicle_out || 0
    byDay[d].pin += r.person_in || 0
    byDay[d].pout += r.person_out || 0
  }
  return byDay
}

function render() {
  const agg = aggregate()
  const dates = Object.keys(agg)
  if (!dates.length) return
  const labels = dates.map((d) => d.slice(5)) // MM-DD
  const vin = dates.map((d) => agg[d].vin)
  const vout = dates.map((d) => agg[d].vout)
  const pin = dates.map((d) => agg[d].pin)
  const pout = dates.map((d) => agg[d].pout)

  setOption({
    color: [CHART_THEME.colorPalette[0], CHART_THEME.colorPalette[3], CHART_THEME.colorPalette[1], CHART_THEME.colorPalette[5]],
    tooltip: makeTooltip({
      formatter: (params) => {
        let html = `<div style="font-weight:600;margin-bottom:4px">${params[0].axisValueLabel || params[0].name}</div>`
        params.forEach((p) => {
          html += `<div>${p.marker} ${p.seriesName}: <b style="color:${p.color}">${p.value}</b></div>`
        })
        return html
      }
    }),
    legend: makeLegend({ data: ['车辆进', '车辆出', '人员进', '人员出'], type: 'scroll' }),
    grid: makeGrid({ top: 30, bottom: 8 }),
    xAxis: makeAxis('category', {
      data: labels,
      axisLabel: {
        color: CHART_THEME.axisLabel, fontSize: 9,
        interval: Math.max(0, Math.floor(labels.length / 7))
      }
    }),
    yAxis: [
      makeAxis('value', { name: '车辆', nameTextStyle: { color: CHART_THEME.axisLabel, fontSize: 9 }, min: 0 }),
      makeAxis('value', { name: '人员', nameTextStyle: { color: CHART_THEME.axisLabel, fontSize: 9 }, min: 0, splitLine: { show: false } })
    ],
    series: [
      { name: '车辆进', type: 'line', smooth: true, showSymbol: false, data: vin, yAxisIndex: 0, areaStyle: { opacity: 0.1 } },
      { name: '车辆出', type: 'line', smooth: true, showSymbol: false, data: vout, yAxisIndex: 0 },
      { name: '人员进', type: 'line', smooth: true, showSymbol: false, data: pin, yAxisIndex: 1, areaStyle: { opacity: 0.06 } },
      { name: '人员出', type: 'line', smooth: true, showSymbol: false, data: pout, yAxisIndex: 1, lineStyle: { type: 'dashed' } }
    ],
    animation: true,
    animationDuration: 400
  })
}

/** 查询最近 N 天的逐小时记录（全局合计）并聚合渲染 */
async function query() {
  loading.value = true
  error.value = ''
  try {
    const end = fmtDate(new Date())
    const start = fmtDate(new Date(Date.now() - (days.value - 1) * 864e5))
    const res = await withTimeout(getHourlyHistory(null, start, end), 8000)
    records.value = (res && res.records) || []
    render()
    if (!records.value.length) {
      error.value = '该时段暂无归档数据（可能 MySQL 未启用或范围内无记录）'
    }
  } catch (e) {
    const map = { 422: '时间参数格式非法', 503: 'MySQL 长期归档未启用', 404: '设备不存在' }
    error.value = (map[e.status] ? map[e.status] + '：' : '') + (e.detail || e.message || '请求失败')
    push('按天统计查询失败：' + error.value, 'error', 4000)
    records.value = []
  } finally {
    // 无论成功失败、是否抛错，都必须关闭加载态（不依赖 ECharts 实例）
    loading.value = false
  }
}

function exportImg() {
  const url = exportImage()
  if (!url) return
  const a = document.createElement('a')
  a.href = url
  a.download = `daily-trend-${Date.now()}.png`
  a.click()
}

onMounted(() => {
  // 不设置"加载中"占位 title：避免 ECharts 实例晚于挂载创建时，setOption(lastOption)
  // 恢复占位文案且不被真实图表覆盖，造成"一直显示加载中"的假象。
  query()
})
</script>

<style scoped>
/* 与 HourlyHistoryChart 一致: 保底高度, 避免被 flex 容器压扁导致图表不渲染 */
.chart-card {
  min-height: 200px;
}
.chart-card :deep(.chart-box) {
  min-height: 160px;
}
.hh-error {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 6px;
  padding: 6px 10px;
  border-radius: 6px;
  background: rgba(255, 107, 107, 0.1);
  border: 1px solid rgba(255, 107, 107, 0.3);
  color: #ff8a8a;
  font-size: 11px;
}
.hh-meta {
  margin-top: 6px;
  font-size: 11px;
  color: #7f93b8;
}
</style>
