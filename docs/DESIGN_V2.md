# cooking2anime — Design v2: one Studio app, key images, style presets

Status: **implemented in v0.2.0** (deviations from the proposal are listed in §10). Replaces the parts of [DESIGN.md](DESIGN.md) (v1, shipped as v0.1.0)
listed in §9. Everything not listed there (provider interfaces, cache, QA metrics,
output contract) carries over unchanged.

## 0. What changes, in one paragraph

Everything happens in **one app, c2a Studio**: an infinite canvas (tldraw SDK) with the
engine behind it. You drop a video on the canvas and the app shows **key images**, the
few frames that best stand for each action, scene or motion. An AI vision model tags
each one, and you check or uncheck them. You pick a style from **built-in presets**
(animation style + director's camera language + film finish) or write your own prompt.
Each checked key image becomes the **main image of one generated clip**
(image-to-video). Clips play right on the canvas. For sound you choose either a new
soundtrack (default) or **keep the original voice**, re-timed so each phrase starts at
its key image and fits the new clip's length. The CLI stays for scripts and servers,
and every command it has is also a button in the Studio.

## 1. One app: c2a Studio

```
┌──────────────────────────── c2a Studio (browser tab or app window) ────────────────────────────┐
│ toolbar: [Doctor] [New run ⬆ video] [Generate ▶] [Re-run ↻] [Export ⬇] [Open output]    status │
│ ┌────────────────────────────────── tldraw canvas ───────────────────────────────────────────┐ │
│ │ Key images strip:  [☑ 0:00 slice·onion] [☐ 0:01 knife·lift] [☑ 0:03 scrape·board] …         │ │
│ │ Style:   preset pickers + prompt card  →  sample grid  →  [Approve] on a sample              │ │
│ │ Clips:   ▶ inline video per key image, status colour, metrics, [↻ Re-run], prompt card      │ │
│ │ Output:  ▶ final video, report                                                              │ │
│ └─────────────────────────────────────────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────────────────────────────────────┘
          │ WebSocket (commands up, view model down) + HTTP (uploads, media, range requests)
┌─────────┴───────── c2a engine (Python, same process as `c2a studio`) ─────────────────────────┐
│ runs/<id>/manifest.json (source of truth) · stages · providers (local ComfyUI / cloud fal)   │
└───────────────────────────────────────────────────────────────────────────────────────────────┘
```

- **Start:** `c2a studio [--host 127.0.0.1] [--port 8765]` starts the engine and
  serves the app. On a headless GPU box (DGX Spark), run it with `--host 0.0.0.0`
  and open it from a laptop browser on the same network, with an access token printed
  at start. On a desktop it opens the default browser; a native window wrapper is
  optional later.
- **CLI ↔ Studio parity.** Every CLI command maps to one Studio action, and both call
  the same engine function:

| CLI | Studio |
|-----|--------|
| `c2a doctor` | **Doctor** panel: ffmpeg, GPU/VRAM, ComfyUI reachable, model files, API keys (fal, Fish, VLM), each with a fix hint |
| `c2a run in.mp4` | **New run**: drop or upload a video; key images appear as they are found |
| `c2a board open` | (the Studio *is* the board) |
| `c2a watch` | (live: changes run immediately; no polling) |
| `c2a approve --pick` | **Approve** button on a style sample |
| `c2a keys <run> --check/--uncheck <t>` | checkbox on a key image |
| `c2a status` | status bar + progress on each card |
| `c2a open` | ▶ inline player; **Open output** downloads or opens the file |
| `c2a rerun --chunk` | **↻ Re-run** on a clip card (new seed) |

- **Why the tldraw SDK:** first-class custom shapes (React components on the canvas:
  checkboxes, tag chips, a `<video>` player, buttons). It's the same `.tldr` record model
  the engine already writes, so the v1 projector, ownership rules and schema validator
  carry over. It's mature, and it works over the network.
  - **License:** the tldraw SDK needs a license key in production. Development is free,
    and tldraw offers a free hobby/non-commercial license (with a "made with tldraw"
    watermark) and paid commercial licenses. Check the current terms before release; the
    key is read from `C2A_TLDRAW_LICENSE`.
- **No file races.** In v1 the engine and GoDraw both wrote `board.tldr`. In the Studio
  the person's edits are sent as **typed commands** (`toggle_key`, `set_prompt`,
  `approve`, `rerun`…), which the engine validates, applies to the manifest, and
  answers with record diffs to every open tab. `board.tldr` is still written as a
  snapshot, so GoDraw or tldraw.com can open it to look at; custom shapes appear there
  as labelled boxes.

