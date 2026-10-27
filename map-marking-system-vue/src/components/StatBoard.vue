<template>
  <!-- ==================== 左侧卡片栏 ==================== -->
  <div class="side-col side-left">
    <!-- ① 全局信息：交通通行指标（全部监控设备合计，来自实时统计 §4.1） -->
    <TrafficMetricsCard
      class="zoomable"
      @dblclick="openCardZoom({ title: '交通通行指标 · 全部监控设备合计', component: TrafficMetricsCard })" />

    <!-- ② 警力分配 -->
    <PolicePanel
      class="zoomable"
      @dblclick="openCardZoom({ title: '警力分配', component: PolicePanel })" />

    <!-- ③ 断面图例（全局唯一）：原 8 张大数据卡底部脚注集中于此，紧跟警力分配 -->
    <FlowLegendPanel
      class="zoomable"
      @dblclick="openCardZoom({ title: '断面图例', component: FlowLegendPanel })" />
  </div>

  <!-- ==================== 右侧卡片栏 ==================== -->
  <div class="side-col side-right">
    <!-- ① 时序预测 -->
    <PredictionPanel
      class="zoomable"
      @dblclick="openCardZoom({ title: '时序预测', component: PredictionPanel })" />

    <!-- ② 按天人数/车流统计折线图 -->
    <DailyTrendChart
      class="zoomable"
      @dblclick="openCardZoom({ title: '按天人数/车流统计', component: DailyTrendChart })" />

    <!-- ③ 视频详情：点击大数据卡右上角「视频」后展示该门 4 路设备视频信息，置右栏底部；
         双击放大打开「轨迹标注视频」面板（实时播放 + AI 检测框/轨迹叠加） -->
    <VideoDetailPanel
      class="zoomable"
      @dblclick="openCardZoom({ title: '轨迹标注视频 · 实时画面与 AI 轨迹', component: VideoTracePanel })" />

    <!-- ④ 轨迹标注视频常驻入口（不依赖门选中态, 始终可见） -->
    <div class="video-entry" @click="openCardZoom({ title: '轨迹标注视频 · 实时画面与 AI 轨迹', component: VideoTracePanel })">
      <span class="ve-ic">📹</span>
      <div class="ve-tx">
        <b>轨迹标注视频</b>
        <i>点击打开 · 实时画面 + AI 检测框/轨迹叠加</i>
      </div>
      <span class="ve-go">▶</span>
    </div>
  </div>
</template>

<script setup>
import PolicePanel from './PolicePanel.vue'
import PredictionPanel from './PredictionPanel.vue'
import DailyTrendChart from './DailyTrendChart.vue'
import TrafficMetricsCard from './TrafficMetricsCard.vue'
import FlowLegendPanel from './FlowLegendPanel.vue'
import VideoDetailPanel from './VideoDetailPanel.vue'
import VideoTracePanel from './VideoTracePanel.vue'
import { useCardZoom } from '../composables/useCardZoom.js'

const { openCardZoom } = useCardZoom()
</script>

<style scoped>
/* 轨迹标注视频常驻入口卡：对齐大屏卡片视觉（深蓝底 + 青色描边） */
.video-entry {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  pointer-events: auto; /* .side-col 整列 pointer-events:none 穿透地图, 非卡片类子元素需显式恢复交互 */
  border: 1px solid rgba(0, 225, 255, 0.3);
  border-radius: 10px;
  background: linear-gradient(160deg, rgba(10, 22, 44, 0.92), rgba(6, 14, 30, 0.96));
  cursor: pointer;
  user-select: none;
  transition: border-color .15s ease, box-shadow .15s ease;
}
.video-entry:hover {
  border-color: rgba(0, 225, 255, 0.6);
  box-shadow: 0 0 14px rgba(0, 225, 255, 0.18);
}
.ve-ic { font-size: 22px; }
.ve-tx { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.ve-tx b { font-size: 13px; color: #d4e4ff; letter-spacing: .5px; }
.ve-tx i { font-style: normal; font-size: 11px; color: #7f93b8; }
.ve-go { font-size: 12px; color: #56e39f; }
</style>
