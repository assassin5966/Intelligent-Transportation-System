<template>
  <!-- 道路断面图例（全局唯一）：原 8 张大数据卡底部的脚注图例集中到此，
       挂在左侧栏 · 警力分配卡下方；卡片外观复用 .chart-card 规范 -->
  <div class="chart-card flow-legend">
    <div class="chart-toolbar">
      <span class="chart-title">🗺 断面图例</span>
      <span class="legend-sub">大数据卡 · 图例说明</span>
    </div>
    <div class="legend-body">
      <span class="rp-fi"><i class="rp-fi-box"></i>绿框 ：车流量卡</span>
      <span class="rp-fi"><i class="rp-fi-box rp-p"></i>蓝框 ：人流量卡</span>
      <span class="rp-fi"><i class="rp-fi-solid"></i>实线 ×2 ：车行道边缘线</span>
      <span class="rp-fi"><i class="rp-fi-dash"></i>虚线 ×3 ：车道分隔线</span>
      <span class="rp-fi"><i class="rp-fi-flow"></i>箭头 ×4 ：车辆行驶方向</span>
      <span class="rp-fi"><i class="rp-fi-cam"></i>摄像头 ×2 ：各覆盖 2 条车道</span>
      <span class="rp-fi"><i class="rp-fi-sw"></i>斜纹条 ×2 ：双侧便道</span>
    </div>
  </div>
</template>

<script setup>
// 纯静态图例面板，无数据依赖；各图例图标（.rp-fi-*）的绘制样式见 main.css
// —— 与卡内同一套颜色令牌（--c-flow-* / --c-rp-*），外观永远一致
</script>
