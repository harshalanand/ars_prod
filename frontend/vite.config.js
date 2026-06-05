import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') }
  },
  server: {
    port: 3000,
    host: true,
    proxy: {
      '/api': { target: 'http://localhost:8080', changeOrigin: true, proxyTimeout:600000 }
    },
    allowedHosts: ['ars.v2retail.net']
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
})
