import { defineConfig } from '@playwright/test'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

// Starts a real `c2a studio` on the fake stack (deterministic, CPU only) and drives it.
const workdir = mkdtempSync(join(tmpdir(), 'c2a-e2e-'))
const python = process.env.C2A_PYTHON ?? 'python'
export const TOKEN = 'e2e-token'
export const PORT = 8798

export default defineConfig({
  testDir: './e2e',
  timeout: 240_000,
  expect: { timeout: 90_000 },
  workers: 1,
  reporter: [['list']],
  use: { baseURL: `http://127.0.0.1:${PORT}`, viewport: { width: 1600, height: 1000 }, screenshot: 'only-on-failure' },
  webServer: {
    command: `${python} -m c2a studio --stack fake --set ingest.shot_detector=ffmpeg_scene --workdir ${workdir} --port ${PORT} --no-browser`,
    url: `http://127.0.0.1:${PORT}/`,
    env: { C2A_STUDIO_TOKEN: TOKEN },
    timeout: 60_000,
    reuseExistingServer: false,
    cwd: '..',
  },
})
