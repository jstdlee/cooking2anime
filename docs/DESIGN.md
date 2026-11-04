# cooking2anime — Design

Related: [DEV_PLAN.md](DEV_PLAN.md) (milestones) · [TESTING.md](TESTING.md) (test rules)
· [configs/default.yaml](../configs/default.yaml)

## 0. Product in one paragraph

Input: a cooking video. Output: the same video remade as Japanese anime, with the
same length, cuts and timing, and the same motions, gestures and emotion. It has a
detailed, photographic finish, contains no text, logos or watermarks, and has **no
narration**. The original sound is removed and replaced by a new soundtrack: a foley
model makes the kitchen sounds and Fish Audio **Drama 3** makes short voice sounds
(breaths, sighs, a pleased "mm~"). Output is 720p or 1080p.

Everything is driven by prompts. The person makes **one decision**, approving the style
on a **GoDraw canvas**. The engine does the rest, on a local GPU or a cloud API.

## 1. Scope: deep, not wide

The rule for this project: support few cases, and make each one precise and
measurable.

**In scope (v1)**
- Input: one video file, ≤ 3 min, 16:9 or 9:16, ≥ 480p, any fps (including variable fps).
  Content: cooking (hands, tools, food, a person who appears now and then).
- One style preset shipped and tuned (`shinkai_food`), plus a free custom prompt.
- Two provider stacks, each fully supported, tested and calibrated:
  - **Local:** ComfyUI (headless) running Qwen-Image-Edit, Wan 2.x FLF2V 14B,
    SeedVR2 and RIFE, plus Qwen-VL, HunyuanVideo-Foley, and Fish Drama 3 via its API
    or a local server.
  - **Cloud:** fal endpoints for the same model families, plus the Fish API.
- Front ends: **GoDraw canvas** (review and approval) and the **CLI** (everything).

**Out of scope (v1) — written down so it doesn't creep in**
- Narration, dialogue, lip sync, subtitles.
- Detecting or removing text, logos or watermarks. They are excluded by the prompt
  only. A text check exists only as a QA step.
- Pose, depth or edge control (it may come back later as a fallback, see §9).
- More vendors (Replicate, Kling, Runway…), more presets, a web GUI, multiplayer.
- Background music.

## 2. Architecture

```
            ┌──────────────── CLI (c2a) ─────────────────┐      ┌──────── GoDraw canvas ────────┐
            │ run · watch · approve · rerun · status ·   │      │ native app, opens run.tldr    │
            │ doctor · calibrate · board                 │      │ reviews, approves, edits notes│
            └──────────────────┬─────────────────────────┘      └───────────────┬───────────────┘
                               │ calls                                          │ reads / writes
                               ▼                                                ▼
┌───────────────────────────────────── ENGINE (c2a.core) ─────────────────────────────────────────┐
│ stage graph · manifest (source of truth) · content-hash cache · scheduler · QA/retry           │
│                                                                                                 │
│  analyze ─► describe ─► preview ─► [GATE] ─► restyle ─► generate ─► post ─► audio ─► export   │
│                                                                                                 │
│ board projector: manifest ──► runs/<id>/board.tldr        board reader: board.tldr ──► inputs  │
└───────────────────────────────────────────┬─────────────────────────────────────────────────────┘
                                            │ provider interfaces
          ┌─────────────────┬───────────────┼──────────────────┬──────────────────┐
          ▼                 ▼               ▼                  ▼                  ▼
     VLM (Qwen-VL /     ImageEdit       VideoGen           Foley              VoiceFX
     cloud VLM)         (Qwen-Edit)     (Wan FLF2V)        (Hunyuan-Foley)    (Fish Drama 3)
          └─────── local: ComfyUI HTTP/WS API · cloud: fal · Fish API ───────┘
```

Three layers, and each has one job:

1. **Engine:** all the logic, with no UI code. Run state lives in
   `runs/<id>/manifest.json`, and that file is the **only source of truth**.
