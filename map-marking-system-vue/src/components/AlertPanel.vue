<template>
  <div class="card">
    <div class="card-hd">🚨 实时告警 <span class="tag crit">{{ alerts.length }}</span></div>
    <div class="card-bd">
      <!-- ECharts 告警分析图（按级别/类别分组 + 钻取） -->
      <AlertTrendChart />
      <div v-if="alerts.length === 0" class="empty">暂无告警</div>
      <div class="alert-item" v-for="a in alerts.slice(0, 20)" :key="a.id"
        :class="a.level === 'critical' ? 'critical' : a.level === 'warning' ? 'warning' : ''">
        <span class="lv" :class="a.level">{{ levelText(a.level) }}</span>
        <span v-if="a.phase" class="phase-tag" :class="a.phase">{{ phaseText(a.phase) }}</span>
        {{ a.message }}
        <div style="color:#7f93b8;font-size:11px;margin-top:2px">
          {{ fmt(a.created_at) }} · {{ categoryText(a.category) }}
          <span v-if="a.device_id" style="margin-left:6px">· 设备: {{ a.device_id }}</span>
          <span v-if="a.congestion_score != null" style="margin-left:6px">· 拥挤度 {{ a.congestion_score }}</span>
          <span v-if="a.vehicle_congested" style="margin-left:6px;color:#ff6b6b">· 车维度</span>
          <span v-if="a.person_congested" style="margin-left:6px;color:#ff6b6b">· 人维度</span>
          <span v-if="a.vehicle_flow_per_min != null" style="margin-left:6px">· {{ a.vehicle_flow_per_min.toFixed(1) }}/min</span>
          <span v-if="a.anomaly_type" style="margin-left:6px">· {{ a.anomaly_type === 'black_screen' ? '黑屏' : '花屏' }}</span>
        </div>
      </div>
      <button class="tool-btn" style="margin-top:10px;width:100%" @click="simAnomaly">
        ⚡ 模拟上报黑屏异常
      </button>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, onMounted } from 'vue'
import { useRealtime } from '../composables/useRealtime.js'
import { useToast } from '../composables/useToast.js'
import { reportAnomaly, listAlerts } from '../api/endpoints.js'
import { useDevices } from '../composables/useDevices.js'
import AlertTrendChart from './AlertTrendChart.vue'

const rt = useRealtime()
const { push } = useToast()
const dev = useDevices().state
const alerts = computed(() => rt.state.alerts)

onMounted(async () => {
  try { rt.state.alerts = await listAlerts(50) } catch {}
})

function fmt(t) {
  if (!t) return ''
  return String(t).replace('T', ' ').slice(0, 19)
}

function levelText(l) {
  return { critical: '严重', warning: '警告', info: '信息' }[l] || l
}

function phaseText(p) {
  return { onset: '发生', recovery: '恢复' }[p] || p
}

function categoryText(c) {
  return {
    vehicle_saturate: '车辆饱和',
    person_saturate: '人员饱和',
    congestion: '拥堵',
    video_anomaly: '视频异常'
  }[c] || c
}

// §5.3 模拟视频异常上报（前端亦可注入告警）
async function simAnomaly() {
  const id = (dev.devices[0] && dev.devices[0].id) || 'CAM001'
  try {
    await reportAnomaly({ device_id: id, anomaly_type: 'black_screen', phase: 'onset' })
    push('已上报黑屏异常，告警已写入', 'success')
  } catch (e) {
    push('上报失败：' + (e.detail || e.message), 'error')
  }
}
</script>
