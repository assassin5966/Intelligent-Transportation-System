<template>
  <div class="topbar">
    <canvas ref="cv" class="top-particles"></canvas>

    <div class="top-left">
      <span class="top-logo">◈</span>
      <span class="top-sub">智慧交管拥堵治理预警监控平台</span>
    </div>

    <div class="top-center">
      <!-- 设备检索：先按「门」再按设备匹配（门 = 地图上的门级大卡，设备 = 32 路标点） -->
      <div ref="boxEl" class="dev-search" @focusout="onSearchBlur">
        <span class="ds-ic">🔍</span>
        <input v-model="kw" class="ds-input" type="search"
          placeholder="搜索门 / 设备名称，回车定位"
          @focus="open = true" @input="open = true"
          @keydown.enter.prevent="pickFirst" @keydown.esc.stop="closeList" />
        <button v-if="kw" class="ds-clear" type="button" title="清空" @mousedown.prevent @click="clearKw">✕</button>
      </div>

      <!-- 下拉挂到 body：顶栏 overflow:hidden 会裁掉、且层叠层级低于地图大卡(9700+)，
           用 Teleport + position:fixed 才能稳定浮在卡片之上 -->
      <Teleport to="body">
        <div class="ds-list" v-if="open && kw" :style="listStyle">
          <div v-if="!gateResults.length && !devResults.length" class="ds-empty">未找到匹配的门 / 设备</div>

          <!-- 门：定位到该门大卡并闪烁一次 -->
          <div v-for="g in gateResults" :key="'gate-' + g.id" class="ds-item"
            title="点击定位到该门" @mousedown.prevent @click="pickGate(g)">
            <span class="ds-kind">门</span>
            <span class="ds-name">{{ g.id }}</span>
            <span class="ds-id">{{ g.edge }}{{ g.direction !== '其他' ? ' · ' + g.direction : '' }}</span>
            <span class="ds-st" :data-status="g.status">{{ statusText(g.status) }}</span>
          </div>

          <!-- 设备：定位到该路标点并展开信息卡 -->
          <div v-for="d in devResults" :key="d.id" class="ds-item"
            :class="{ 'no-geo': !hasGeo(d.id) }"
            :title="hasGeo(d.id) ? '点击定位到该设备' : '该设备暂无地图坐标，无法定位'"
            @mousedown.prevent @click="pick(d)">
            <span class="ds-kind">设备</span>
            <span class="ds-name">{{ d.name || d.id }}</span>
            <span class="ds-id">{{ d.id }}</span>
            <span class="ds-st" :data-status="d.status">{{ statusText(d.status) }}</span>
          </div>
        </div>
      </Teleport>
    </div>

    <div class="top-right">
      <div class="top-actions">
        <button class="tool-btn" @click="mapCtl.fitRange()">📐 适配范围</button>
        <button class="tool-btn primary" @click="goOps('device-info.html')">🛠 设备管理</button>
        <button class="tool-btn" @click="goOps('device-config.html')">📡 计数启流</button>
        <button class="tool-btn" @click="goOps('business-rules.html')">⚙ 业务规则</button>
        <span class="spacer"></span>
        <button class="top-action" @click="toggleTheme"
          :title="theme === 'dark' ? '切换到浅色模式' : '切换到深色模式'">
          <span class="ta-ic">{{ theme === 'dark' ? '☀️' : '🌙' }}</span>
          <span class="ta-lb">{{ theme === 'dark' ? '浅色模式' : '深色模式' }}</span>
        </button>
      </div>
      <div class="clock-panel">
        <div class="clock-main">
          <div class="clock-time" ref="timeEl">--:--:--</div>
          <div class="clock-date-row">
            <span class="clock-date" ref="dateEl">---- -- --</span>
            <span class="clock-week" ref="weekEl">周-</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { useMapControl } from '../composables/useMapControl.js'
import { useTheme } from '../composables/useTheme.js'
import { useDevices } from '../composables/useDevices.js'
import { API_BASE } from '../api/config.js'

const { mapCtl } = useMapControl()
const dev = useDevices()
const { theme, toggleTheme } = useTheme()

const cv = ref(null)
const dateEl = ref(null)
const timeEl = ref(null)
const weekEl = ref(null)

/** 打开后端运维页面 (设备管理 / 计数启流 / 业务规则), 新标签页以免丢失大屏 */
function goOps(file) {
  window.open(`${API_BASE}/static/${file}`, '_blank')
}

