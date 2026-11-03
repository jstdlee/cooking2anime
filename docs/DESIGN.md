# cooking2anime — Pipeline Design & Plan

Goal: take an input cooking video and remake it as Japanese anime with a
customizable prompt. The output keeps the original's motions, gestures and emotion,
has a detailed, photographic, professional look, contains no text, logos or
watermarks, and has **no narration**. The original sound is removed and a new soundtrack
is made to match the new video (Fish Audio Drama 3 for the people's voice sounds, a foley
model for the kitchen sounds). Output is 720p or 1080p.

**Everything is driven by prompts.**
- *What it looks like* comes from the style prompt plus a restyled keyframe.
- *How things move* comes from a **motion prompt** written automatically for each shot
  by a vision-language model (VLM) that watches the original.
- *What stays out* (text, logos, watermarks, subtitles) comes from the negative
  prompt. Nothing is detected or masked.

People only take part at **one gate**: they approve the style on a few sample
keyframes. After that the pipeline runs on its own. Every AI step goes through a
provider interface, so it can run on a **local** GPU (ComfyUI / Python) or a
**cloud** API, chosen in config.

---

## 1. Principles

| # | Decision | Why |
|---|----------|-----|
| 1 | **No detection/removal stage for text & logos.** | The output is newly generated from a restyled keyframe plus prompts, never edited from the source pixels. Overlays are left out of the keyframe restyle ("clean image, no text, no logo, no watermark") and blocked again in the video negative prompt. |
| 2 | **Motion is described in words.** | A VLM watches each shot and writes timed action beats ("0–2 s: right hand slices the scallion in quick strokes; 2–4 s: pushes it aside with the knife, smiles"), gestures, facial emotion and camera movement. That text becomes the video prompt. |
| 3 | **Anchor every chunk with first + last keyframes.** | A prompt alone produces similar motion, not the same motion. Restyling the original frames at both ends of each ~5 s chunk and using a first-last-frame image-to-video model (Wan FLF2V / Wan 2.x I2V, Kling, Veo…) pins where each motion starts and ends. That gets close to the original without pose control. |
| 4 | **Chunks share boundary keyframes.** | Chunk *i* ends on keyframe *k(i+1)*, and chunk *i+1* starts on that same keyframe. The joins line up exactly, and style cannot drift, because no chunk is generated from the previous chunk's output. |
| 5 | **Work per shot.** | Scene detection splits the video at cuts. Each shot gets its own description, and long shots are split into chunks of about 5 s. |
| 6 | **"Anime" vs "photographic texture" is a setting.** | A style preset (e.g. Shinkai-style lighting, painterly backgrounds, glossy realistic food, shallow depth of field, film grain) plus a `realism` value (0–1) that changes the prompt wording and the LoRA weights. |
| 7 | **1080p is an upscale step.** | Open video models generate at 720p at most. Generate at 720p, then use a temporally consistent upscaler (SeedVR2 / cloud) and RIFE interpolation to 24/30 fps. |
| 8 | **Remove the original sound and make a new one; no narration.** | The source audio is thrown away. There is no spoken narration and no script, so no transcript stage. The new soundtrack has two layers, both timed to the *generated* video. |
| 9 | **Drama 3 for voice sounds, foley for kitchen sounds.** | Fish Audio **Drama 3** (Oct 2026; API or open weights) is a speech model. It makes sounds a *person* makes, such as breaths, sighs, a pleased "mm~", a small laugh or humming, timed to the emotion beats from the VLM. It cannot make sizzling, chopping or boiling, so those come from a video-to-audio **foley** model (HunyuanVideo-Foley / ControlFoley / MMAudio) run on the generated video and guided by the VLM sound cues. Either layer can be switched off; with both off the output is silent. |
| 10 | **Automatic QA instead of human review.** | A VLM compares each generated chunk with the original shot description (motion, gestures, emotion, any visible text) and scores style similarity to the approved keyframe. Chunks that fail are retried with a new seed or a sharper prompt. |

---

## 2. Architecture

