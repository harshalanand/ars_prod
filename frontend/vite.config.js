import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

export default defineConfig(({ mode }) => {
  // Load VITE_* vars from .env / .env.local / .env.[mode] in the frontend dir.
  const env = loadEnv(mode, process.cwd())

  const proxyTarget = env.VITE_PROXY_TARGET || 'http://localhost:8000'
  const allowedHosts = (env.VITE_ALLOWED_HOSTS || 'ars2.v2retail.net')
    .split(',')
    .map(h => h.trim())
    .filter(Boolean)
  const port = Number(env.VITE_DEV_PORT) || 3000

  return {
    plugins: [react()],
    resolve: {
      alias: { '@': path.resolve(__dirname, './src') }
    },
    server: {
      port,
      host: true,
      proxy: {
        '/api': { target: proxyTarget, changeOrigin: true }
      },
      allowedHosts,
    },
    build: {
      target: 'es2020',
      chunkSizeWarningLimit: 600,
      rollupOptions: {
        output: {
          manualChunks: {
            'vendor-react': ['react', 'react-dom', 'react-router-dom'],
            'vendor-grid': ['ag-grid-community', 'ag-grid-react'],
            'vendor-utils': ['axios', 'zustand', 'react-hot-toast', 'date-fns'],
            'vendor-charts': ['recharts'],
          },
        },
      },
    },
    optimizeDeps: {
      include: ['react', 'react-dom', 'axios', 'zustand'],
    },
  }
})
