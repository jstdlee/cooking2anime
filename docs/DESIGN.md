# cooking2anime — Pipeline Design & Plan

Goal: take an input cooking video and remake it as Japanese anime with a
customizable prompt. The output keeps the original's motions, gestures and emotion,
has a detailed, photographic, professional look, contains no text, logos or
watermarks, keeps the original soundtrack by default (or, if the user chooses,
regenerates it with Fish Audio voice and synced foley), and exports at 720p or 1080p.

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
| 8 | **Keep the original sound by default.** | The output has exactly the same length and timeline as the input: every chunk is generated with the source's frame count, then re-timed to the source fps. Chunk boundaries are pinned to the original frames, so the original audio stays in sync at least at every boundary, and close to it in between. The original track is copied over, only loudness-normalized. |
| 9 | **Regenerating the sound is opt-in.** | Users can choose `audio.mode: regenerate` (new soundtrack) or `mix` (original sound plus additions), and pick the parts: **sfx**, generated from the new video by a foley model (HunyuanVideo-Foley / ControlFoley / MMAudio) and guided by the sound cues; **narration**, Japanese with emotion from Fish Audio **Drama 3**, which is text-to-speech only, via API or open weights; **bgm**. `mix` can also drop the original voice or music (Demucs stem split) while keeping the real kitchen sound. |
| 10 | **Automatic QA instead of human review.** | A VLM compares each generated chunk with the original shot description (motion, gestures, emotion, any visible text) and scores style similarity to the approved keyframe. Chunks that fail are retried with a new seed or a sharper prompt. |

---

## 2. Architecture

```
            ┌──────────────────────── ANALYZE ────────────────────────┐
input.mp4 ─►│ probe → shot detect → chunk (~5 s) → boundary keyframes │
            │ → transcript (WhisperX)                                 │
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
            │ mode=original (default): copy source audio → loudnorm   │
            │ mode=regenerate | mix (user opt-in), parts selectable:  │
            │   sfx: foley V2A on generated video (VLM sound cues)    │
            │   narration: LLM JP script w/ emotion tags → Fish Drama 3│
            │   bgm (optional) · mix: Demucs keep/drop original stems │
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
| Transcript | WhisperX | Cloud STT | Content source for narration |
| Narration script | Local LLM (Qwen) | Claude API | JP rewrite + Fish direction tags + per-shot character budget |
| TTS | Fish Drama 3 (SGLang-Omni, large GPU) or S2-Pro (4B) | Fish API `POST /v1/tts`, header `model: drama-3` | |
| Foley | HunyuanVideo-Foley / ControlFoley / MMAudio | fal MMAudio etc. | Prompt = VLM sound cues |
| Stem split (mix mode) | Demucs / BS-RoFormer | — | Keep kitchen sound, drop original voice/music if wanted |
| Mix/mux | ffmpeg (copy original track by default; loudnorm, duck under voice) | — | |

### 2.3 Code layout (planned)

```
cooking2anime/
  c2a/
    cli.py               # c2a run | approve | resume | rerun --chunk | status
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
| **M0 Skeleton** | CLI, config schema, run folder + manifest/caching, ffmpeg probe/split/concat/mux, provider base classes | `c2a run in.mp4` runs end-to-end with every stage stubbed |
| **M1 Analyze + Describe** | Shot detection, chunking, boundary keyframes, VLM motion/emotion JSON, prompt builder, WhisperX | `prompts.jsonl` reads like an accurate shot list |
| **M2 Preview + gate** | Keyframe restyle via ComfyUI and a cloud backend, style presets, review HTML, `approve`, `style.lock.json` | A person can pick a style in under 2 minutes; no text in the restyled frames |
| **M3 Generate** | Wan FLF2V workflow + cloud backend, chunk concat, per-chunk rerun | A 30 s test clip with no manual steps and clean joins |
| **M4 Post + original audio** | RIFE, SeedVR2 for 1080p, grade/LUT/grain, encode presets, exact duration match, original audio muxed (default mode) | 720p and 1080p outputs with the original sound in sync |
| **M5 Optional audio regeneration** | `regenerate`/`mix` modes: foley V2A, Demucs stems, LLM narration script, Fish Drama 3 (API + local), BGM, loudness mix | User can switch modes and parts in config; final mp4 at -14 LUFS |
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
| Original sound drifts slightly from the regenerated motion | Boundaries are frame-exact; if a chunk drifts noticeably, the QA judge flags it and the chunk is retried, and the user can switch that video to `mix` or `regenerate` |
| Voice timing does not fit the shots (narration option) | Script per shot with a character budget based on shot length; small time-stretch (≤8%) with ffmpeg `atempo` |
| License / rights | License flag per backend; Fish Community License terms; voice cloning only with consent |
