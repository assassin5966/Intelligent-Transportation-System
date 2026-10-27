import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Vite 配置：纯前端 SPA，地图 SDK（高德）通过 index.html 的 <script> 以全局变量 AMap 注入。
//
// 开发代理：前端 API_BASE 默认同源（location.origin），/api、/ws、/static 统一代理到本地后端 8000。
// 好处：localhost 与局域网 IP（192.168.x.x:5173）访问均可正常工作，无需 VITE_API_BASE。
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    host: true,
    open: true,
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true },
      '/static': { target: 'http://localhost:8000', changeOrigin: true },
      '/ws': { target: 'ws://localhost:8000', ws: true, changeOrigin: true }
    }
  },
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 1500
  }
})
