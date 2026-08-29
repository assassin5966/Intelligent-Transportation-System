<template>
  <!-- ==================== 左侧卡片栏 ==================== -->
  <div class="side-col side-left">
    <!-- ① 全局信息：交通通行指标（全部监控设备合计，来自实时统计 §4.1） -->
    <div class="card">
      <div class="card-hd">🚦 交通通行指标 <span class="tag">全部监控设备合计</span></div>
      <div class="card-bd">
        <div class="metric">
          <span class="k">在场车辆</span>
          <span class="v">{{ s.current_vehicles ?? '—' }}</span>
        </div>
        <div class="metric">
          <span class="k">在场人员</span>
          <span class="v">{{ s.current_persons ?? '—' }}</span>
        </div>
        <div class="metric" style="margin-top:6px">
          <span class="k">今日车流 进/出</span>
          <span class="v">{{ s.today_vehicle_in ?? 0 }} / {{ s.today_vehicle_out ?? 0 }}</span>
        </div>
        <div class="metric">
          <span class="k">今日人流 进/出</span>
          <span class="v">{{ s.today_person_in ?? 0 }} / {{ s.today_person_out ?? 0 }}</span>
        </div>
        <div class="metric">
          <span class="k">活跃 / 异常设备</span>
          <span class="v good">{{ s.active_devices ?? 0 }}
            <span style="color:#ffd23f;font-size:12px">/ {{ abnormalCount }} 异常</span></span>
        </div>
        <div style="margin-top:8px;font-size:11px;color:#7f93b8">
          通行指数(估算)：{{ idx }}<span v-if="idxLevel"> · {{ idxLevel }}</span>
        </div>
        <div class="bar"><i :style="{ width: idxPct + '%', background: idxColor }"></i></div>
      </div>
    </div>

    <!-- ② 警力分配 -->
    <PolicePanel />
  </div>

  <!-- ==================== 右侧卡片栏 ==================== -->
  <div class="side-col side-right">
    <!-- ③ 时序预测 -->
    <PredictionPanel />

    <!-- ④ 按天人数/车流统计折线图 -->
    <DailyTrendChart />
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useRealtime } from '../composables/useRealtime.js'
import { useDevices } from '../composables/useDevices.js'
import PolicePanel from './PolicePanel.vue'
import PredictionPanel from './PredictionPanel.vue'
import DailyTrendChart from './DailyTrendChart.vue'

const rt = useRealtime()
const dev = useDevices().state

const s = computed(() => rt.state.stats || {})

// 异常设备数（ws 无数据/未在线 -> abnormal，前端标灰展示）
const abnormalCount = computed(() => dev.devices.filter((d) => d.status === 'abnormal').length)

// 通行指数（按在场车辆估算，仅展示用）
const idx = computed(() => {
  const v = s.value.current_vehicles || 0
  return Math.max(0, Math.min(100, Math.round(100 - v * 0.7)))
})
const idxLevel = computed(() => idx.value > 85 ? '畅通' : idx.value > 70 ? '缓行' : '拥堵')
const idxColor = computed(() => idx.value > 85 ? '#5fe39a' : idx.value > 70 ? '#ffd23f' : '#ff6b6b')
const idxPct = computed(() => idx.value)
</script>
