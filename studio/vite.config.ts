import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Built into the Python package: c2a/studio/static (served by `c2a studio`).
export default defineConfig({
  plugins: [react()],
  base: '/',
  build: { outDir: '../c2a/studio/static', emptyOutDir: true, chunkSizeWarningLimit: 4000 },
  server: { proxy: { '/api': { target: 'http://127.0.0.1:8765', ws: true }, '/media': 'http://127.0.0.1:8765' } },
})
