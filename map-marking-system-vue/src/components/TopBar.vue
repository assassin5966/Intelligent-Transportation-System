<template>
  <div class="topbar">
    <canvas ref="cv" class="top-particles"></canvas>

    <div class="top-left">
      <span class="top-logo">◈</span>
      <span class="top-sub">智慧交管拥堵治理预警监控平台</span>
    </div>

    <div class="top-center">
      <select id="citySelect" :value="city" @change="onCity">
        <option value="datong">大同 · 古城</option>
        <option value="yungangshiku">大同 · 云冈石窟</option>
        <option value="xian">西安 · 雁塔</option>
      </select>
      <select id="styleSelect" :value="style" @change="onStyle" title="底图配色">
        <option v-for="o in styleOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
      </select>
      <span class="spacer"></span>
      <button class="tool-btn" @click="mapCtl.resetView()">⟲ 重置视角</button>
      <button class="tool-btn" @click="mapCtl.setTopView()">🔝 俯视</button>
      <button class="tool-btn" @click="mapCtl.setOblique()">🔜 斜视</button>
      <button class="tool-btn" @click="mapCtl.fitRange()">📐 适配范围</button>
      <button class="tool-btn" id="freeBtn" @click="mapCtl.toggleFree3D()">🧊 自由3D</button>
      <button class="tool-btn primary" @click="mapCtl.startAdd()">➕ 添加设备</button>
    </div>

    <div class="top-right">
      <div class="top-actions">
        <button class="top-action" @click="toggleTheme"
          :title="theme === 'dark' ? '切换到浅色模式' : '切换到深色模式'">
          <span class="ta-ic">{{ theme === 'dark' ? '☀️' : '🌙' }}</span>
          <span class="ta-lb">{{ theme === 'dark' ? '浅色模式' : '深色模式' }}</span>
        </button>
        <button class="top-action" @click="rulesOpen = true"
          title="业务规则配置（计数/告警/拥挤/警力/预测/异常/跟踪）">
          <span class="ta-ic">⚙</span><span class="ta-lb">业务规则</span>
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
        <div class="clock-divider"></div>
        <div class="clock-status">
          <span><i class="ws-dot" :class="rt.state.wsBackend ? 'on' : 'off'"></i>后端WS</span>
          <span><i class="ws-dot" :class="rt.state.restMode ? 'on' : 'off'"></i>REST兜底</span>
        </div>
      </div>
    </div>
  </div>

  <BusinessRulesPanel :open="rulesOpen" @close="rulesOpen = false" />
</template>

<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { useMapControl } from '../composables/useMapControl.js'
import { useRealtime } from '../composables/useRealtime.js'
import { useTheme } from '../composables/useTheme.js'
import BusinessRulesPanel from './BusinessRulesPanel.vue'

const { mapCtl } = useMapControl()
const rt = useRealtime()
const { theme, toggleTheme } = useTheme()

const rulesOpen = ref(false)
const city = ref('datong')
const style = ref(theme.value === 'dark' ? 'amap://styles/blue' : 'amap://styles/normal')
const cv = ref(null)
const dateEl = ref(null)
const timeEl = ref(null)
const weekEl = ref(null)

// 底图配色随主题提供不同预设（深色组 / 浅色组）
const STYLE_OPTIONS = {
  dark: [
    { value: 'amap://styles/blue', label: '🔵 科技蓝' },
    { value: 'amap://styles/dark', label: '🌑 深色标准' },
    { value: 'amap://styles/grey', label: '🌒 暗夜灰' }
  ],
  light: [
    { value: 'amap://styles/normal', label: '🎨 标准彩色' },
    { value: 'amap://styles/fresh', label: '💧 清新蓝' },
    { value: 'amap://styles/macaron', label: '🍬 马卡龙' },
    { value: 'amap://styles/wonderland', label: '🌈 绿野仙踪' }
  ]
}
const styleOptions = computed(() => STYLE_OPTIONS[theme.value] || STYLE_OPTIONS.dark)

function onCity(e) { city.value = e.target.value; mapCtl.changeCity(e.target.value) }
function onStyle(e) { style.value = e.target.value; mapCtl.changeStyle(e.target.value) }

// 主题切换时，底图配色选择器回到当前主题的默认样式（实际换瓦片由 MapPanel 的 theme watch 完成）
watch(theme, (t) => {
  style.value = t === 'dark' ? 'amap://styles/blue' : 'amap://styles/normal'
})

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
})
onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
  if (raf) cancelAnimationFrame(raf)
})
</script>
