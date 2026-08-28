<template>
  <div>
    <!-- 大屏顶栏：标题 / 城市·配色·视角控件 / 主题·规则入口 / 实时时钟 / WS 状态 -->
    <TopBar />

    <!-- 地图 + 设备标点（含添加/编辑弹窗） -->
    <MapPanel />

    <!-- 左右卡片栏（交通指标 / 轨迹 / 监测 / 路口信息 / 分析 / 监控 / 警力 / 告警 / 预测） -->
    <StatBoard />

    <!-- 底部操作提示 -->
    <div class="op-tip">右键拖动旋转 · 滚轮缩放 · 左键平移 · 点「➕ 添加设备」后单击地图落点</div>

    <!-- 全局 Toast 与加载遮罩 -->
    <ToastHost />
    <LoadingOverlay :show="loading" text="智慧交管平台初始化中…" />
  </div>
</template>

<script setup>
import { ref, watch, onMounted, onBeforeUnmount } from 'vue'
import TopBar from './components/TopBar.vue'
import MapPanel from './components/MapPanel.vue'
import StatBoard from './components/StatBoard.vue'
import ToastHost from './components/ToastHost.vue'
import LoadingOverlay from './components/LoadingOverlay.vue'
import { useRealtime } from './composables/useRealtime.js'
import { useTheme } from './composables/useTheme.js'

const rt = useRealtime()
const loading = ref(true)
// 全局主题（深色 / 浅色）—— 驱动 CSS 变量与地图底图样式
const { theme } = useTheme()
let fallback = null

onMounted(() => {
  // 确保根节点带上 data-theme（useTheme 已在模块加载时设置，这里再保险一次）
  document.documentElement.setAttribute('data-theme', theme.value)
  // 启动实时层（WebSocket + 轮询兜底）
  rt.start()
  // 首帧统计到达即收起加载层；最多 3s 兜底
  watch(() => rt.state.stats, (v) => { if (v) loading.value = false }, { immediate: true })
  fallback = setTimeout(() => { loading.value = false }, 3000)
})

onBeforeUnmount(() => {
  rt.stop()
  if (fallback) clearTimeout(fallback)
})
</script>