## 2. Key images (replaces fixed chunks and boundary keyframes)

A **key image** is a frame that represents one action, scene or motion beat, e.g. "knife
comes down on the onion" or "pan lid lifts, steam". It is the **main image** of one
generated clip. The video is no longer cut into equal ~5 s chunks.

### 2.1 Finding candidates (stage `keys`)
1. **Cuts:** TransNetV2 shot boundaries. The first frame of every shot is a candidate
   and is **locked checked**, because a clip can never span a cut.
2. **Change points within a shot:** peaks in a combined signal of optical-flow
   magnitude (RAFT, sampled at 8 fps), the direction change of the dominant motion, and
   the image-embedding distance (DINOv2) to the previous candidate. Non-maximum
   suppression keeps at least `keys.min_gap_s` (default 0.75 s) between candidates.
3. **Frame choice:** within ±0.25 s of each change point, take the sharpest frame
   (variance of the Laplacian) with the least motion blur. That is the image a
   still-to-video model can animate best.
4. **Coverage:** if two checked keys end up more than `keys.max_gap_s` apart (default
   = the model's longest clip, e.g. 5 s), the best candidate in between is added.

### 2.2 Auto-tagging (stage `tag`, VLM)
For each candidate the VLM gets the image plus the clip from it to the next candidate
(4 fps), and returns strict JSON:
```json
{"t": 3.20, "scene": "cutting board, warm window light", "action": "scrape",
 "motion": {"verb": "scrape", "direction": "left", "speed": "fast"},
 "subjects": ["knife", "green onion"], "emotion": {"label": "focused", "intensity": 1},
 "camera": "static", "sound_cues": ["knife_scrape"], "importance": 0.82,
 "suggest": true, "reason": "new action starts: slices pushed aside"}
```
- **Vocabularies:** camera, emotion and sound cues stay closed (v1 lists). Action/motion
  verbs come from a closed cooking-verb list (~60: slice, dice, stir, flip, pour, knead,
  plate…), so tags can be filtered and compared.
- **Auto-check:** `suggest && importance ≥ keys.auto_check_min` (default 0.6), plus
  every locked shot start, plus the coverage rule in §2.1.

### 2.3 You decide
- **Check/uncheck** any key image (except locked shot starts). Unchecking merges that
  span into the previous key image's clip.
- **Edit tags:** the tags become the motion prompt for that key's clip, so editing a tag
  edits the clip.
- **Add** a key image: scrub the source timeline under the strip and press "+ key here".
  The new image is tagged automatically.
- **Counter:** a running total shows `N clips · ~M s output · est. cost/time`.

### 2.4 Segments and clip length
Checked keys `k1 < k2 < …` define segments `[t_i, t_{i+1})`; the last one runs to its
shot's end. For each segment, the **clip length** is chosen by `clips.length_mode`:

| Mode | Clip length | Output length |
|------|-------------|---------------|
| `match` (default) | segment length (clamped to [1 s, model max]; longer segments get extra keys by the coverage rule) | ≈ source length |
| `fixed` | `clips.fixed_s` (e.g. 3 s) for every clip | N × fixed |
| `per_clip` | set on each clip card | sum of clips |

Frame counts are snapped to the model rule (4n+1), and each clip is retimed to its
exact length on output. That is v1's timeline logic, now applied with variable
segments; the property tests carry over.

### 2.5 Generation per clip
- **Main image:** the key image restyled in the locked style (v1 restyle stage,
  unchanged).
- **Prompt:** style layers (§3) + the key's tags rendered as motion text ("knife
  scrapes the green onion slices fast to the left, static camera, focused").
- **End anchor** (`clips.anchor_end`, default `next_key_in_shot`): when the next key is
  in the same shot, its restyled image is used as the last frame (first-last-frame
  mode), so clips join smoothly. Otherwise it's plain image-to-video, and the clip ends
  where the model takes it.
- **QA:** v1 gates apply, with "boundary match" checking frame 0 against the main image,
  and the last frame only when it is anchored.

## 3. Style presets (layered prompt customization)

A style is up to **four layers**, combined in a fixed order into the image-restyle
prompt, the video prompt, the negative prompt and post-processing settings:

| Layer | Controls | Built-in examples (v2 ships 6 per layer) |
|-------|----------|------------------------------------------|
| **Animation style** | rendering: line, shading, texture | Theatrical anime (luminous skies, detailed light) · 90s cel anime · Watercolor storybook (soft warm hand-painted) · Ukiyo-e woodblock · Modern TV anime (clean lines, flat shading) · Toon-shaded 3D |
| **Director's language** (inspired by, not imitating) | framing, camera movement, pacing, palette | Ozu-inspired: low static camera, tatami-height symmetry · Wes Anderson-inspired: centred symmetry, pastel palette · Wong Kar-wai-inspired: saturated neon, step-printed motion, handheld · Kurosawa-inspired: high contrast, deep focus, weather · Kore-eda-inspired: soft natural daylight, intimate handheld · Food-commercial: macro, slow push-in, glossy speculars |
| **Film finish / filter** | post: LUT, grain, halation, colour | 35 mm grain · Kodachrome warm · Bleach bypass · Teal-orange · Soft halation glow · Black & white |
| **Custom** | free prompt + negative | anything |

- **Files:** each preset is a YAML file in `c2a/styles/<layer>/<name>.yaml` with
  `image_prompt`, `video_prompt`, `negative`, `camera_bias` (it nudges the camera
  vocabulary; e.g. Ozu forces `static`), `post` (LUT, grain, halation), `realism`
  default and a thumbnail.
- **Composition is deterministic:** layer order, de-duplication, token budget (v1 drop
  order; custom text is never dropped). The resolved prompts are shown on the card, and
  you can edit them.
- **Sample grid:** 3 key images × your chosen combination (+ 2 variations). You approve
  one sample, which locks the style exactly as in v1.
- **Naming:** director presets describe a *language* ("low static camera, symmetrical
  framing") and use "inspired by" in their names. They never claim to reproduce a
  person's work.

## 4. Sound (adds a keep-voice option)

`audio.mode`:

| Mode | What you get |
|------|--------------|
| `regenerate` (default, as v1) | Original sound removed. Foley from the new clips + Fish Drama 3 voice sounds on emotion beats. No narration. |
| `keep_voice` | Original **voice** kept (music and kitchen noise removed), re-timed to the new clips; foley added under it. |
| `silent` | No audio track. |

**Keep-voice timing (precise rules):**
1. **Separate** the voice stem from the source (BS-RoFormer/Demucs), then run VAD and
   word timestamps (WhisperX) on the stem.
2. **Map phrases to clips.** Each spoken phrase belongs to the segment where it
   *starts*. Within clip *i*, it starts at the same offset from the key image as in the
   source: `start_in_clip = phrase_start − t_i`, so voice starts and stops line up with
   the key image timestamps.
3. **Fit to the clip length.** With `r = clip_len / segment_len`:
   - `0.85 ≤ r ≤ 1.15`: time-stretch the phrase by `r`, keeping pitch (rubberband), so
     offsets scale too.
   - `r < 0.85` (shorter clip): keep the natural speed and cut at the last whole word
     that fits, with an 80 ms fade. The cut words are listed on the clip card.
   - `r > 1.15` (longer clip): keep the natural speed and fill the rest with room tone.
4. **No overlaps:** a phrase never crosses into the next clip; a 40 ms crossfade is
   added at clip joins.
5. **Mix:** foley is ducked 8 dB under voice, then the v1 loudness and true-peak
   contract applies.

Lip-sync is out of scope (anime mouths are re-drawn by the video model); the clip card
warns when a face is visible during speech.

## 5. Engine changes

| v1 | v2 |
|----|----|
| `analyze` → fixed chunks (≤ 5 s, shared boundary keyframes) | `keys` (candidates) → `tag` (VLM) → **gate 1: key selection** (auto-checked; Generate proceeds) |
| `describe` per chunk | Folded into `tag` (per key + its segment) |
| `preview` on sampled keyframes | `preview` on 3 checked keys × style combos → **gate 2: style approval** |
| `restyle` boundary keyframes | `restyle` checked key images (+ next-key anchors) |
| `generate` FLF2V per chunk | `generate` I2V/FLF2V per segment, variable length |
| `post` exact source timeline | `post` concatenates clips at their chosen lengths |
| `audio` regenerate | `audio` regenerate / keep_voice / silent |
| board file + GoDraw + `watch` | Studio server (WebSocket commands), `board.tldr` snapshot for outside viewers |

Both gates happen in the same Studio session: check keys, pick a style, then press
**Generate**. Changing either later re-runs only what depends on it (cache keys
unchanged in principle).

## 6. API (Studio ↔ engine)

- `POST /api/runs` (multipart video) → `{run_id}`; `GET /api/runs`; `GET /api/doctor`.
- `WS /api/runs/{id}`, client → server commands (all validated, idempotent with `op_id`):
  `toggle_key {t, checked}`, `add_key {t}`, `edit_tags {t, tags}`,
  `set_style {layers, custom}`, `approve {sample}`, `set_clip {t, length_s|anchor}`,
  `set_audio {mode}`, `generate`, `rerun {t}`, `cancel`.
- Server → client: `view {view}` (the whole run view), `progress {stage, unit, pct}`,
  `notice {level, text}`, `ack {op_id}`.
- `GET /media/{run}/{path}` with HTTP Range (inline video scrubbing); allow-listed file
  types, as the v1 clip server.
- Auth: a random token at start (URL fragment + cookie). Binds to 127.0.0.1 unless
  `--host` is given.

## 7. Packaging

- The frontend (React + tldraw, Vite) is built in CI and bundled into the Python package
  as static files, so the arm64 tarball contains the whole Studio: `c2a studio` and
  open the URL. No Node.js on the target.
- Python adds `fastapi` + `uvicorn` (arm64 wheels exist).
- GoDraw drops out of the release (still opens `board.tldr` snapshots if installed).

## 8. Tests added (TESTING.md rules apply)

- **Key detection:** synthetic fixtures with known action changes (a moving box that
  changes direction at known times) → candidates within ±2 frames; min-gap and coverage
  properties under hypothesis.
- **Segments/clip-length:** property tests for all three length modes: output length is
  the sum of clip lengths, and no segment crosses a cut.
- **Keep-voice fitting:** synthetic speech-like signal with known phrase times → start
  offsets within 10 ms; stretch ratios; cut-at-word rule; no phrase crosses a join.
- **Style composition:** snapshot per preset combination; the token budget never drops
  custom text; Ozu forces `static`.
- **API:** every command validated (bad `t`, locked key, unknown preset); idempotent
  `op_id`; two tabs receive the same diffs.
- **Studio e2e:** Playwright (headless Chromium, in CI): upload fixture → keys appear →
  uncheck one → choose presets → approve → Generate (fake stack) → inline video plays
  (`video.readyState ≥ 2`) → output downloads.

## 9. What v2 supersedes in DESIGN.md (v1)

§2 front ends (GoDraw + `watch` → Studio), §3.1 chunking and §3.2 describe (→ keys/tag),
§3.4 preview sampling, §3.6 generate inputs, §3.7 post timeline (→ clip lengths), §3.8
audio (→ modes), §6 GoDraw integration (→ Studio + snapshot). §1's "out of scope" no
longer excludes keeping the original voice.

## 10. As built (v0.2.0): deviations from the proposal

- **View model instead of record diffs.** The engine pushes a JSON view of the run
  (keys, plan, style, samples, clips, output) and the frontend renders it inside one
  custom shape type in five kinds. The engine never writes tldraw records, so there
  is nothing to merge. See ADR-006.
- **Key detection stand-ins**, until the model-based signals land with the real stack:
  - *motion*: mean frame difference instead of RAFT flow. The change is scored relative
    to the motion level (a stop scores 1, so it doesn't depend on object size).
  - *direction*: phase correlation for camera moves, and the centroid velocity of
    changed pixels for objects.
  - *appearance*: a grey-histogram + edge-profile signature instead of DINOv2.

  The score is the strongest signal relative to its reference level (motion 0.8, a
  90° turn 0.9); default sensitivity 0.7, with 0.3 s windows. On synthetic clips,
  changes are found within ±2 frames, and steady sub-pixel motion produces no keys
  (tests/unit/test_keys_detect.py).
- **Gate:** the style samples are re-made only while no approval is current. Changing
  the style needs a new approval; checking keys after approval does not.
- **Keep-voice:** the voice stem and word timing are providers (BS-RoFormer/WhisperX
  for the real stack). The CPU stack uses a pass-through separator and an energy-based
  voice-activity detector, accurate to 20 ms on clean speech. Stretching uses ffmpeg
  `atempo`, which keeps pitch.
- **Studio:** a `Run ▶` button re-runs from cache. A canvas navigation bar was added,
  because tldraw does not render off-screen shapes. `C2A_STUDIO_TOKEN` sets a fixed
  token (tests, kiosks).
