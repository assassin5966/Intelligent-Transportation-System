<template>
  <div>
    <div class="video-box" :class="{ 'has-video': hasVideo }">
      <video ref="video" autoplay muted loop playsinline></video>
      <div class="video-ph">📷<br>监控视频接入位</div>
    </div>
    <div class="video-note" v-if="focus">
      当前设备：<code>{{ focus.name }}</code><br />
      流地址：<code>{{ focus.stream_url }}</code><br />
      将转码后的 HLS/FLV 地址传入 <code>connectRoadVideo(url, type)</code> 即可显示实时画面。
      <div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">
        <button class="tool-btn" @click="playDevice">▶ 播放实时流</button>
        <button class="tool-btn" @click="refreshStream">🔄 刷新流地址</button>
        <button class="tool-btn" @click="connectSample">🎬 示例流</button>
      </div>
    </div>
    <div class="video-note" v-else>请在大图上选择一个设备以查看其视频流。</div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { useDevices } from '../composables/useDevices.js'
import { useToast } from '../composables/useToast.js'
import { getDeviceStream, getDevicePlayUrl } from '../api/endpoints.js'

const { state: dev } = useDevices()
const { push } = useToast()
const video = ref(null)
const hasVideo = ref(false)

const focus = computed(() => {
  const list = dev.devices
  if (!list.length) return null
  return list.find((d) => d.id === dev.selectedId) || list[0]
})

// §3.5 刷新流地址（供 AI 拉流用）
async function refreshStream() {
  if (!focus.value) return
  try {
    const r = await getDeviceStream(focus.value.id)
    push('流地址已刷新：' + (r.stream_url || ''), 'success')
  } catch (e) {
    push('刷新流地址失败：' + (e.detail || e.message), 'warn')
  }
}

// §3.6 前端播放地址：获取浏览器可直放地址并接入播放器（flv→flv.js / hls→hls.js）
async function playDevice() {
  if (!focus.value) return
  try {
    const r = await getDevicePlayUrl(focus.value.id)
    if (!r || !r.play_url) {
      push('该设备暂无可播放的流地址', 'warn')
      return
    }
    connectRoadVideo(r.play_url, r.protocol)
    push(`已接入 ${r.protocol.toUpperCase()} 流：${r.play_url}`, 'success', 3000)
  } catch (e) {
    push('获取播放地址失败：' + (e.detail || e.message), 'warn')
  }
}

// 演示：播放一段公开示例流（浏览器原生支持的 mp4）
function connectSample() {
  const v = video.value
  if (!v) return
  hasVideo.value = true
  v.src = 'https://www.w3schools.com/html/mov_bbb.mp4'
  v.play().catch(() => {})
}

// 通用接入点（HLS 需 hls.js，FLV 需 flv.js）
function connectRoadVideo(url, type) {
  const v = video.value
  if (!v) return
  hasVideo.value = true
  try {
    if (type === 'hls' && window.Hls && window.Hls.isSupported()) {
      const hls = new window.Hls(); hls.loadSource(url); hls.attachMedia(v)
    } else if (type === 'flv' && window.flvjs) {
      const flv = window.flvjs.createPlayer({ type: 'flv', url }); flv.attachMediaElement(v); flv.load(); flv.play()
    } else {
      v.src = url
    }
    v.play().catch(() => {})
  } catch (e) {
    console.error('视频接入失败', e)
  }
}
</script>
