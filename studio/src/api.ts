// Studio ↔ engine client: token auth, REST, WebSocket with reconnect, a tiny view store.

export type Json = Record<string, any>

const params = new URLSearchParams(location.search)
const urlToken = params.get('token')
if (urlToken) sessionStorage.setItem('c2a_token', urlToken)
export const token = urlToken ?? sessionStorage.getItem('c2a_token') ?? ''

export function media(run: string, rel: string | null | undefined): string | undefined {
  return rel ? `/media/${run}/${rel}?token=${encodeURIComponent(token)}` : undefined
}

async function req(path: string, init: RequestInit = {}): Promise<any> {
  const res = await fetch(path, {
    ...init,
    headers: { 'X-C2A-Token': token, ...(init.headers ?? {}) },
  })
  if (!res.ok) {
    let msg = `${res.status}`
    try {
      msg = (await res.json()).detail ?? msg
    } catch {
      /* not json */
    }
    throw new Error(msg)
  }
  return res.json()
}

export const api = {
  runs: () => req('/api/runs'),
  doctor: () => req('/api/doctor'),
  view: (run: string) => req(`/api/runs/${run}`),
  upload: (file: File) => {
    const fd = new FormData()
    fd.append('video', file)
    return req('/api/runs', { method: 'POST', body: fd })
  },
  saveBoard: (run: string, snapshot: unknown) =>
    req(`/api/runs/${run}/board`, {
      method: 'PUT',
      body: JSON.stringify(snapshot),
      headers: { 'Content-Type': 'application/json' },
    }),
}

// ------------------------------------------------------------------ store
type Listener = () => void

export interface Progress {
  stage: string
  unit: string
  pct: number
}

export interface StoreState {
  run: string | null
  view: Json | null
  progress: Progress | null
  notices: { level: string; text: string; t: number }[]
  connected: boolean
}

let state: StoreState = { run: null, view: null, progress: null, notices: [], connected: false }
const listeners = new Set<Listener>()

export const store = {
  get: () => state,
  set(patch: Partial<StoreState>) {
    state = { ...state, ...patch }
    listeners.forEach((l) => l())
  },
  subscribe(l: Listener) {
    listeners.add(l)
    return () => listeners.delete(l)
  },
  notice(level: string, text: string) {
    store.set({ notices: [...state.notices.slice(-4), { level, text, t: Date.now() }] })
  },
}

// ------------------------------------------------------------------ socket
let sock: WebSocket | null = null
let retry = 0
let opSeq = 0

export function connect(run: string) {
  sock?.close()
  store.set({ run, view: null, progress: null, connected: false })
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  const ws = new WebSocket(`${proto}://${location.host}/api/runs/${run}/ws?token=${encodeURIComponent(token)}`)
  sock = ws
  ws.onopen = () => {
    retry = 0
    store.set({ connected: true })
  }
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data)
    if (msg.type === 'view') store.set({ view: msg.view, progress: msg.view.busy ? state.progress : null })
    else if (msg.type === 'progress') store.set({ progress: msg })
    else if (msg.type === 'notice') store.notice(msg.level, msg.text)
  }
  ws.onclose = () => {
    if (sock !== ws) return
    store.set({ connected: false })
    retry = Math.min(retry + 1, 6)
    setTimeout(() => sock === ws && connect(run), 300 * 2 ** retry)
  }
}

export function send(op: string, args: Json = {}) {
  const op_id = `${Date.now()}-${++opSeq}`
  if (!sock || sock.readyState !== WebSocket.OPEN) {
    store.notice('error', 'not connected to the engine')
    return
  }
  sock.send(JSON.stringify({ op, args, op_id }))
}
