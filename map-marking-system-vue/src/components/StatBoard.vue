<template>
  <!-- ==================== 左侧卡片栏 ==================== -->
  <div class="side-col side-left">
    <!-- ① 交通通行指标（来自实时统计 §4.1） -->
    <div class="card">
      <div class="card-hd">🚦 交通通行指标 <span class="tag">实时</span></div>
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
          <span class="k">活跃设备</span>
          <span class="v good">{{ s.active_devices ?? 0 }}</span>
        </div>
        <div class="metric" style="margin-top:4px">
          <span class="k">拥堵分级</span>
          <span class="v">
            <span class="cong-chip r">{{ cong.severe }}</span><span class="cong-chip y">{{ cong.mid }}</span><span class="cong-chip g">{{ cong.normal }}</span>
          </span>
        </div>
        <div style="margin-top:8px;font-size:11px;color:#7f93b8">
          通行指数(估算)：{{ idx }}<span v-if="idxLevel"> · {{ idxLevel }}</span>
        </div>
        <div class="bar"><i :style="{ width: idxPct + '%', background: idxColor }"></i></div>
      </div>
    </div>

      <!-- ② 车辆轨迹（机动车摄像头实时计数 §4.2，v0.7 含拥堵状态） -->
    <div class="card">
      <div class="card-hd">🚗 车辆轨迹 <span class="tag" v-if="cong.vCong > 0 || cong.pCong > 0"
          style="background:rgba(255,107,107,.2);color:#ff6b6b">⚠ {{ cong.vCong }} 车 / {{ cong.pCong }} 人 拥堵</span></div>
      <div class="card-bd">
        <div v-if="vehicleDevs.length === 0" class="empty">暂无机动车摄像头</div>
        <div class="item" v-for="d in vehicleDevs" :key="d.id">
          <span><span class="dot" :class="dotCls(d.status)"></span>{{ d.name }}
            <span v-if="d.stat && d.stat.congested" class="cong-tag">拥堵</span>
          </span>
          <span class="v" :class="d.stat && d.stat.congested ? 'bad' : ''">{{ d.stat ? d.stat.current_vehicles : 0 }}
            辆</span>
          <div class="cong-detail" v-if="d.stat && d.stat.max_vehicles > 0">
            <span class="cong-mini">ROI {{ d.stat.roi_vehicles }}/{{ d.stat.max_vehicles }}</span>
            <span class="cong-mini">{{ (d.stat.vehicle_flow_per_min || 0).toFixed(1) }}/min</span>
          </div>
        </div>
      </div>
    </div>

    <!-- ③ 路口监测（设备在线状态 + 各设备计数图表） -->
    <div class="card">
      <div class="card-hd">📡 路口监测</div>
      <div class="card-bd">
        <div v-if="dev.devices.length === 0" class="empty">加载中…</div>
        <div class="item" v-for="d in dev.devices" :key="d.id">
          <span><span class="dot" :class="dotCls(d.status)"></span>{{ d.name }}</span>
          <span class="v" :class="txtCls(d.status)">{{ statusText(d.status) }}</span>
        </div>
      </div>
    </div>

    <!-- ③.5 各设备计数图表（§4.2/§4.3，支持分类筛选 + 钻取 + 图表切换） -->
    <DeviceCountChart />

    <!-- ⑦ 警力分配（§7，附加功能卡片） -->
    <PolicePanel />
  </div>

  <!-- ==================== 右侧卡片栏 ==================== -->
  <div class="side-col side-right">
    <!-- ④ 路口信息（选中/首个设备） -->
    <div class="card">
      <div class="card-hd">🛣️ 路口信息</div>
      <div class="card-bd" v-if="focus">
        <div class="metric"><span class="k">设备名称</span><span class="v">{{ focus.name }}</span></div>
        <div class="metric"><span class="k">类型</span><span class="v">{{ typeText(focus.camera_type) }}</span></div>
        <div class="metric"><span class="k">状态</span><span class="v" :class="txtCls(focus.status)">{{
          statusText(focus.status) }}</span></div>
        <div class="metric"><span class="k">中心坐标</span><span class="v">{{ posText }}</span></div>
        <div class="metric"><span class="k">今日车进/出</span><span class="v">{{ focus.stat ? focus.stat.today_vehicle_in : 0
            }} / {{ focus.stat ? focus.stat.today_vehicle_out : 0 }}</span></div>
        <div class="metric"><span class="k">今日人进/出</span><span class="v">{{ focus.stat ? focus.stat.today_person_in : 0
            }} / {{ focus.stat ? focus.stat.today_person_out : 0 }}</span></div>
        <div class="metric" v-if="focus.stat && focus.stat.max_vehicles > 0">
          <span class="k">拥堵阈值</span>
          <span class="v">{{ focus.stat.max_vehicles }} 辆</span>
        </div>
        <div class="metric" v-if="focus.stat && focus.stat.max_vehicles > 0">
          <span class="k">ROI 车辆</span>
          <span class="v" :class="focus.stat.congested ? 'bad' : 'good'">{{ focus.stat.roi_vehicles }} <span
              style="font-size:11px;color:#7f93b8">{{ focus.stat.congested ? '⚠ 拥堵' : '正常' }}</span></span>
        </div>
        <div class="metric" v-if="focus.stat && focus.stat.max_vehicles > 0">
          <span class="k">车流速度</span>
          <span class="v">{{ (focus.stat.vehicle_flow_per_min || 0).toFixed(1) }} 辆/min</span>
        </div>
      </div>
      <div class="card-bd" v-else>
        <div class="empty">请选择设备</div>
      </div>
    </div>

    <!-- ⑤ 历史车流报表（§4.5 小时级长期报表，v0.9.0 新增） -->
    <HourlyHistoryChart />

    <!-- ⑥ 道路监控 -->
    <div class="card">
      <div class="card-hd">🎥 道路监控 <span class="tag warn">待接入</span></div>
      <div class="card-bd">
        <VideoCard />
      </div>
    </div>

    <!-- ⑦ 拥堵状态仪表（v0.7.0 §4.4） -->
    <CongestionChart />

    <!-- ⑧ 实时告警（§5，含 v0.7 拥堵/异常告警） -->
    <AlertPanel />

    <!-- ⑨ 时序预测（§6，ECharts 图表） -->
    <PredictionPanel />
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useRealtime } from '../composables/useRealtime.js'
import { useDevices } from '../composables/useDevices.js'
import VideoCard from './VideoCard.vue'
import PolicePanel from './PolicePanel.vue'
import AlertPanel from './AlertPanel.vue'
import PredictionPanel from './PredictionPanel.vue'
import StatsTrendChart from './StatsTrendChart.vue'
import DeviceCountChart from './DeviceCountChart.vue'
import CongestionChart from './CongestionChart.vue'
import HourlyHistoryChart from './HourlyHistoryChart.vue'

