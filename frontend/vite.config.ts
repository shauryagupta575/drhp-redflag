import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    // Forward API calls to the FastAPI backend during local development.
    proxy: {
      '/health': process.env.VITE_API_PROXY_TARGET ?? 'http://localhost:8000',
    },
  },
})
