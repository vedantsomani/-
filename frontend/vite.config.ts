import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

const API = process.env.VITE_API_TARGET ?? 'http://127.0.0.1:8000'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  // Aladin Lite (lazy-loaded) is one ~2.4 MB chunk with embedded WASM.
  build: { chunkSizeWarningLimit: 2600 },
  server: {
    port: 5173,
    proxy: { '/api': { target: API, changeOrigin: true } },
  },
})
