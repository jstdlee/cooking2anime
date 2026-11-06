// Canvas stress benchmark (tldraw side): the same script as src/bin/canvas_bench.rs.
//   /?n=5000  → window.__bench holds the JSON report when done.
import { getAssetUrlsByImport } from '@tldraw/assets/imports.vite'
import { createRoot } from 'react-dom/client'
import { AssetRecordType, Tldraw, createShapeId } from 'tldraw'
import 'tldraw/tldraw.css'

const q = new URLSearchParams(location.search)
const N = Number(q.get('n') || 1000)
const IMGS = 500
const CELL = [240, 144], CARD = [220, 124]
const WARMUP = 3, PHASE_S = 4, PHASES = ['idle', 'pan', 'zoom', 'drag', 'duplicate']
const TAU = Math.PI * 2

function camera(t) {
  const p = Math.floor(t / PHASE_S), u = (t % PHASE_S) / PHASE_S
  if (p === 1) return [640 + 1500 * Math.sin(TAU * u), 400 + 300 * (1 - Math.cos(TAU * u)), 1]
  if (p === 2) return [640, 400, Math.exp(Math.log(0.08) * 0.5 * (1 - Math.cos(TAU * u)))]
  if (p === 3 || p === 4) return [1600, 1000, 0.4]
  return [640, 400, 1]
}
function dragOffset(t) {
  if (Math.floor(t / PHASE_S) !== 3) return [0, 0]
  const u = (t % PHASE_S) / 2
  return [300 * Math.sin(TAU * u), 150 * Math.sin(TAU * u)]
}

function onMount(editor) {
  const t0 = performance.now()
  editor.updateInstanceState({ isGridMode: false })
  const assets = Array.from({ length: IMGS }, (_, i) => ({
    id: AssetRecordType.createId('t' + i), typeName: 'asset', type: 'image', meta: {},
    props: { name: `t${i}.png`, src: `/t${i}.png`, w: 384, h: 216, mimeType: 'image/png', isAnimated: false },
  }))
  editor.createAssets(assets)
  const cols = Math.ceil(Math.sqrt(N) * 1.5)
  const base = []
  const shapes = Array.from({ length: N }, (_, i) => {
    const x = (i % cols) * CELL[0], y = Math.floor(i / cols) * CELL[1]
    base.push([x, y])
    return { id: createShapeId('c' + i), type: 'image', x, y, props: { assetId: assets[i % IMGS].id, w: CARD[0], h: CARD[1] } }
  })
  editor.run(() => editor.createShapes(shapes))
  const sel = []
  for (let r = 0; r < 10; r++) for (let c = 0; c < 20; c++) if (r * cols + c < N) sel.push(r * cols + c)
  const selIds = sel.map(i => shapes[i].id)
  editor.select(...selIds)
  const loadMs = performance.now() - t0

  const frames = PHASES.map(() => [])
  const start = performance.now()
  window.__benchPhase0 = performance.timeOrigin + start + WARMUP * 1000 // epoch ms of phase 0
  let last = null, dups = 0, done = false
  const tick = now => {
    const t = (now - start) / 1000 - WARMUP
    if (last !== null && t >= 0 && t / PHASE_S < PHASES.length) frames[Math.floor(t / PHASE_S)].push(now - last)
    last = now
    if (t >= PHASE_S * PHASES.length) {
      if (!done) { done = true; report(frames, loadMs, editor) }
      return
    }
    const tt = Math.max(0, t)
    if (Math.floor(tt / PHASE_S) === 4) {
      const due = Math.min(8, Math.floor((tt - 4 * PHASE_S) / 0.5) + 1)
      while (dups < due) { dups++; editor.duplicateShapes(selIds, { x: 20 * dups, y: 20 * dups }); editor.select(...selIds) }
    }
    const [dx, dy] = dragOffset(tt)
    if (Math.floor(tt / PHASE_S) === 3) {
      editor.updateShapes(sel.map(i => ({ id: shapes[i].id, type: 'image', x: base[i][0] + dx, y: base[i][1] + dy })))
    }
    const [cx, cy, z] = camera(tt)
    const vb = editor.getViewportScreenBounds()
    editor.setCamera({ x: vb.w / (2 * z) - cx, y: vb.h / (2 * z) - cy, z }, { force: true })
    requestAnimationFrame(tick)
  }
  requestAnimationFrame(tick)
}

function report(frames, loadMs, editor) {
  const phases = {}
  PHASES.forEach((name, i) => {
    const v = [...frames[i]].sort((a, b) => a - b)
    const qq = p => +(v[Math.round((v.length - 1) * p)] || 0).toFixed(1)
    phases[name] = { fps: +(v.length / PHASE_S).toFixed(1), p50: qq(0.5), p95: qq(0.95), p99: qq(0.99), max: qq(1), jank: v.filter(x => x > 33.4).length }
  })
  const vq = document.querySelector('#bench-video').getVideoPlaybackQuality()
  window.__bench = {
    stack: 'tldraw', cards_end: editor.getCurrentPageShapeIds().size, load_ms: Math.round(loadMs),
    heap_mb: performance.memory ? Math.round(performance.memory.usedJSHeapSize / 2 ** 20) : null,
    video: { shown: vq.totalVideoFrames - vq.droppedVideoFrames, dropped: vq.droppedVideoFrames }, phases,
  }
}

createRoot(document.getElementById('root')).render(
  <div style={{ position: 'fixed', inset: 0 }}>
    <Tldraw onMount={onMount} assetUrls={getAssetUrlsByImport()} hideUi options={{ maxShapesPerPage: 50000 }} />
    <video id="bench-video" src="/clip.mp4" autoPlay muted playsInline
      style={{ position: 'fixed', top: 8, right: 8, width: 480, height: 270, background: '#000', zIndex: 1000 }} />
  </div>,
)
