<template>
  <!-- ==================== 轨迹标注视频（放大弹窗内） ====================
       选择已启流计数的设备 → 拉取 /play 播放地址 (FLV/HLS) 实时播放,
       同时经后端 /ws/tracks 转发通道订阅 AI 分析服务的 tracks 消息,
       在透明 canvas 上叠加检测框 (人绿/车橙) + 运动轨迹线 + 轨迹ID + 越线红圈.
       数据通道: 前端 → 业务后端 8000 /ws/tracks → AI 服务 8001 (架构约定: 前端不直连 AI). -->
  <div class="vtp">
    <div class="vtp-bar">
      <select v-model="deviceId" class="vtp-select" @change="onDeviceChange">
        <option value="" disabled>选择设备…</option>
        <option v-for="d in devices" :key="d.id" :value="d.id">
          {{ d.name || d.id }} [{{ d.status || '?' }}]
        </option>
      </select>
      <button class="vtp-btn" :disabled="!deviceId || playing" @click="play">▶ 播放</button>
      <button v-if="playing" class="vtp-btn stop" @click="stop">■ 停止</button>
      <span class="vtp-status" :class="{ on: playing, err: !!error }">{{ statusText }}</span>
    </div>

    <div class="vtp-stage">
      <video ref="videoEl" muted autoplay playsinline></video>
      <canvas ref="canvasEl"></canvas>
      <div v-if="!playing && !error" class="vtp-empty">
        <div class="vtp-empty-ic">📹</div>
        <div>选择已启流计数的设备后点「播放」</div>
        <div class="vtp-empty-sub">实时画面上叠加 AI 检测框与运动轨迹；未启流设备仅可看画面</div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onBeforeUnmount } from 'vue'
import { listDevices, getDevicePlayUrl } from '../api/endpoints.js'

// initialDeviceId: 从「视频详情」某一路点入时预选该设备并自动播放
const props = defineProps({ initialDeviceId: { type: String, default: '' } })

const videoEl = ref(null)
const canvasEl = ref(null)
const devices = ref([])
const deviceId = ref('')
const playing = ref(false)
const error = ref('')
const statusText = computed(() => {
  if (error.value) return error.value
  if (playing.value) return `叠加中 · ${deviceId.value.slice(0, 18)}…（检测框 / 轨迹 / 计数线 / 锚点 / ROI / 越线事件）`
  return '未播放'
})

let player = null        // flv.js / hls.js 实例
let ws = null            // /ws/tracks 转发通道
let rafId = null
let latestTracks = null  // 当前设备最近一帧 tracks 消息
let crossingFlash = null // { at, x, y } 越线事件闪烁
let geom = null          // 当前设备计数线/锚点/ROI (归一化坐标, 随播放对齐画布)

listDevices().then(res => {
  devices.value = Array.isArray(res) ? res : []
  if (props.initialDeviceId && devices.value.some(d => d.id === props.initialDeviceId)) {
    // 「视频详情」某一路点入: 预选该设备并自动播放
    deviceId.value = props.initialDeviceId
    play()
    return
  }
  // 默认选中第一台已启流 (online) 的设备
  const online = devices.value.find(d => d.status === 'online')
  if (online) deviceId.value = online.id
}).catch(() => { /* 列表拉取失败不阻塞面板, 用户仍可手动重试 */ })

function onDeviceChange() {
  if (playing.value) stop()
  latestTracks = null
  crossingFlash = null
  loadGeom()
}

// 读取所选设备的计数线/锚点/ROI 归一化坐标 (叠加层显示用)
function loadGeom() {
  const d = devices.value.find(x => x.id === deviceId.value)
  const norm = (s) => String(s || '').split(',').map(Number).filter(n => Number.isFinite(n))
  const line = norm(d?.line_coords)
  const anchor = norm(d?.anchor_coords)
  const roi = norm(d?.roi_coords)
  geom = {
    line: line.length === 4 ? line : null,   // [x1,y1,x2,y2] 0-1
    anchor: anchor.length === 2 ? anchor : null, // [x,y] 0-1
    roi: roi.length >= 6 ? roi : null,       // 平铺 [x,y,x,y,...] 0-1
  }
}

