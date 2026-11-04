import { getAssetUrlsByImport } from '@tldraw/assets/imports.vite'
import { useEffect, useRef, useState } from 'react'
import { createShapeId, type Editor, getSnapshot, Tldraw } from 'tldraw'
import 'tldraw/tldraw.css'
import { api, connect, type Json, media, send, store, token } from './api'
import { useStore } from './panels'
import { LAYOUT, PANEL, PanelShapeUtil } from './shapes'

const assetUrls = getAssetUrlsByImport()
const shapeUtils = [PanelShapeUtil]
const licenseKey =
  document.querySelector<HTMLMetaElement>('meta[name="c2a-tldraw-license"]')?.content || undefined

const STAGES: Record<string, string> = {
  ingesting: 'reading the video',
  finding_keys: 'finding key images',
  tagging: 'tagging key images',
  previewing: 'making style samples',
  awaiting_approval: 'waiting for your style approval',
  restyling: 'restyling key images',
  generating: 'generating clips',
  post: 'finishing the video',
  audio: 'making the sound',
  exporting: 'exporting',
  done: 'done',
  failed: 'failed',
}

function ensurePanels(editor: Editor) {
  editor.run(() => {
    for (const p of LAYOUT) {
      const id = createShapeId(`c2a-${p.kind}`)
      if (!editor.getShape(id)) {
        editor.createShape({ id, type: PANEL, x: p.x, y: p.y, props: { w: p.w, h: p.h, kind: p.kind } })
      }
    }
  })
}

function Doctor({ onClose }: { onClose: () => void }) {
  const [checks, setChecks] = useState<Json[] | null>(null)
  useEffect(() => {
    api.doctor().then((d) => setChecks(d.checks), (e) => setChecks([{ name: 'doctor', ok: false, detail: String(e) }]))
  }, [])
  return (
    <div className="modal" onClick={onClose}>
      <div className="dialog" data-testid="doctor" onClick={(e) => e.stopPropagation()}>
        <h3>Doctor</h3>
        {!checks && <p>checking…</p>}
        {checks?.map((c) => (
          <div key={c.name} className={`check-row ${c.ok ? 'ok' : 'bad'}`}>
            <b>{c.ok ? '✓' : '✗'} {c.name}</b> <span>{c.detail}</span>
            {!c.ok && c.fix && <div className="fix">fix: {c.fix}</div>}
          </div>
        ))}
        <button onClick={onClose}>Close</button>
      </div>
    </div>
  )
}

let currentEditor: Editor | null = null

function goTo(kind: string) {
  const editor = currentEditor
  const shape = editor?.getShape(createShapeId(`c2a-${kind}`))
  if (!editor || !shape) return
  const b = editor.getShapePageBounds(shape)
  if (b) editor.zoomToBounds(b, { animation: { duration: 250 }, inset: 24 })
}

