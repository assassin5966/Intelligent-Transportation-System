<template>
  <!-- ==================== 视频详情列表（左栏 · 警力分配下方） ====================
       点击大数据卡右上角「详情」后展开：列出该门 4 路设备（北便道人流 / 摄像头A /
       摄像头B / 南便道人流），每路带视频截图占位区（无真实码流，与全局"暂无视频流"口径一致） -->
  <div class="card video-detail" v-if="state.gateId">
    <div class="card-hd">
      <span class="vd-title">📹 视频详情 · {{ state.gateName }}</span>
      <span class="vd-close" title="收起" @click="close">✕</span>
    </div>
    <div class="card-bd vd-list">
      <div class="vd-item" v-for="d in state.devices" :key="d.ch">
        <div class="vd-shot">
          <i class="vd-scan"></i>
          <span class="vd-ph-ic">📷</span>
          <span class="vd-ph-tx">暂无视频流</span>
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
import { useVideoDetail } from '../composables/useVideoDetail.js'

const { state, close } = useVideoDetail()
</script>
