<template>
  <div class="loading-mask" :class="{ hide: !show }">
    <div class="lm-ring"></div>
    <div class="lm-txt">{{ text }}</div>
  </div>
</template>

<script setup>
defineProps({
  show: { type: Boolean, default: true },
  text: { type: String, default: '数据加载中…' }
})
</script>
