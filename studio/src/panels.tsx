// The five canvas panels. Each reads the live run view and sends commands to the engine.
import { useState, useSyncExternalStore } from 'react'
import { type Json, media, send, store } from './api'

export function useStore() {
  return useSyncExternalStore(store.subscribe, store.get)
}

const stop = {
  onPointerDown: (e: React.PointerEvent) => e.stopPropagation(),
  onPointerUp: (e: React.PointerEvent) => e.stopPropagation(),
  onWheel: (e: React.WheelEvent) => e.stopPropagation(),
  onKeyDown: (e: React.KeyboardEvent) => e.stopPropagation(),
}

function fmt(t: number) {
  const m = Math.floor(t / 60)
  return `${m}:${(t - m * 60).toFixed(2).padStart(5, '0')}`
}

function Empty({ children }: { children: React.ReactNode }) {
  return <div className="empty">{children}</div>
}

// ------------------------------------------------------------------ key images
function KeyCard({ k, run, vocab }: { k: Json; run: string; vocab: Json }) {
  const [editing, setEditing] = useState(false)
  const [motion, setMotion] = useState<string>(k.motion_override ?? '')
  const t = k.tags ?? {}
  const mo = t.motion ?? {}
  return (
    <div className={`key ${k.checked ? 'on' : 'off'}`} data-testid={`key-${k.start}`}>
      <img src={media(run, k.thumb)} alt={`key image at ${fmt(k.t)}`} />
      <label className="check" title={k.locked ? 'starts a shot: always a key image' : ''}>
        <input
          type="checkbox"
          data-testid={`key-check-${k.start}`}
          checked={k.checked}
          disabled={k.locked}
          onChange={(e) => send('toggle_key', { start: k.start, checked: e.target.checked })}
        />
        <span>{fmt(k.t)}</span>
        {k.locked && <span className="badge">shot</span>}
        {k.user_added && <span className="badge">added</span>}
      </label>
      {k.tags ? (
        <div className="tags">
          <span className="chip action">{t.action.replace('_', ' ')}</span>
          {mo.direction !== 'none' && <span className="chip">{mo.direction.replaceAll('_', ' ')}</span>}
          <span className="chip">{mo.speed}</span>
          <span className="chip">{t.camera.replace('_', ' ')}</span>
          <div className="imp" title={`importance ${t.importance}`}>
            <div style={{ width: `${t.importance * 100}%` }} />
          </div>
          <div className="reason">{k.reasons.join(', ')}</div>
        </div>
      ) : (
        <div className="tags muted">tagging…</div>
      )}
      {k.tags && (
        <button className="link" onClick={() => setEditing(!editing)}>
          {editing ? 'close' : 'edit motion'}
        </button>
      )}
      {editing && k.tags && (
        <div className="edit">
          <select
            value={t.action}
            onChange={(e) =>
              send('edit_tags', { start: k.start, tags: { action: e.target.value, motion: { ...mo, verb: e.target.value } } })
            }
          >
            {vocab.verbs.map((v: string) => (
              <option key={v}>{v}</option>
            ))}
          </select>
          <select
            value={mo.direction}
            onChange={(e) => send('edit_tags', { start: k.start, tags: { motion: { ...mo, direction: e.target.value } } })}
          >
            {vocab.directions.map((v: string) => (
              <option key={v}>{v}</option>
            ))}
          </select>
          <select
            value={mo.speed}
            onChange={(e) => send('edit_tags', { start: k.start, tags: { motion: { ...mo, speed: e.target.value } } })}
          >
            {vocab.speeds.map((v: string) => (
              <option key={v}>{v}</option>
            ))}
          </select>
          <textarea
            placeholder="or describe the motion in your own words"
            value={motion}
            onChange={(e) => setMotion(e.target.value)}
            onBlur={() => send('set_motion', { start: k.start, text: motion || null })}
          />
        </div>
      )}
    </div>
  )
}