function Toolbar() {
  const { run, view, progress, notices, connected } = useStore()
  const [runs, setRuns] = useState<Json[]>([])
  const [doctor, setDoctor] = useState(false)
  const [uploading, setUploading] = useState(false)
  const file = useRef<HTMLInputElement>(null)
  const refresh = () => api.runs().then((d) => setRuns(d.runs), () => undefined)
  useEffect(() => {
    refresh()
  }, [run, view?.state])
  const open = (id: string) => {
    history.replaceState(null, '', `?run=${id}`)
    connect(id)
  }
  const upload = async (f: File) => {
    setUploading(true)
    try {
      const { run } = await api.upload(f)
      await refresh()
      open(run)
    } catch (e) {
      store.notice('error', `upload failed: ${e}`)
    } finally {
      setUploading(false)
    }
  }
  const state: string = view?.state ?? ''
  return (
    <div className="toolbar" data-testid="toolbar">
      <b className="brand">c2a Studio</b>
      <button onClick={() => setDoctor(true)} data-testid="doctor-button">
        Doctor
      </button>
      <button onClick={() => file.current?.click()} disabled={uploading} data-testid="new-run">
        {uploading ? 'Uploading…' : 'New run ⬆'}
      </button>
      <input
        ref={file}
        type="file"
        accept="video/*"
        hidden
        data-testid="file-input"
        onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])}
      />
      <select value={run ?? ''} onChange={(e) => open(e.target.value)} data-testid="run-picker">
        <option value="" disabled>
          {runs.length ? 'open a run…' : 'no runs yet'}
        </option>
        {runs.map((r) => (
          <option key={r.id} value={r.id}>
            {r.source} · {r.state}
          </option>
        ))}
      </select>
      {run && (
        <>
          <span className="nav">
            {(['keys', 'style', 'samples', 'clips', 'output'] as const).map((k) => (
              <button key={k} data-testid={`nav-${k}`} onClick={() => goTo(k)}>
                {k[0].toUpperCase() + k.slice(1)}
              </button>
            ))}
          </span>
          <button onClick={() => send('generate')} disabled={view?.busy}>
            Run ▶
          </button>
          <button onClick={() => send('cancel')} disabled={!view?.busy}>
            Cancel
          </button>
          {view?.output && (
            <a className="button" href={media(run, view.output.video)} target="_blank" rel="noreferrer">
              Open output
            </a>
          )}
        </>
      )}
      <span className="status" data-testid="status">
        {!token && 'no token: open the URL printed by c2a studio'}
        {run && !connected && 'connecting…'}
        {state && (STAGES[state] ?? state)}
        {view?.busy && progress && progress.pct > 0 && progress.pct < 100 && ` ${progress.pct.toFixed(0)}%`}
        {state === 'failed' && view?.note && `: ${view.note}`}
      </span>
      <div className="notices">
        {notices.slice(-3).map((n) => (
          <div key={n.t} className={`notice ${n.level}`}>
            {n.text}
          </div>
        ))}
      </div>
      {doctor && <Doctor onClose={() => setDoctor(false)} />}
    </div>
  )
}

export default function App() {
  const { run } = useStore()
  const editorRef = useRef<Editor | null>(null)
  useEffect(() => {
    const id = new URLSearchParams(location.search).get('run')
    if (id) connect(id)
  }, [])
  useEffect(() => {
    const editor = editorRef.current
    if (!editor || !run) return
    let timer: ReturnType<typeof setTimeout> | undefined
    const stopListen = editor.store.listen(
      () => {
        clearTimeout(timer)
        timer = setTimeout(() => api.saveBoard(run, getSnapshot(editor.store)).catch(() => undefined), 3000)
      },
      { scope: 'document', source: 'user' },
    )
    return () => {
      clearTimeout(timer)
      stopListen()
    }
  }, [run])
  return (
    <div
      className="studio"
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => {
        const f = e.dataTransfer.files?.[0]
        if (f && f.type.startsWith('video/')) {
          e.preventDefault()
          e.stopPropagation()
          ;(document.querySelector('[data-testid="file-input"]') as HTMLInputElement | null)?.dispatchEvent(new Event('noop'))
          api.upload(f).then(({ run }) => {
            history.replaceState(null, '', `?run=${run}`)
            connect(run)
          }, (err) => store.notice('error', `upload failed: ${err}`))
        }
      }}
    >
      <Toolbar />
      <div className="canvas">
        <Tldraw
          key={run ?? 'none'}
          persistenceKey={run ? `c2a-${run}` : undefined}
          shapeUtils={shapeUtils}
          assetUrls={assetUrls}
          licenseKey={licenseKey}
          onMount={(editor) => {
            editorRef.current = editor
            currentEditor = editor
            ensurePanels(editor)
            // open on the decisions: key images, style and samples, at a readable zoom
            editor.zoomToBounds({ x: -20, y: -20, w: 1500, h: 1080 }, { animation: { duration: 0 }, inset: 0 })
          }}
        />
      </div>
    </div>
  )
}