```
            ┌──────────────────────── ANALYZE ────────────────────────┐
input.mp4 ─►│ probe → shot detect → chunk (~5 s) → boundary keyframes │
            └──────────────────────────────┬──────────────────────────┘
                                           ▼
            ┌──────────────────────── DESCRIBE (VLM) ─────────────────┐
            │ per shot/chunk → JSON: subjects, timed action beats,     │
            │ hand gestures, facial emotion, camera move, food state,  │
            │ sound cues (ignores on-screen text/logos)                │
            └──────────────────────────────┬──────────────────────────┘
                                           ▼
            ┌──────────────────────── STYLE PREVIEW ──────────────────┐
            │ N sample keyframes × K seeds → image edit/restyle        │
            │ (Qwen-Image-Edit / FLUX Kontext + anime LoRA)            │
            │ prompt = style + "clean, no text/logo/watermark"         │
            │ → review page (grid)                                     │
            └──────────────────────────────┬──────────────────────────┘
                                           ▼
                           ★ HUMAN GATE: approve / tweak prompt ★
                                           ▼
            ┌──────────────────────── GENERATE (auto) ────────────────┐
            │ restyle ALL boundary keyframes with locked style         │
            │ per chunk: FLF2V(first=k_i, last=k_i+1,                  │
            │            prompt = style + motion description,          │
            │            negative = text, logo, watermark, ...)        │
            │ → QA (VLM judge + style sim) → retry if below threshold  │
            └──────────────────────────────┬──────────────────────────┘
                                           ▼
            ┌────────────────────────── POST ─────────────────────────┐
            │ concat chunks (shared frames dropped) → RIFE interp →   │
            │ SeedVR2 upscale (1080p only) → grade/LUT + grain        │
            └──────────────────────────────┬──────────────────────────┘
                                           ▼
            ┌────────────────────────── AUDIO ────────────────────────┐
            │ original audio discarded · no narration                 │
            │ sfx:   foley V2A on generated video (VLM sound cues)    │
            │ voice: Fish Drama 3 non-speech vocals (breath, sigh,    │
            │        "mm~", laugh) placed on VLM emotion beats         │
            │ mix + loudnorm (-14 LUFS) → mux                         │
            └──────────────────────────────┬──────────────────────────┘
                                           ▼
                                 output_720p.mp4 / output_1080p.mp4
```

### 2.1 Prompt assembly (per chunk)

```
positive = [style preset prompt]
         + [scene: subjects, setting, food state]        ← VLM
         + [motion beats with timing + gestures]          ← VLM
         + [facial emotion / mood]                        ← VLM
         + [camera: static / slow push-in / pan left]     ← VLM
         + [quality: detailed texture, cinematic lighting, professional photography]
negative = text, letters, subtitles, captions, watermark, logo, signature, UI,
           blurry, deformed hands, extra fingers, flicker, low quality
```

The VLM gets a fixed JSON schema, and the prompt builder is a template, so the
output is reproducible. Users can add their own text in `style.prompt` and
`style.negative`. For each chunk, the resolved prompt is saved in `prompts.jsonl`
so it can be inspected or edited, then that chunk alone re-run.

### 2.2 Stage details & recommended models (Nov 2026)

| Stage | Local (default) | Cloud alternative | Notes |
|-------|-----------------|-------------------|-------|
| Shot detection | TransNetV2 / PySceneDetect | — | Chunks ≤ 81 frames at 16 fps (~5 s) |
| Motion/scene description | Qwen-VL (video input) | Gemini / Claude / GPT (video or frame sequence) | Strict JSON schema, told to ignore overlays |
| Keyframe restyle | Qwen-Image-Edit or FLUX.1 Kontext + anime/food LoRA via ComfyUI | fal Kontext / Qwen-Edit, Gemini/GPT image edit | Seed, LoRA and prompt locked after approval |
| Video generation | Wan 2.x FLF2V / I2V 14B (fp8/GGUF) via ComfyUI | fal / Replicate Wan, Kling, Veo, Seedance (first-last-frame modes) | 720p |
| QA judge | Qwen-VL + DINOv2 similarity | Gemini / Claude | Motion match, emotion match, visible text, style |
| Upscale 1080p | SeedVR2 | Topaz / fal video upscale | Only when `output.resolution: 1080p` |
| Interpolation | RIFE 4.x | — | 16 fps → 24/30 fps |
| Voice sounds | Fish Drama 3 (SGLang-Omni, large GPU) or S2-Pro (4B) | Fish API `POST /v1/tts`, header `model: drama-3` | Non-speech only, e.g. `[soft satisfied sigh]`, `[quiet pleased hum]` |
| Foley | HunyuanVideo-Foley / ControlFoley / MMAudio | fal MMAudio etc. | Prompt = VLM sound cues |
| Mix/mux | ffmpeg (drop source audio, loudnorm) | — | |