export function KeysPanel() {
  const { view, run } = useStore()
  const [at, setAt] = useState(0)
  if (!view || !run) return <Empty>Drop or upload a cooking video to start (toolbar: New run).</Empty>
  if (!view.keys) return <Empty>Finding key images…</Empty>
  const keys: Json[] = view.keys
  const dur: number = view.meta?.duration ?? 0
  const plan = view.plan
  return (
    <div className="panel-body">
      <div className="row head">
        <b>Key images</b>
        <span className="muted">
          {keys.filter((k) => k.checked).length} of {keys.length} checked
          {plan && ` · ${plan.clips.length} clips · ${plan.out_duration.toFixed(1)} s output`}
        </span>
      </div>
      <div className="keys" {...stop}>
        {keys.map((k) => (
          <KeyCard key={k.start} k={k} run={run} vocab={view.vocab} />
        ))}
      </div>
      <div className="row scrub" {...stop}>
        <input
          type="range"
          min={0}
          max={dur}
          step={0.04}
          value={at}
          onChange={(e) => setAt(parseFloat(e.target.value))}
          aria-label="source time"
        />
        <span>{fmt(at)}</span>
        <button data-testid="add-key" onClick={() => send('add_key', { t: at })}>
          + key here
        </button>
      </div>
    </div>
  )
}

// ------------------------------------------------------------------ style + settings
export function StylePanel() {
  const { view } = useStore()
  const sel: Json = view?.settings?.style ?? {}
  const [prompt, setPrompt] = useState<string | null>(null)
  const [negative, setNegative] = useState<string | null>(null)
  if (!view) return <Empty>Style</Empty>
  const cat: Json = view.catalog
  const layer = (name: string, label: string) => (
    <label className="field">
      <span>{label}</span>
      <select
        data-testid={`style-${name}`}
        value={sel[name] ?? 'none'}
        onChange={(e) => send('set_style', { [name]: e.target.value === 'none' ? null : e.target.value })}
      >
        <option value="none">— none —</option>
        {cat[name].map((p: Json) => (
          <option key={p.name} value={p.name} title={p.description}>
            {p.title}
          </option>
        ))}
      </select>
    </label>
  )
  const clips: Json = view.settings.clips
  return (
    <div className="panel-body" {...stop}>
      <b>Style</b>
      {layer('animation', 'Animation style')}
      {layer('director', "Director's language")}
      {layer('finish', 'Film finish')}
      <label className="field">
        <span>Your prompt</span>
        <textarea
          data-testid="custom-prompt"
          value={prompt ?? sel.custom_prompt ?? ''}
          onChange={(e) => setPrompt(e.target.value)}
          onBlur={() => prompt !== null && send('set_style', { custom_prompt: prompt })}
        />
      </label>
      <label className="field">
        <span>Avoid</span>
        <input
          value={negative ?? sel.custom_negative ?? ''}
          onChange={(e) => setNegative(e.target.value)}
          onBlur={() => negative !== null && send('set_style', { custom_negative: negative })}
        />
      </label>
      {view.style && <div className="resolved">{view.style.composed.image_prompt}</div>}
      <hr />
      <label className="field">
        <span>Clip length</span>
        <select data-testid="clip-mode" value={clips.length_mode} onChange={(e) => send('set_clips', { length_mode: e.target.value })}>
          <option value="match">match the source</option>
          <option value="fixed">fixed</option>
          <option value="per_clip">set per clip</option>
        </select>
        {clips.length_mode === 'fixed' && (
          <input
            type="number"
            min={1}
            max={clips.max_clip_s}
            step={0.5}
            defaultValue={clips.fixed_s}
            onBlur={(e) => send('set_clips', { fixed_s: parseFloat(e.target.value) })}
          />
        )}
      </label>
      <label className="field">
        <span>Sound</span>
        <select data-testid="audio-mode" value={view.settings.audio.mode} onChange={(e) => send('set_audio', { mode: e.target.value })}>
          <option value="regenerate">new soundtrack (foley + voice sounds)</option>
          <option value="keep_voice" disabled={!view.meta?.has_audio}>
            keep the original voice
          </option>
          <option value="silent">silent</option>
        </select>
      </label>
    </div>
  )
}