2. **CLI:** a thin wrapper around the engine. Anything the canvas can do, the CLI can
   also do (`c2a approve --pick`), so CI and servers never need the canvas.
3. **Canvas (GoDraw):** a *view* of the manifest plus an *input surface*. The engine
   writes the board, and the person edits it. The engine reads back only a few
   well-defined inputs (§6).

## 3. Stage specifications

Every stage reads only from `runs/<id>/` and writes only into its own folder,
`runs/<id>/<stage>/`. Its cache key is `sha256(stage version, config subset, input
hashes)`. Changing a prompt invalidates exactly the chunks that depend on it.

### 3.1 analyze
- **Probe and normalize.** Convert to constant frame rate at the source's rational fps
  (e.g. `30000/1001`), keeping the timestamp of every frame. Throw the audio away now;
  no later stage ever reads it.
- **Find cuts** with TransNetV2 (threshold 0.5). A cut is a hard boundary: chunks never
  cross one.
- **Chunk.** Videos are generated at `gen_fps = 16`. Wan needs a frame count of `4n+1`,
  so the chunk length `L ∈ {17, 33, 49, 65, 81}` frames (1–5 s). Pick the largest `L`
  that fits, split shots evenly, and merge a leftover under 17 frames into the previous
  chunk.
- **Boundary keyframes** are the source frames at each chunk's start and end time.
  Neighbouring chunks share their boundary frame. Each keyframe is saved as a PNG, with
  its source timestamp, in `keyframes.json`.
- **Timeline invariant:** the sum of chunk durations equals the source duration to
  within one output frame. Property tests check this (see TESTING.md).

### 3.2 describe (VLM)
- **Input:** each chunk's source clip, sampled at 4 fps, with timestamps made relative
  to the chunk start.
- **Output:** strict JSON matching `c2a/prompts/describe.schema.json`. Invalid JSON is
  retried twice, then the stage fails loudly:
  ```json
  {
    "subjects": ["person (hands only)", "chef's knife", "green onion", "wooden board"],
    "setting": "home kitchen, warm window light from left",
    "food_state": "whole green onions, half sliced",
    "beats": [
      {"t0": 0.0, "t1": 2.1, "action": "right hand slices green onion in quick even strokes"},
      {"t0": 2.1, "t1": 3.4, "action": "knife scrapes slices to the left of the board"}
    ],
    "gestures": ["left hand fingertips curled guiding the onion"],
    "emotion": {"label": "focused", "intensity": 1, "t": 0.0},
    "camera": "static",
    "sound_cues": [{"t0": 0.0, "t1": 2.1, "cue": "knife_chop_board"},
                   {"t0": 2.1, "t1": 3.4, "cue": "knife_scrape"}]
  }
  ```
- **Closed vocabularies keep prompts stable:**
  - `camera`: static, pan_left, pan_right, tilt_up, tilt_down, push_in, pull_out, handheld.
  - `emotion.label`: neutral, focused, calm, happy, delighted, surprised, satisfied.
    `intensity` runs 0–3.
  - `cue`: about 40 kitchen sound IDs (`sizzle_oil`, `boil_bubbling`, `knife_chop_board`,
    …) defined in `c2a/prompts/sound_cues.yaml`, each with a foley prompt phrase.
- **Rules:** at most 4 beats per chunk, and beats must cover the whole chunk without
  gaps. The VLM is told never to describe on-screen text, logos or UI.

### 3.3 Prompt builder (deterministic)
```
positive = style.prompt
         + scene(subjects, setting, food_state)
         + beats rendered as "first …, then …, finally …"
         + gestures + emotion phrase(label, intensity) + camera phrase(camera)
         + quality suffix (preset)
negative = preset.negative + style.negative
```
- It's a pure function. Its output, along with the template version, is saved per chunk
  in `prompts.jsonl`.
