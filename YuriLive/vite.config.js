import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5174,
    host: true,   // 局域网可访问, 手机直接连
    proxy: {
      '/api': 'http://127.0.0.1:8003',
      '/media': 'http://127.0.0.1:8003',
      '/ws': { target: 'ws://127.0.0.1:8003', ws: true },
    },
  },
})
