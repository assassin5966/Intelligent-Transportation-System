import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Vite 配置：纯前端 SPA，地图 SDK（高德）通过 index.html 的 <script> 以全局变量 AMap 注入。
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    host: true,
    open: true
  },
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 1500
  }
})
