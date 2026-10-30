<template>
  <!-- ==================== 视频详情列表（右栏 · 按天统计下方/列底） ====================
       点击大数据卡右上角「视频」后展开：列出该门 4 路设备（北便道人流 / 摄像头A /
       摄像头B / 南便道人流）。每路视频区可点击：有关联设备 → 打开「轨迹标注视频」
       面板实时播放（AI 检测框/轨迹/计数线叠加）；无关联设备保持"暂无视频流"占位。
       卡片位于右栏底部（列内 flex 定位，始终可见，无需滚动），
       故「自动定位」用一次边框/辉光闪烁把视线引过来，切换门时重放。 -->
  <div class="card video-detail" :class="{ 'vd-flash': flash }" v-if="state.gateId">
    <div class="card-hd">
      <span class="vd-title">📹 视频详情 · {{ state.gateName }}</span>
      <span class="vd-close" title="收起" @click="close">✕</span>
    </div>
    <div class="card-bd vd-list">
      <div class="vd-item" v-for="d in state.devices" :key="d.ch">
        <div class="vd-shot" :class="{ 'vd-playable': !!d.id }"
             :title="d.id ? '点击实时播放（AI 轨迹叠加）' : '该路暂无关联设备'"
             @click="d.id && openPlay(d)">
          <i class="vd-scan"></i>
          <span class="vd-ph-ic" v-if="!d.id">📷</span>
          <span class="vd-ph-ic vd-play-ic" v-else>▶</span>
          <span class="vd-ph-tx">{{ d.id ? '点击播放' : '暂无视频流' }}</span>
          <span class="vd-ch">CH{{ d.ch }}</span>
          <span class="vd-live">LIVE</span>
        </div>
        <div class="vd-meta">
          <div class="vd-name">
            <span class="vd-dot" :data-kind="d.kind"></span>
            <span class="vd-name-tx">{{ d.name }}</span>
            <span class="vd-status" :data-level="d.level">{{ d.status }}</span>
          </div>
          <div class="vd-row" v-for="r in d.rows" :key="r[0]"><span>{{ r[0] }}</span><b>{{ r[1] }}</b></div>
          <div class="vd-row"><span>活跃</span><b>{{ d.active }}</b></div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, watch, nextTick } from 'vue'
import { useVideoDetail } from '../composables/useVideoDetail.js'
import { useCardZoom } from '../composables/useCardZoom.js'
import VideoTracePanel from './VideoTracePanel.vue'

const { state, close } = useVideoDetail()
const { openCardZoom } = useCardZoom()

// 点击某一路视频区 → 打开「轨迹标注视频」面板并自动播放该路设备
function openPlay(d) {
  openCardZoom({
    title: '轨迹标注视频 · 实时画面与 AI 轨迹',
    component: VideoTracePanel,
    props: { initialDeviceId: d.id }
  })
}

// 打开 / 切换门时闪烁一次，提示「内容在右栏底部已更新」（见模板注释）
const flash = ref(false)
let flashTimer = null
watch(() => state.gateId, async (id) => {
  if (flashTimer) { clearTimeout(flashTimer); flashTimer = null }
  flash.value = false
  if (!id) return
  // watch 为 pre flush：等首帧渲染出卡片后再加 class，动画才会播放
  await nextTick()
  flash.value = true
  flashTimer = setTimeout(() => { flash.value = false; flashTimer = null }, 1400)
})
</script>

<style scoped>
/* 一次边框高亮 + 外辉光，1.4s 后回到常态（深浅主题自适应） */
.video-detail.vd-flash {
  animation: vdFlash 1.4s ease-out 1;
}

@keyframes vdFlash {
  0% {
    border-color: var(--c-accent);
    box-shadow: 0 0 0 2px var(--c-accent), 0 0 18px var(--c-accent), 0 6px 24px rgba(0, 0, 0, .4);
  }

  100% {
    border-color: var(--c-border);
    box-shadow: 0 0 0 0 transparent, 0 6px 24px rgba(0, 0, 0, .4);
  }
}

/* 有关联设备的视频区可点击播放: 悬停高亮 + ▶ 图标强调 */
.vd-shot.vd-playable {
  cursor: pointer;
  transition: border-color .15s ease, box-shadow .15s ease;
}
.vd-shot.vd-playable:hover {
  border-color: rgba(0, 225, 255, 0.65);
  box-shadow: 0 0 12px rgba(0, 225, 255, 0.22);
}
.vd-shot.vd-playable:hover .vd-play-ic {
  color: #00e1ff;
  text-shadow: 0 0 10px rgba(0, 225, 255, 0.8);
}
.vd-play-ic {
  color: #9fb8d8;
}
</style>