- **Token budget:** must stay under Wan's text-encoder limit of 512 tokens, with
  measured headroom of 15%. To fit, it drops parts in this order: gestures, then the
  quality suffix, then the setting. Beats and emotion are never dropped.
- **Overrides:** a per-chunk prompt note on the board (§6) replaces the beats sentence
  only, so the style always stays in.

### 3.4 preview + gate
- Pick `N = 4` sample keyframes spread across different shots (farthest-point sampling
  on CLIP embeddings), and restyle each with `K = 3` seeds.
- **Image edit prompt:** `style.prompt + style.clean_prompt`, using the edit model's
  "keep composition" instruction.
- Results are written to the board (§6). The run moves to `awaiting_approval`.
- **Approval** writes `style.lock.json`. It records the prompt, negative, LoRA set and
  weights, model hashes, seed, and the **approved image**. That image becomes the
  style reference for every later restyle (multi-image edit input).

### 3.5 restyle (all boundary keyframes)
- Restyle every keyframe with the locked style, using the approved image as reference.
- **Fixed seed per shot**, so frames within a shot stay consistent.
- **QA per keyframe:**
  - Style similarity: DINOv2 cosine to the approved image ≥ `qa.min_style_sim`.
  - Structure kept: SSIM of the luminance edges vs the source keyframe ≥ 0.35. This
    catches a restyle that changed the composition.
  - No text: OCR check (see §7).

  A keyframe that fails is retried with seed + 1, up to `max_retries` times.

### 3.6 generate
- **Per chunk:** FLF2V with first frame = restyled keyframe *k(i)*, last frame =
  restyled *k(i+1)*, `L` frames, the chunk's prompt, and the seed from the
  `(shot, chunk)` hash.
- **Boundary check:** SSIM of generated frame 0 vs *k(i)* and of frame L−1 vs *k(i+1)*
  ≥ 0.90. This proves the joins line up.
- **QA:** motion score from the VLM judge, which compares the generated clip with the
  chunk's beats (0–1), flicker (RAFT-warped frame error), the text check, and style
  similarity. Failing chunks are retried with a new seed. After the second retry the
  stage rewrites the beats into simpler wording. A chunk that still fails is marked
  `flagged` (red on the board) and the run carries on.
- **Scheduling:** chunks don't depend on each other, so locally they run through one
  queue of GPU jobs and in the cloud up to `cloud.max_parallel` run at once.

### 3.7 post
- Join chunks, dropping the duplicate boundary frame.
- RIFE retime from 16 fps to the output fps (24, or the source fps), so the total frame
  count matches the source duration exactly.
- 1080p only: SeedVR2 upscale.
- Grade/LUT, then film grain.
- Encode H.264 High, yuv420p, CRF 18, `+faststart`, at 1280×720 or 1920×1080
  (720×1280 or 1080×1920 for vertical video).

### 3.8 audio (new soundtrack only)
- **Foley:** HunyuanVideo-Foley on the *generated* video, one shot at a time (shots
  over 8 s go in overlapping 8 s windows with 0.5 s crossfades). The prompt is built
  from the shot's `sound_cues` phrases, with timing.
- **Voice sounds (Drama 3):** an event is made for each emotion beat with intensity ≥
  2, at most one every 4 s, and only in shots where a person is visible.
  - The text is a sound direction only, e.g. `[soft satisfied sigh]` or `[quiet
    delighted "mm"]`, sent to the Fish API (`model: drama-3`) or a local server.
  - **Word guard:** run ASR (Whisper) on each clip; if it hears any word, retry, and
    drop the event after 2 retries.
  - Placed at beat time `t`, at `voice.gain_db` relative to the foley.
- **Mix:** integrated loudness −14 LUFS, true peak ≤ −1 dBTP, 48 kHz stereo AAC
  192 kbps. With both layers disabled, the video has no audio track at all.

### 3.9 export
`runs/<id>/out/<name>_<res>.mp4` plus `report.json` and `report.html` (metrics for
each chunk, flagged chunks, timings, cost).