async function play() {
  error.value = ''
  latestTracks = null
  loadGeom() // 默认选中不触发 change, 播放前刷新一次几何配置
  const id = deviceId.value
  if (!id) return
  try {
    const res = await getDevicePlayUrl(id)
    const { play_url: playUrl, protocol } = res || {}
    if (!playUrl) throw new Error('未获取到播放地址 (设备可能未启流)')
    const video = videoEl.value
    if (protocol === 'flv') {
      const flvjs = (await import('flv.js')).default
      if (!flvjs.isSupported()) throw new Error('当前浏览器不支持 FLV 播放')
      player = flvjs.createPlayer({ type: 'flv', url: playUrl, isLive: true })
      player.attachMediaElement(video)
      player.load()
    } else {
      const Hls = (await import('hls.js')).default
      if (video.canPlayType('application/vnd.apple.mpegurl')) {
        video.src = playUrl // Safari 原生 HLS
      } else if (Hls.isSupported()) {
        player = new Hls()
        player.loadSource(playUrl)
        player.attachMedia(video)
      } else {
        throw new Error('当前浏览器不支持 HLS 播放')
      }
    }
    video.style.display = 'block'
    await video.play()
    playing.value = true
    connectWs()
    ensureLoop()
  } catch (e) {
    error.value = '播放失败: ' + (e?.message || e)
    cleanupPlayer()
  }
}

function stop() {
  playing.value = false
  error.value = ''
  latestTracks = null
  crossingFlash = null
  cleanupPlayer()
  const canvas = canvasEl.value
  if (canvas) canvas.getContext('2d').clearRect(0, 0, canvas.width, canvas.height)
}

function cleanupPlayer() {
  const video = videoEl.value
  if (video) {
    video.style.display = ''
    try { video.pause(); video.removeAttribute('src'); video.load() } catch (e) { /* ignore */ }
  }
  if (player) {
    try { player.destroy?.() } catch (e) { /* ignore */ }
    player = null
  }
  if (ws) { try { ws.close() } catch (e) { /* ignore */ } ws = null }
  if (rafId) { cancelAnimationFrame(rafId); rafId = null }
}

function connectWs() {
  if (ws && ws.readyState <= 1) return
  const proto = location.protocol === 'https:' ? 'wss://' : 'ws://'
  ws = new WebSocket(proto + location.host + '/ws/tracks')
  ws.onmessage = (e) => {
    let m
    try { m = JSON.parse(e.data) } catch { return }
    if (m.type === 'error') { error.value = m.message || '轨迹通道异常'; return }
    if (m.device_id !== deviceId.value) return
    if (m.type === 'tracks') latestTracks = m
    if (m.type === 'crossing_event' && Array.isArray(m.cross_point)) {
      crossingFlash = { at: performance.now(), x: m.cross_point[0], y: m.cross_point[1] }
    }
  }
  ws.onclose = () => { ws = null }
}

function ensureLoop() {
  if (rafId) return
  const loop = () => {
    if (!playing.value) { rafId = null; return }
    draw()
    rafId = requestAnimationFrame(loop)
  }
  rafId = requestAnimationFrame(loop)
}