// —— 门 / 设备检索（即时匹配 → 地图定位）——
const kw = ref('')
const open = ref(false)
const boxEl = ref(null)     // 搜索框（下拉定位基准）
const listStyle = ref({})   // 下拉为 body 上的 fixed 元素，需手动对齐搜索框
/** 门级大卡列表（由 MapPanel 随设备数据发布到控制总线） */
const gates = computed(() => mapCtl.gates || [])
/** 门匹配：门断面名 / 大城门 / 所在城墙 / 进出处 */
const gateResults = computed(() => {
  const q = kw.value.trim().toLowerCase()
  if (!q) return []
  return gates.value
    .filter((g) => `${g.id || ''} ${g.gate || ''} ${g.edge || ''} ${g.direction || ''}`.toLowerCase().includes(q))
    .slice(0, 6)
})
/** 设备匹配：编号 / 名称 / 分类 */
const devResults = computed(() => {
  const q = kw.value.trim().toLowerCase()
  if (!q) return []
  return dev.state.devices
    .filter((d) => `${d.id || ''} ${d.name || ''} ${d.category || ''}`.toLowerCase().includes(q))
    .slice(0, 8)
})
/** 把下拉对齐到搜索框正下方（顶栏固定不滚动，仅需在打开与窗口变化时同步） */
function syncListPos() {
  const el = boxEl.value
  if (!el) return
  const r = el.getBoundingClientRect()
  listStyle.value = { left: `${r.left}px`, top: `${r.bottom + 6}px`, width: `${r.width}px` }
}
watch(open, (v) => { if (v) nextTick(syncListPos) })
function onWinResize() { if (open.value) syncListPos() }
function hasGeo(id) {
  const p = dev.state.positions[id]
  return !!(p && Number.isFinite(p.lng) && Number.isFinite(p.lat))
}
function statusText(s) {
  return { online: '在线', offline: '离线', abnormal: '异常', syncing: '同步中' }[s] || '—'
}
function pick(d) {
  if (!d) return
  open.value = false
  kw.value = d.name || d.id
  mapCtl.focusDevice(d.id)
}
function pickGate(g) {
  if (!g) return
  open.value = false
  kw.value = g.id
  mapCtl.focusGate(g.id)
}
// 回车定位：门优先（大屏以门级大卡为主视图），无门命中再取设备
function pickFirst() {
  if (gateResults.value.length) { pickGate(gateResults.value[0]); return }
  if (devResults.value.length) pick(devResults.value[0])
}
function clearKw() { kw.value = ''; open.value = false }
function closeList() { open.value = false }
// 失焦收起下拉：条目用 mousedown.prevent 保持焦点，故此处可立即收起
function onSearchBlur() { open.value = false }

// —— 实时时钟 ——
const wk = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']
const p = (n) => (n < 10 ? '0' : '') + n
let timer = null
function tick() {
  const n = new Date()
  if (dateEl.value) dateEl.value.textContent = `${n.getFullYear()}-${p(n.getMonth() + 1)}-${p(n.getDate())}`
  if (timeEl.value) timeEl.value.textContent = `${p(n.getHours())}:${p(n.getMinutes())}:${p(n.getSeconds())}`
  if (weekEl.value) weekEl.value.textContent = wk[n.getDay()]
}

// —— 顶栏粒子动画 ——
let raf = null
function initParticles() {
  const c = cv.value
  if (!c || !c.getContext) return
  const ctx = c.getContext('2d')
  const resize = () => { c.width = c.clientWidth; c.height = c.clientHeight }
  resize()
  window.addEventListener('resize', resize)
  const ps = Array.from({ length: 48 }, () => ({
    x: Math.random() * c.width, y: Math.random() * c.height,
    r: Math.random() * 1.8 + 0.4, s: Math.random() * 0.4 + 0.12, a: Math.random() * 0.5 + 0.2
  }))
  const loop = () => {
    ctx.clearRect(0, 0, c.width, c.height)
    ps.forEach((q) => {
      q.y -= q.s
      if (q.y < -2) { q.y = c.height + 2; q.x = Math.random() * c.width }
      ctx.beginPath(); ctx.arc(q.x, q.y, q.r, 0, Math.PI * 2)
      ctx.fillStyle = `rgba(0,225,255,${q.a})`
      ctx.shadowColor = 'rgba(0,225,255,.85)'; ctx.shadowBlur = 6; ctx.fill()
    })
    raf = requestAnimationFrame(loop)
  }
  loop()
}

onMounted(() => {
  tick(); timer = setInterval(tick, 1000)
  initParticles()
  window.addEventListener('resize', onWinResize)
})
onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
  if (raf) cancelAnimationFrame(raf)
  window.removeEventListener('resize', onWinResize)
})
</script>
