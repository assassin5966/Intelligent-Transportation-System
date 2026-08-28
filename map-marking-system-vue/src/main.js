import { createApp } from 'vue'
import App from './App.vue'
import './styles/main.css'

// 应用入口：挂载根组件 App。
// 全局样式(视觉/布局)全部在 styles/main.css 中，严格沿用原 map-marking-system.html 的设计，
// 业务后端地址 / Token / 超时 / Mock 开关见 src/api/config.js。
createApp(App).mount('#app')