function draw() {
  const video = videoEl.value
  const canvas = canvasEl.value
  if (!video || !canvas) return
  // 画布对齐视频分辨率: bbox/轨迹坐标为流原始分辨率, 无需换算
  if (video.videoWidth && (canvas.width !== video.videoWidth || canvas.height !== video.videoHeight)) {
    canvas.width = video.videoWidth
    canvas.height = video.videoHeight
  }
  const ctx = canvas.getContext('2d')
  ctx.clearRect(0, 0, canvas.width, canvas.height)
  // 计数线 / 锚点 / ROI (归一化 -> 画布像素)
  if (geom) {
    const W = canvas.width, H = canvas.height
    if (geom.roi) {
      const pts = geom.roi
      ctx.beginPath()
      ctx.moveTo(pts[0] * W, pts[1] * H)
      for (let i = 2; i < pts.length; i += 2) ctx.lineTo(pts[i] * W, pts[i + 1] * H)
      ctx.closePath()
      ctx.fillStyle = 'rgba(0,225,255,0.08)'
      ctx.fill()
      ctx.strokeStyle = 'rgba(0,225,255,0.75)'
      ctx.lineWidth = 2
      ctx.setLineDash([10, 6])
      ctx.stroke()
      ctx.setLineDash([])
    }
    if (geom.line) {
      ctx.strokeStyle = '#00ff66'
      ctx.lineWidth = 3
      ctx.beginPath()
      ctx.moveTo(geom.line[0] * W, geom.line[1] * H)
      ctx.lineTo(geom.line[2] * W, geom.line[3] * H)
      ctx.stroke()
    }
    if (geom.anchor) {
      const ax = geom.anchor[0] * W, ay = geom.anchor[1] * H
      ctx.fillStyle = '#ff3355'
      ctx.beginPath()
      ctx.arc(ax, ay, 7, 0, Math.PI * 2)
      ctx.fill()
      ctx.strokeStyle = 'rgba(255,51,85,0.5)'
      ctx.lineWidth = 2
      ctx.beginPath()
      ctx.arc(ax, ay, 13, 0, Math.PI * 2)
      ctx.stroke()
    }
  }
  if (latestTracks) {
    ctx.lineWidth = 2
    ctx.lineCap = 'round'
    ctx.font = '24px monospace'
    for (const t of (latestTracks.tracks || [])) {
      const color = t.class_name === 'person' ? '#00ff88' : '#ffaa00'
      const [x1, y1, x2, y2] = t.bbox
      ctx.strokeStyle = color
      ctx.strokeRect(x1, y1, x2 - x1, y2 - y1)
      const hist = t.history || []
      if (hist.length > 1) {
        ctx.globalAlpha = 0.85
        ctx.beginPath()
        ctx.moveTo(hist[0][0], hist[0][1])
        for (let i = 1; i < hist.length; i++) ctx.lineTo(hist[i][0], hist[i][1])
        ctx.stroke()
        ctx.globalAlpha = 1
      }
      const ly = Math.max(28, y1 - 10)
      const label = String(t.track_id)
      const lw = ctx.measureText(label).width + 14
      ctx.fillStyle = 'rgba(0,0,0,0.55)'
      ctx.fillRect(x1, ly - 24, lw, 28)
      ctx.fillStyle = color
      ctx.fillText(label, x1 + 7, ly)
    }
  }
  if (crossingFlash) {
    const age = (performance.now() - crossingFlash.at) / 600
    if (age < 1) {
      ctx.strokeStyle = '#ff3355'
      ctx.lineWidth = 4
      ctx.globalAlpha = 1 - age
      ctx.beginPath()
      ctx.arc(crossingFlash.x, crossingFlash.y, 12 + age * 40, 0, Math.PI * 2)
      ctx.stroke()
      ctx.globalAlpha = 1
    } else crossingFlash = null
  }
}

onBeforeUnmount(cleanupPlayer)
</script>

<style scoped>
.vtp {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.vtp-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: 0 0 auto;
}
.vtp-select {
  max-width: 46%;
  padding: 7px 10px;
  border-radius: 8px;
  border: 1px solid rgba(0, 225, 255, 0.3);
  background: rgba(4, 16, 34, 0.9);
  color: #d4e4ff;
  font-size: 13px;
  outline: none;
}
.vtp-btn {
  padding: 7px 16px;
  border-radius: 8px;
  cursor: pointer;
  border: 1px solid rgba(0, 225, 255, 0.35);
  background: rgba(0, 225, 255, 0.12);
  color: #d4e4ff;
  font-size: 13px;
}
.vtp-btn:disabled { opacity: 0.45; cursor: not-allowed; }
.vtp-btn.stop { border-color: rgba(255, 80, 80, 0.5); background: rgba(255, 80, 80, 0.14); color: #ffc9c9; }
.vtp-status { font-size: 12px; color: #7f93b8; }
.vtp-status.on { color: #56e39f; }
.vtp-status.err { color: #ff8484; }

.vtp-stage {
  position: relative;
  flex: 1;
  min-height: 0;
  background: #03080f;
  border: 1px solid rgba(0, 225, 255, 0.25);
  border-radius: 10px;
  overflow: hidden;
  display: flex;
  align-items: center;
  justify-content: center;
}
.vtp-stage video {
  max-width: 100%;
  max-height: 100%;
  display: none; /* 有流时由 JS 切换为 block */
}
.vtp-stage canvas {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
}
.vtp-empty {
  text-align: center;
  color: #7f93b8;
  font-size: 14px;
  line-height: 1.9;
}
.vtp-empty-ic { font-size: 40px; margin-bottom: 6px; }
.vtp-empty-sub { font-size: 12px; color: #5a6d8f; }
</style>
