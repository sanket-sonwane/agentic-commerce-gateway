import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api/merchant': { target: 'http://localhost:8000', changeOrigin: true, rewrite: p => p.replace(/^\/api\/merchant/, '') },
      '/api/buyer': { target: 'http://localhost:8001', changeOrigin: true, rewrite: p => p.replace(/^\/api\/buyer/, '') },
    },
  },
})