### 2.3 Code layout (planned)

```
cooking2anime/
  c2a/
    cli.py               # c2a run | approve | resume | rerun --chunk | status
    web/                 # FastAPI app + static page (GUI on the same engine)
    pipeline.py          # stage graph, caching, resume
    config.py            # pydantic schema for configs/*.yaml
    prompts/             # VLM schema, prompt templates, style presets
    stages/
      analyze.py  describe.py  preview.py  generate.py  post.py  audio.py  qa.py
    providers/           # one interface per capability, many backends
      base.py            # VLM, ImageStylizer, VideoGenerator, TTS, Foley, Upscaler, LLM
      comfyui.py         # local: queues workflow JSON via ComfyUI HTTP/WebSocket API
      fal.py  replicate.py  fish.py  anthropic.py  openai_compat.py
    workflows/           # ComfyUI API-format workflow JSONs
    review/              # static HTML review page for the approval gate
  configs/
    default.yaml
    styles/*.yaml
  runs/<run_id>/         # artifacts + manifest.json (git-ignored)
```

### 2.4 Key design decisions

- **Stages are pure functions over files.** Each stage writes to `runs/<id>/<stage>/`
  and records the input hash in `manifest.json`. Re-running skips anything already
  done, a crash resumes where it stopped, and editing a prompt only re-runs the
  chunks that depend on it.
- **One interface per capability.** For example, `VideoGenerator.generate(first, last,
  prompt, negative, frames, seed) -> video`. Backends are chosen in config, so moving
  a stage between local and cloud is a one-line change. `comfyui.url` can point at a
  remote GPU box.
- **The ComfyUI API is the local engine.** The pipeline patches node values in a
  workflow JSON template, queues the job and downloads the outputs. No manual
  clicking.
- **Human gate = a pause between stages.** `c2a run` stops with status
  `awaiting_approval` and writes `review/index.html`. The person picks a style with
  `c2a approve <run> --pick N` (or edits the prompt and reruns the preview), and
  everything after that runs unattended.
- **Parallel work.** Restyling the keyframes and generating the chunks are independent
  of each other (thanks to the shared boundary keyframes), so cloud backends can run
  them all at once. Locally they go through a single GPU queue.

### 2.5 User interface: pipeline engine + CLI + thin web GUI (not a canvas)

| Option | Good for | Problem for this project |
|--------|----------|--------------------------|
| Canvas (ComfyUI node graph) | Tuning one shot, trying models and settings by hand | Has no good way to express "for every shot, for every chunk, retry on QA fail, call cloud APIs, pause for approval". The graph turns into a tangle, and you end up working step by step by hand |
| CLI only | Automation, batch folders, servers, cron, cloud GPUs | The approval step (choosing a style from images) is awkward in a terminal |
| **Engine + CLI + web GUI (chosen)** | One Python engine does all the work; the CLI and GUI are two thin front ends on it | — |

- **Engine (`c2a/pipeline.py`)**: stages, caching, resume, provider backends. All the
  logic lives here.
- **CLI (`c2a run/approve/resume/rerun`)**: built first. Used for batch runs,
  servers and scripts.
