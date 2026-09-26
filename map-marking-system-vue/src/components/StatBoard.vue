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

    <!-- ③ 视频详情：点击大数据卡右上角「视频」后展示该门 4 路设备视频信息，置右栏底部 -->
    <VideoDetailPanel
      class="zoomable"
      @dblclick="openCardZoom({ title: '视频详情', component: VideoDetailPanel })" />
  </div>
</template>

<script setup>
import PolicePanel from './PolicePanel.vue'
import PredictionPanel from './PredictionPanel.vue'
import DailyTrendChart from './DailyTrendChart.vue'
import TrafficMetricsCard from './TrafficMetricsCard.vue'
import FlowLegendPanel from './FlowLegendPanel.vue'
import VideoDetailPanel from './VideoDetailPanel.vue'
import { useCardZoom } from '../composables/useCardZoom.js'

const { openCardZoom } = useCardZoom()
</script>