## 4. Provider interfaces

```python
class VLM(Protocol):        def describe(clip: Path, schema: dict) -> dict
class ImageEdit(Protocol):  def edit(src: Path, refs: list[Path], prompt: str, neg: str, seed: int) -> Path
class VideoGen(Protocol):   def flf2v(first: Path, last: Path, frames: int, prompt: str, neg: str, seed: int) -> Path
class Upscaler(Protocol):   def upscale(video: Path, size: tuple[int, int]) -> Path
class Interp(Protocol):     def retime(video: Path, n_frames_out: int) -> Path
class Foley(Protocol):      def foley(video: Path, prompt: str, seed: int) -> Path
class VoiceFX(Protocol):    def vocal(direction: str, voice_ref: str | None, seed: int) -> Path
```
- Every provider declares `name`, `version`, `license`, `commercial_ok` and
  `deterministic` (whether the same seed gives the same output). The engine refuses to
  run a `commercial_ok=False` provider when `run.commercial: true`.
- **ComfyUI backend:** loads an API-format workflow JSON from `c2a/workflows/`, patches
  the nodes listed in its sidecar `*.bindings.yaml` (node ID → parameter), queues it,
  follows progress over the WebSocket, and downloads the outputs. Workflows can be
  tuned on the ComfyUI canvas and dropped back in. The binding file is the contract,
  and CI checks it.
- **Fake backends** (`providers/fake.py`) are deterministic and CPU-only. Example:
  `FakeVideoGen` cross-fades from first to last with exactly `frames` frames. They let
  the whole pipeline run in CI.

## 5. Run state & CLI

```
c2a doctor                      # GPU/VRAM, ComfyUI reachable, models present + hashes, API keys, ffmpeg
c2a run in.mp4 [-c cfg.yaml]    # runs to the gate, writes board, prints board path
c2a board open <run>            # opens runs/<id>/board.tldr in GoDraw
c2a watch <run>                 # watches board.tldr; applies approval/overrides; resumes automatically
c2a approve <run> --pick 2:1    # headless approval: sample 2, seed 1
c2a rerun <run> --chunk 3.2     # re-run one chunk (e.g. after editing its note)
c2a open <run> [--chunk 3.2]    # open a chunk / the output mp4 in the system player
c2a status <run> [--json]
c2a calibrate --golden data/golden   # recompute QA thresholds on the golden set
```
Run states: `created → analyzing → describing → previewing → awaiting_approval →
generating → post → audio → done | failed`. Every change of state is written to the
manifest atomically (temp file + `rename`).

## 6. GoDraw canvas integration