const rt = useRealtime()
const dev = useDevices().state

const s = computed(() => rt.state.stats || {})

// v0.7.0 拥堵设备计数
const congestedCount = computed(() => {
  return dev.devices.filter(d => {
    const st = dev.statsById[d.id]
    return st && st.congested
  }).length
})

// v0.11.0 拥堵分级汇总：按 congestion_score 阈值 + 车/人维度分别计数
const cong = computed(() => {
  let severe = 0, mid = 0, normal = 0, vCong = 0, pCong = 0
  dev.devices.forEach((d) => {
    const st = dev.statsById[d.id]
    if (!st) { normal++; return }
    const sc = st.congestion_score || 0
    if (sc >= 0.66) severe++
    else if (sc >= 0.33) mid++
    else normal++
    if (st.vehicle_congested) vCong++
    if (st.person_congested) pCong++
  })
  return { severe, mid, normal, vCong, pCong }
})

// 通行指数（按在场车辆估算，仅展示用）
const idx = computed(() => {
  const v = s.value.current_vehicles || 0
  return Math.max(0, Math.min(100, Math.round(100 - v * 0.7)))
})
const idxLevel = computed(() => idx.value > 85 ? '畅通' : idx.value > 70 ? '缓行' : '拥堵')
const idxColor = computed(() => idx.value > 85 ? '#5fe39a' : idx.value > 70 ? '#ffd23f' : '#ff6b6b')
const idxPct = computed(() => idx.value)

const vehicleDevs = computed(() => dev.devices.filter((d) => d.camera_type !== 'person'))

const focus = computed(() => {
  const list = dev.devices
  if (!list.length) return null
  const sel = list.find((d) => d.id === dev.selectedId)
  return sel || list[0]
})
const posText = computed(() => {
  if (!focus.value) return '—'
  const p = dev.positions[focus.value.id]
  return p ? `${p.lng.toFixed(3)}, ${p.lat.toFixed(3)}` : '—'
})

const analysis = computed(() => {
  const cv = s.value.current_vehicles || 0
  const cp = s.value.current_persons || 0
  const total = cv + cp || 1
  return [
    { k: '机动车流量', v: Math.round((cv / total) * 100), c: '#00e1ff' },
    { k: '人流密度', v: Math.round((cp / total) * 100), c: '#5fe39a' },
    { k: '事故风险', v: Math.min(99, Math.round((s.value.active_devices || 0) * 8)), c: '#ff6b6b' }
  ]
})

function dotCls(st) { return st === 'online' ? 'g' : st === 'offline' ? 'r' : 'y' }
function txtCls(st) { return st === 'online' ? 'good' : st === 'offline' ? 'bad' : 'mid' }
function statusText(st) { return st === 'online' ? '在线' : st === 'offline' ? '离线' : st === 'synced' ? '已同步' : '已注册' }
function typeText(t) { return t === 'vehicle' ? '机动车' : t === 'person' ? '人流' : '全部' }
</script>