- **Web GUI (FastAPI + a simple page, opened at `localhost`)**: upload a video, edit
  the style prompt, pick the style from the preview grid (the approval gate), watch
  progress per shot, play chunks, re-run one chunk, download. It calls the same
  engine, so the GUI and CLI never behave differently.
- **Canvas stays under the hood**: ComfyUI runs headless as the local engine, and
  its workflow JSONs in `c2a/workflows/` can be opened on the ComfyUI canvas by
  power users who want to tune a model setting. The pipeline then uses the tuned
  workflow automatically.

---

## 3. Example config

See [`configs/default.yaml`](../configs/default.yaml).

---

## 4. Hardware & cost guide (rough)

| Setup | What runs locally | Notes |
|-------|-------------------|-------|
| 24 GB GPU (RTX 4090) | Wan 14B FLF2V fp8/GGUF at 720p (several minutes per 5 s chunk), Kontext/Qwen-Edit, a 7B-class VLM, MMAudio | Fish API for voice, or S2-Pro locally |
| 32 GB+ / 48–80 GB | Same, faster; larger VLM; Drama 3 locally | |
| Cloud only | All stages via fal/Replicate + a VLM API + Fish API | Cost scales with seconds of video; the preview gate keeps spend low before the expensive stage |

A 60 s video is about 12–15 chunks of 5 s each, plus retries. Plan for 1–3 hours on
one 4090, or minutes with parallel cloud jobs.

---

## 5. Implementation plan

| Milestone | Scope | Done when |
|-----------|-------|-----------|
| **M0 Skeleton** | Pipeline engine + CLI, config schema, run folder + manifest/caching, ffmpeg probe/split/concat/mux, provider base classes | `c2a run in.mp4` runs end-to-end with every stage stubbed |
| **M1 Analyze + Describe** | Shot detection, chunking, boundary keyframes, VLM motion/emotion/sound-cue JSON, prompt builder | `prompts.jsonl` reads like an accurate shot list |
| **M2 Preview + gate** | Keyframe restyle via ComfyUI and a cloud backend, style presets, review HTML, `approve`, `style.lock.json` | A person can pick a style in under 2 minutes; no text in the restyled frames |
| **M3 Generate** | Wan FLF2V workflow + cloud backend, chunk concat, per-chunk rerun | A 30 s test clip with no manual steps and clean joins |
| **M4 Post** | RIFE, SeedVR2 for 1080p, grade/LUT/grain, encode presets, source audio dropped | Silent 720p and 1080p outputs |
| **M5 Audio** | Foley V2A + Fish Drama 3 voice sounds (API + local), beat placement, loudness mix | Final mp4 with a new synced soundtrack at -14 LUFS |
| **M5.5 Web GUI** | Local web app on the same engine: upload, style gate, progress, per-chunk rerun | Whole flow usable without the terminal |
| **M6 QA + scale** | VLM judge + style score, retry policy, final report, parallel cloud jobs, batch folder mode | Unattended batch of 5 videos with a report |

Build M0 → M2 first, to confirm the look before spending GPU time on video.

---

## 6. Risks & mitigations

| Risk | Mitigation |
|------|------------|
| Motion differs from the original (prompt is looser than pose control) | First+last keyframe anchors, timed action beats in the prompt, shorter chunks (e.g. 3 s) for fast actions; optional `motion_control: pose` fallback on shots QA flags repeatedly |
| Text/logo leaks into the restyled keyframe | Strong clean-image wording in the edit prompt + negative prompt; QA judge flags visible text and retries that keyframe |
| Flicker / style drift | Every chunk is anchored on restyled keyframes that share the locked style; temporal upscaler; color match on concat |
| Hands/knife distorted | Hand-specific negatives, hand detail in the motion description, retry on QA flag |
| Food loses appetizing look | `realism` setting + food LoRA, food-specific quality phrases |
| Long run fails midway | Stage caching + `c2a resume` |
| Drama 3 vocals sound out of place | Short, quiet, placed only on clear emotion beats; volume set below the foley; layer can be turned off |
| License / rights | License flag per backend; Fish Community License terms; voice cloning only with consent |