**Decision: GoDraw is the canvas.** [`egoist/godraw`](https://github.com/egoist/godraw)
is a native, local whiteboard (Go on MyGo, no web view) for macOS, Linux and Windows.
It reads and writes tldraw `.tldr` files and **picks up changes other apps make to the
file**. That makes the file a clean contract: the engine writes `board.tldr`, the person
works in GoDraw, and the engine watches the file. The same file also opens in tldraw,
and CI validates every board against tldraw's schema.

- **Version:** pinned to one GoDraw commit (`canvas.godraw_commit`). `c2a doctor`
  checks the installed build, and `c2a board open` launches it.
- **Video:** not played on the canvas. Every chunk and the final output has a clickable
  `▶ open` shape whose link points to the mp4 (`file://…`), so it opens in the system
  player. `c2a open <run> --chunk 3.2` does the same from the CLI. The chunk frame shows
  restyled first/last keyframes and a small strip of frames, so the board can be
  scanned without playing anything.
- **Images:** JPEG thumbnails ≤ 512 px. The asset storage mode (embedded data URL vs
  file path next to the board) is fixed in spike S1. The budget is about 25 MB per
  board whichever mode is used.

**Board layout (one `.tldr`, three pages)**

| Page | Engine writes | Person can do |
|------|---------------|---------------|
| `1 · Style gate` | One frame per sample keyframe with K candidate images; a `STYLE PROMPT` note; a `✓ APPROVE` sticker note | Edit the prompt note (a new preview runs); **drag the ✓ sticker into a candidate frame** to approve |
| `2 · Shots` | Per chunk: a frame with a restyled first/last keyframe, a frame strip, metrics, a status colour (grey queued, yellow running, green pass, red flagged); a `PROMPT` note per chunk | Edit a chunk's prompt note; put a `↻` sticker on a chunk to re-run it |
| `3 · Report` | Summary numbers, list of flagged chunks, output paths | — |

**Ownership rules (how both sides write the same file safely)**
- Each record the engine creates has `meta.c2a = {owner: "engine", key: "<stable
  id>", rev: n}`. Notes and stickers the person can edit have `owner: "user-input"`.
  Everything else (the person's own drawings) belongs to the person and is never
  touched.
- **Engine write** = read the file → replace only `owner=engine` records, matched by
  `key` → keep everything else byte-for-byte → write to a temp file → `rename`. If the
  file's hash changed between read and write, start again (optimistic locking).
  Writes are batched at most once every 2 s.
- **Engine read** takes only these inputs:
  1. Approval: the sticker's `parentId` is a candidate frame.
  2. Prompt note text, if it differs from the last value the engine wrote.
  3. Re-run stickers.

  Each input is checked and copied into the manifest with the board hash, then shown on
  the board as handled (the sticker turns green).
- **The board is rebuildable.** If GoDraw autosaves an older copy over a status record with an older copy,
  the next projection puts it back. Losing the board never loses state, and `c2a board
  rebuild` makes it again from the manifest.

## 7. Quality gates (measured, not eyeballed)

| Metric | Where | How | Default threshold |
|--------|-------|-----|-------------------|
| Style similarity | restyle, generate | DINOv2-L cosine to approved image | ≥ 0.80 (calibrated) |
| Structure kept | restyle | Edge-map SSIM vs source keyframe | ≥ 0.35 |
| Boundary match | generate | SSIM of first/last generated frames vs keyframes | ≥ 0.90 |
| Motion match | generate | VLM judge: beats vs generated clip, 0–1 | ≥ 0.70 |
| Flicker | generate, post | RAFT-warped L1 between frames | ≤ 0.05 |
| No text | restyle, generate, export | PaddleOCR on 1 fps samples, conf ≥ 0.6 | 0 boxes |
| Duration | export | `|out − src|` | ≤ 1 output frame |
| No words in voice | audio | Whisper word count | 0 |
| Loudness | export | EBU R128 | −14 ± 1 LUFS, TP ≤ −1 dBTP |

`c2a calibrate` sets thresholds from the golden set (§TESTING): each threshold is the
5th percentile of the passing human-rated samples.

## 8. Hardware & cost (rough)

| Setup | Notes |
|-------|-------|
| 24 GB GPU (RTX 4090) | Wan FLF2V 14B fp8 at 720p takes several minutes per 5 s chunk; voice through the Fish API |
| 48–80 GB | Faster; Drama 3 can also run locally (SGLang-Omni) |
| Cloud | Everything on fal + the Fish API; chunks run in parallel; the gate keeps spend low before generation |

A 60 s video is about 12–15 chunks of 5 s each. Expect 1–3 h on a single 4090.

## 9. Risks

| Risk | Mitigation |
|------|------------|
| Motion differs from the source | First+last anchors, timed beats, shorter chunks on QA fail; pose-control fallback is a v2 item |
| Text leaks in | Clean-image prompt + negative prompt; OCR gate with retry |
| Engine and GoDraw both writing the board | Ownership rules, optimistic write, board rebuildable from the manifest; CLI `approve` always works |
| Drama 3 speaks real words | Whisper word guard, then drop the event |
| Licenses | `commercial_ok` per provider; Fish Community License terms; voice refs only with consent |
