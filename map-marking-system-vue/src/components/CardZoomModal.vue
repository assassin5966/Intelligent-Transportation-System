<template>
  <Teleport to="body">
    <transition name="zoom-fade">
      <div v-if="zoomState" class="card-zoom-mask" @dblclick.self="close" @click.self="close">
        <div class="card-zoom-modal">
          <div class="czm-hd">
            <span class="czm-title">{{ zoomState.title }}</span>
            <div class="czm-actions">
              <span class="czm-hint">双击遮罩 / 按 Esc 关闭</span>
              <button class="czm-close" type="button" @click="close" title="关闭">✕</button>
            </div>
          </div>
          <div class="zoom-body">
            <component :is="zoomState.component" v-bind="zoomState.props" />
          </div>
        </div>
      </div>
    </transition>
  </Teleport>
</template>

<script setup>
import { onMounted, onBeforeUnmount } from 'vue'
import { useCardZoom } from '../composables/useCardZoom.js'

const { zoomState, closeCardZoom } = useCardZoom()

function close() {
  closeCardZoom()
}
function onKey(e) {
  if (e.key === 'Escape') close()
}
onMounted(() => window.addEventListener('keydown', onKey))
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))
</script>

<style scoped>
.card-zoom-mask {
  position: fixed;
  inset: 0;
  z-index: 9999;
  background: rgba(4, 10, 22, 0.82);
  backdrop-filter: blur(2px);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 3vh 3vw;
}
.card-zoom-modal {
  width: 92vw;
  height: 90vh;
  display: flex;
  flex-direction: column;
  background: linear-gradient(160deg, rgba(10, 22, 44, 0.96), rgba(6, 14, 30, 0.98));
  border: 1px solid rgba(0, 225, 255, 0.35);
  border-radius: 12px;
  box-shadow:
    0 0 0 1px rgba(0, 225, 255, 0.08),
    0 18px 60px rgba(0, 0, 0, 0.6),
    inset 0 0 40px rgba(0, 225, 255, 0.05);
  overflow: hidden;
}
.czm-hd {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 16px;
  border-bottom: 1px solid rgba(0, 225, 255, 0.18);
  background: rgba(0, 225, 255, 0.06);
  flex: 0 0 auto;
}
.czm-title {
  font-size: 16px;
  font-weight: 700;
  color: #d4e4ff;
  letter-spacing: 1px;
}
.czm-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}
.czm-hint {
  font-size: 11px;
  color: #7f93b8;
}
.czm-close {
  width: 30px;
  height: 30px;
  border-radius: 6px;
  cursor: pointer;
  border: 1px solid rgba(0, 225, 255, 0.3);
  background: rgba(0, 225, 255, 0.08);
  color: #d4e4ff;
  font-size: 15px;
  line-height: 1;
}
.czm-close:hover {
  background: rgba(0, 225, 255, 0.2);
}

/* 放大副本需要撑满遮罩主体：让卡片根与图表容器 flex 展开 */
.zoom-body {
  flex: 1;
  min-height: 0;
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
}
.zoom-body > :deep(.card),
.zoom-body > :deep(.chart-card) {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.zoom-body :deep(.card-bd) {
  flex: 1;
  min-height: 0;
  overflow: auto;
}
.zoom-body :deep(.chart-box) {
  flex: 1;
  min-height: 240px;
}

.zoom-fade-enter-active,
.zoom-fade-leave-active {
  transition: opacity 0.18s ease;
}
.zoom-fade-enter-from,
.zoom-fade-leave-to {
  opacity: 0;
}
</style>

<!-- 全局：可放大卡片的统一交互提示（不 scoped，作用于任意卡片根） -->
<style>
.zoomable {
  cursor: zoom-in;
  position: relative;
}
.zoomable::after {
  content: '⛶';
  position: absolute;
  top: 6px;
  right: 8px;
  font-size: 13px;
  line-height: 1;
  color: rgba(0, 225, 255, 0.55);
  opacity: 0;
  transition: opacity 0.15s ease;
  pointer-events: none;
  z-index: 5;
}
.zoomable:hover::after {
  opacity: 1;
}
</style>
