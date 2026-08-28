<template>
  <div class="chart-card">
    <div class="chart-toolbar">
      <span class="chart-title">📈 历史车流报表</span>
      <div class="chart-filters">
        <select v-model="deviceId" title="统计对象（不选=全局合计）">
          <option value="">全局合计</option>
          <option v-for="d in dev.devices" :key="d.id" :value="d.id">{{ d.name }}</option>
        </select>
        <input type="date" v-model="startDate" :max="today" title="开始日期（整日起）" />
        <span class="range-sep">→</span>
        <input type="date" v-model="endDate" :max="today" title="结束日期（至 23 点）" />
        <button class="tool-btn mini primary" :disabled="loading" @click="query">🔍 查询</button>
        <button class="tool-btn mini" @click="exportImg" title="导出图片">📥</button>
      </div>
    </div>
    <div ref="chartRef" class="chart-box"></div>
    <div class="chart-loading" v-if="loading">报表数据加载中…</div>
    <div class="hh-error" v-if="error">
      <span>⚠ {{ error }}</span>
      <button class="tool-btn mini" @click="query">重试</button>
    </div>
    <div class="hh-meta" v-if="records.length && !loading">
      共 {{ records.length }} 条小时记录 · {{ summary }}
    </div>
  </div>
</template>

<script setup>
/**
 * 历史车流报表 —— api(8).md §4.5 GET /api/stats/hourly/history（v0.9.0 新增）
 * ---------------------------------------------------------------------------
 * - 查询历史任意时段的小时级车流/人流量（MySQL hourly_traffic 归档，跨 30 天窗口）；
 * - device_id 不传 = 全局合计（按日期+小时聚合所有设备）；
 * - start_date / end_date 支持 YYYY-MM-DD（整日）或 YYYY-MM-DD:HH（精确到小时）；
 * - 错误处理：422 时间格式非法 / 503 MySQL 未启用 / 404 设备不存在 / 网络超时，均展示并可重试；
 * - 加载状态：图表 loading 动画 + 禁用查询按钮。
 */
import { ref, computed, onMounted } from 'vue'
import { useEcharts, makeTooltip, makeGrid, makeLegend, makeAxis, CHART_THEME } from '../composables/useEcharts.js'
import { useDevices } from '../composables/useDevices.js'
import { getHourlyHistory } from '../api/endpoints.js'
import { useToast } from '../composables/useToast.js'

const { state: dev } = useDevices()
const { chartRef, setOption, exportImage, showLoading, hideLoading } = useEcharts()
const { push } = useToast()

const deviceId = ref('')
const today = new Date().toISOString().slice(0, 10)
// 默认查询最近 7 天
const startDate = ref(fmtDate(new Date(Date.now() - 6 * 864e5)))
const endDate = ref(today)

const loading = ref(false)
const error = ref('')
const records = ref([])

const summary = computed(() => {
  const vin = records.value.reduce((s, r) => s + (r.vehicle_in || 0), 0)
  const vout = records.value.reduce((s, r) => s + (r.vehicle_out || 0), 0)
  const pin = records.value.reduce((s, r) => s + (r.person_in || 0), 0)
  const pout = records.value.reduce((s, r) => s + (r.person_out || 0), 0)
  return `期间车辆 进/出 ${vin}/${vout} · 人员 进/出 ${pin}/${pout}`
})

function fmtDate(d) {
  const p = (n) => (n < 10 ? '0' + n : '' + n)
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate())
}

function render() {
  if (!records.value.length) return
  const labels = records.value.map((r) => `${r.stat_date.slice(5)} ${String(r.hour).padStart(2, '0')}时`)
  const vin = records.value.map((r) => r.vehicle_in)
  const vout = records.value.map((r) => r.vehicle_out)
  const pin = records.value.map((r) => r.person_in)
  const pout = records.value.map((r) => r.person_out)

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
        interval: Math.max(0, Math.floor(labels.length / 8))
      }
    }),
    yAxis: [
      makeAxis('value', { name: '车辆', nameTextStyle: { color: CHART_THEME.axisLabel, fontSize: 9 }, min: 0 }),
      makeAxis('value', { name: '人员', nameTextStyle: { color: CHART_THEME.axisLabel, fontSize: 9 }, min: 0, splitLine: { show: false } })
    ],
    dataZoom: labels.length > 24 ? [{ type: 'inside', start: 0, end: 100 }] : undefined,
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

/** 查询 §4.5 接口（含前端预校验 + 全类型错误处理） */
async function query() {
  // 前端预校验：日期必填且开始不晚于结束（后端同样会以 422 拦截）
  if (!startDate.value || !endDate.value) {
    error.value = '请先选择开始与结束日期'
    return
  }
  if (startDate.value > endDate.value) {
    error.value = '开始日期不能晚于结束日期'
    return
  }
  loading.value = true
  error.value = ''
  showLoading()
  try {
    const res = await getHourlyHistory(deviceId.value || null, startDate.value, endDate.value)
    records.value = (res && res.records) || []
    render()
    if (!records.value.length) {
      error.value = '该时段暂无归档数据（可能 MySQL 未启用或范围内无记录）'
    }
  } catch (e) {
    // 422 参数非法 / 503 MySQL 未启用 / 404 设备不存在 / 0 网络超时
    const map = { 422: '时间参数格式非法', 503: 'MySQL 长期归档未启用', 404: '设备不存在' }
    error.value = (map[e.status] ? map[e.status] + '：' : '') + (e.detail || e.message || '请求失败')
    push('历史报表查询失败：' + error.value, 'error', 4000)
    records.value = []
  } finally {
    hideLoading()
    loading.value = false
  }
}

function exportImg() {
  const url = exportImage()
  if (!url) return
  const a = document.createElement('a')
  a.href = url
  a.download = `hourly-history-${Date.now()}.png`
  a.click()
}

onMounted(() => {
  setOption({
    title: { text: '选择时间范围后点击「查询」', left: 'center', top: 'center', textStyle: { color: CHART_THEME.axisLabel, fontSize: 12 } }
  })
  query()
})
</script>

<style scoped>
/* —— 局部样式：仅作用于本组件 ——
 * 右侧栏位密集（路口信息/历史/道路/拥堵/告警/预测 共占约 493px），
 * 默认 flex 1 1 0 + min-height:0 会把图表容器压扁至 0，导致 ECharts canvas 不渲染。
 * 给 .chart-card 一个保底高度，并让 chart-box 至少 160px，确保四线图能正常绘制。
 */
.chart-card {
  min-height: 200px;
}
.chart-card :deep(.chart-box) {
  min-height: 160px;
}
.range-sep {
  color: #7f93b8;
  font-size: 11px;
}
.chart-filters input[type='date'] {
  background: rgba(0, 225, 255, 0.06);
  border: 1px solid rgba(0, 225, 255, 0.2);
  border-radius: 4px;
  color: #d4e4ff;
  font-size: 11px;
  padding: 2px 6px;
  height: 24px;
  color-scheme: dark;
  outline: none;
}
.chart-filters input[type='date']:focus {
  border-color: rgba(0, 225, 255, 0.5);
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
