// usage: node bench-web.cjs <n> → the tldraw JSON report, plus Chromium CPU per phase and peak RSS
const { chromium } = require('playwright')
const fs = require('fs')
const PHASES = ['idle', 'pan', 'zoom', 'drag', 'duplicate'], PHASE_MS = 4000
const parse = path => {
  const stat = fs.readFileSync(path, 'utf8')
  const f = stat.slice(stat.lastIndexOf(')') + 2).split(' ')
  return { comm: stat.slice(stat.indexOf('(') + 1, stat.lastIndexOf(')')), cpu: (+f[11] + +f[12]) / 100, rss: (+f[21]) * 4096 / 2 ** 20 }
}
// main = renderer main thread (JS, React, DOM, style, layout, paint recording);
// raster = GPU process, compositor and tile raster workers (SwiftShader/Skia here)
const group = t => /^CrRendererMain/.test(t) ? 'main'
  : /^(CompositorTileW|CrGpuMain|VizCompositorTh|Compositor|Chrome_InProcGp|GpuMemory|SkiaGraphite|DrmThread)/.test(t) ? 'raster'
  : /^(Media|VideoFrame|AudioOutput|ThreadPoolForeg|ThreadPoolSingl)/.test(t) ? 'media' : 'other'
function chromeTotals() { // [cpu seconds, rss MB, per-group cpu] over every Chromium process
  let cpu = 0, rss = 0
  const g = { main: 0, raster: 0, media: 0, other: 0 }
  for (const pid of fs.readdirSync('/proc').filter(d => /^\d+$/.test(d))) {
    try {
      const p = parse(`/proc/${pid}/stat`)
      if (!/chrom|headless/.test(p.comm)) continue
      cpu += p.cpu; rss += p.rss
      const kind = (fs.readFileSync(`/proc/${pid}/cmdline`, 'utf8').match(/--type=([a-z-]+)/) || [, 'browser'])[1]
      for (const tid of fs.readdirSync(`/proc/${pid}/task`)) {
        try {
          const t = parse(`/proc/${pid}/task/${tid}/stat`)
          // the renderer's main thread has tid == pid; the GPU process is all raster here
          g[kind === 'renderer' && tid === pid ? 'main' : kind === 'gpu-process' ? 'raster' : group(t.comm)] += t.cpu
        } catch {}
      }
    } catch {}
  }
  return [cpu, rss, g]
}
;(async () => {
  const n = process.argv[2] || 1000
  const browser = await chromium.launch({ headless: false, args: ['--autoplay-policy=no-user-gesture-required', '--window-position=0,0', '--enable-precise-memory-info'] })
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } })
  const samples = []
  const poll = setInterval(() => samples.push([Date.now(), ...chromeTotals()]), 100)
  await page.goto(`http://127.0.0.1:4179/?n=${n}`)
  await page.waitForFunction(() => window.__bench, null, { timeout: 180000, polling: 500 })
  clearInterval(poll)
  const r = await page.evaluate(() => ({ ...window.__bench, phase0: window.__benchPhase0 }))
  const at = t => samples.reduce((a, b) => (Math.abs(b[0] - t) < Math.abs(a[0] - t) ? b : a))
  PHASES.forEach((name, i) => {
    const a = at(r.phase0 + i * PHASE_MS), b = at(r.phase0 + (i + 1) * PHASE_MS), s = PHASE_MS / 1000, ph = r.phases[name]
    ph.cpu_cores = +((b[1] - a[1]) / s).toFixed(2)
    for (const k of ['main', 'raster', 'media', 'other']) ph[k + '_cores'] = +((b[3][k] - a[3][k]) / s).toFixed(2)
    ph.main_ms_per_frame = +(ph.main_cores * PHASE_MS / Math.max(1, ph.fps * s)).toFixed(1)
  })
  r.rss_mb = Math.round(Math.max(...samples.map(s => s[2])))
  delete r.phase0
  console.log(JSON.stringify(r))
  await browser.close()
})()