// ------------------------------------------------------------------ samples
export function SamplesPanel() {
  const { view, run } = useStore()
  if (!view || !run) return <Empty>Style samples</Empty>
  const st = view.style
  if (!st?.samples) return <Empty>Making style samples…</Empty>
  const approved = st.approval?.current ? st.approval.candidate : null
  return (
    <div className="panel-body">
      <div className="row head">
        <b>Style samples</b>
        <span className="muted">
          {approved ? `approved ${approved}` : st.samples_current ? 'approve one to generate' : 'updating for the new style…'}
        </span>
      </div>
      <div className="samples" {...stop}>
        {st.samples.map((s: Json) => (
          <figure key={s.id} className={approved === s.id ? 'approved' : ''} data-testid={`sample-${s.id}`}>
            <img src={media(run, s.image)} alt={`sample ${s.id}`} />
            <figcaption>
              <span>{s.id}</span>
              <button
                data-testid={`approve-${s.id}`}
                disabled={!st.samples_current}
                onClick={() => send('approve', { sample: s.id })}
              >
                {approved === s.id ? '✓ approved' : 'Approve'}
              </button>
            </figcaption>
          </figure>
        ))}
      </div>
    </div>
  )
}

// ------------------------------------------------------------------ clips
export function ClipsPanel() {
  const { view, run } = useStore()
  if (!view || !run) return <Empty>Clips</Empty>
  if (!view.plan) return <Empty>Clips appear after the key images are tagged.</Empty>
  const byId: Record<string, Json> = Object.fromEntries((view.clips ?? []).map((c: Json) => [c.id, c]))
  const perClip = view.settings.clips.length_mode === 'per_clip'
  return (
    <div className="panel-body">
      <div className="row head">
        <b>Clips</b>
        <span className="muted">one per checked key image</span>
      </div>
      <div className="clips" {...stop}>
        {view.plan.clips.map((p: Json) => {
          const c = byId[p.id] ?? {}
          return (
            <div key={p.id} className={`clip ${c.status}`} data-testid={`clip-${p.id}`}>
              {c.video ? (
                <video src={media(run, c.video)} poster={media(run, c.poster)} controls muted loop preload="metadata" />
              ) : (
                <div className="placeholder">{c.status === 'pending' ? 'not generated yet' : c.status}</div>
              )}
              <div className="row">
                <span className={`status ${c.status}`}>{c.status}</span>
                <span>
                  {fmt(p.src[0])}–{fmt(p.src[1])} → {p.clip_s.toFixed(2)} s{p.anchored ? ' ⇥' : ''}
                </span>
              </div>
              <div className="row">
                {perClip && (
                  <input
                    type="number"
                    min={1}
                    max={view.settings.clips.max_clip_s}
                    step={0.25}
                    defaultValue={p.clip_s}
                    aria-label="clip seconds"
                    onBlur={(e) => send('set_clips', { per_clip: { [p.start]: parseFloat(e.target.value) } })}
                  />
                )}
                <button disabled={!c.video} onClick={() => send('rerun', { clip: p.id })}>
                  ↻ Re-run
                </button>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ------------------------------------------------------------------ output
export function OutputPanel() {
  const { view, run } = useStore()
  if (!view || !run) return <Empty>Output</Empty>
  const o = view.output
  if (!o) return <Empty>The final video appears here.</Empty>
  const ch = o.checks
  return (
    <div className="panel-body" {...stop}>
      <div className="row head">
        <b>Output</b>
        <a href={media(run, o.video)} download data-testid="download">
          ⬇ download
        </a>
      </div>
      <video data-testid="output-video" src={media(run, o.video)} controls preload="metadata" />
      <div className="checks">
        {ch.size[0]}×{ch.size[1]} · {ch.frames} frames · {ch.duration_s.toFixed(2)} s · sound: {ch.audio_mode}
        {ch.loudness_lufs !== undefined && ` · ${ch.loudness_lufs} LUFS · ${ch.true_peak_dbtp} dBTP`}
        {o.flagged_clips.length > 0 && <div className="warn">flagged clips: {o.flagged_clips.join(', ')}</div>}
        {o.audio?.cut_words?.length > 0 && <div className="warn">voice words cut to fit: {o.audio.cut_words.join(' ')}</div>}
      </div>
    </div>
  )
}
