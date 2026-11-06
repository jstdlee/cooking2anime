import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// BENCH_ASSETS: folder with t0.png… and clip.mp4 (copied into the build).
export default defineConfig({
  plugins: [react()],
  publicDir: process.env.BENCH_ASSETS,
  build: { chunkSizeWarningLimit: 4000 },
})
