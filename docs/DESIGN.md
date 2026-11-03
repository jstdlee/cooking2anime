# cooking2anime — Pipeline Design & Plan

Goal: take an input cooking video, remove watermarks and burned-in text, restyle it
as Japanese anime with a customizable prompt, keep the original motion, gestures and
emotion, give it a detailed, photographic, professional look, add sound (Fish Audio
voice plus synced foley), and export at 720p or 1080p.

People only take part at **one gate**: they approve the style on a few sample
keyframes. After that the pipeline runs on its own. Every AI step goes through a
provider interface, so it can run on a **local** GPU (ComfyUI / Python) or a
**cloud** API, chosen in config.

---

## 1. Review of the proposed idea

The idea works. These are the changes that matter most in practice:

| # | Point | Why |
|---|-------|-----|
| 1 | **Clean before you stylize.** | If a watermark or caption reaches the stylizer, it gets redrawn as an anime artifact and is much harder to remove afterwards. Remove first, on the real footage, where inpainting works best. |
| 2 | **Work per shot, not per video.** | Split the video into shots (scene detection) and keep one style reference per shot. Video models only produce about 5 s (81 frames) per pass, and a shot boundary is a natural place to restart. |
| 3 | **Keyframe → propagate is the right pattern.** | First stylize one keyframe per shot with an image model; it is fast, cheap and easy for a person to judge. Then a video model spreads that look across the shot, guided by pose, depth and edge maps of the original. This is how today's best V2V restyling is done. |
| 4 | **Gestures and emotion come from control signals.** | Copy them explicitly. Use DWPose (body + **hands** + face) and depth as VACE control videos. For close-ups of a person's face, use Wan-Animate–style face/expression retargeting. Cooking shots are mostly hands, so hand keypoints plus canny/depth for the knife and food matter more than full-body pose. |
| 5 | **"Anime" and "photographic texture" pull in opposite directions; make the balance a setting.** | A style preset that sits between them (e.g. *Shinkai-style lighting, painterly backgrounds, realistic food rendering, shallow depth of field, film grain*) plus a `realism` value (0–1) that sets control strength and denoise. |
| 6 | **1080p is an upscale step.** | Open video models (Wan 2.x 14B) generate at 720p at most. Generate at 720p, then use a temporally consistent video upscaler (SeedVR2, or a cloud upscaler) for 1080p, plus frame interpolation (RIFE) to 24/30 fps. |
| 7 | **Fish Audio covers voice only.** | Fish Audio **Drama 3** (released Oct 2026, open weights + API) is the best fit for **narration and dialogue**: emotional Japanese voice from inline directions like `[excited, whispering]`. It is text-to-speech, so it cannot make sizzling, chopping or boiling sounds. Pair it with a **video-to-audio foley** model (HunyuanVideo-Foley / ControlFoley / MMAudio), or reuse the cleaned original ambience. |
| 8 | **Check the result automatically, not by eye.** | Score each shot automatically (style match to the approved keyframe, pose match to the source, flicker, leftover text found by OCR) and retry with a new seed when a score is too low. That is what lets the pipeline run unattended. |
| 9 | **Rights & licenses.** | Only remove watermarks from footage you own or have a license for. ProPainter's license is non-commercial; for commercial use prefer MiniMax-Remover or Wan VACE (Apache-2.0). Fish Drama 3 is under the Fish Community License (free under US$500k revenue; voice cloning needs consent). |

---

## 2. Architecture

```
            ┌──────────────────────── ANALYZE ────────────────────────┐
input.mp4 ─►│ probe → shot detect → keyframes → OCR/logo masks →      │
            │ pose (DWPose) + depth + canny → transcript (WhisperX)   │
            └──────────────────────────────┬──────────────────────────┘
                                           ▼
            ┌───────────────────────── CLEAN ─────────────────────────┐
            │ mask dilate/track → video inpaint (MiniMax-Remover /    │
            │ ProPainter / VACE-inpaint) → denoise (optional)         │
            └──────────────────────────────┬──────────────────────────┘
                                           ▼
            ┌──────────────────────── STYLE PREVIEW ──────────────────┐
            │ N sample keyframes × K seeds → image stylize            │
            │ (Qwen-Image-Edit / FLUX Kontext + anime LoRA, ControlNet)│
            │ → contact sheet / review page                           │
            └──────────────────────────────┬──────────────────────────┘
                                           ▼
                           ★ HUMAN GATE: approve / tweak prompt ★
                                           ▼
            ┌──────────────────────── STYLIZE (auto) ─────────────────┐
            │ per shot: stylize keyframe (locked style, seed, LoRA) → │
            │ Wan VACE V2V: ref=stylized keyframe, control=pose+depth │
            │ chunks of 81f with overlap, last-frame continuation,     │
            │ color match → QA score → retry if below threshold       │
            └──────────────────────────────┬──────────────────────────┘
                                           ▼
            ┌────────────────────────── POST ─────────────────────────┐
            │ shot stitch + crossfade → RIFE interp → SeedVR2 upscale │
            │ (1080p only) → grade/LUT + grain → sharpen              │
            └──────────────────────────────┬──────────────────────────┘
                                           ▼
            ┌────────────────────────── AUDIO ────────────────────────┐
            │ Demucs: split original → keep ambience, drop voice/music│
            │ Foley V2A on stylized video (fill gaps / replace)       │
            │ LLM: JP narration script with emotion tags, timed       │
            │ Fish Audio Drama 3 TTS → align → BGM (optional)         │
            │ mix + loudnorm (-14 LUFS) → mux                         │
            └──────────────────────────────┬──────────────────────────┘
                                           ▼
                                 output_720p.mp4 / output_1080p.mp4
```

### 2.1 Stage details & recommended models (Nov 2026)

| Stage | Local (default) | Cloud alternative | Notes |
|-------|-----------------|-------------------|-------|
| Shot detection | PySceneDetect / TransNetV2 | — | Also caps shots at ~5 s chunks |
| Keyframe pick | sharpness + motion-low + CLIP centrality | — | 1 per shot, plus extras on long shots |
| Text/logo detection | PaddleOCR + static-pixel logo detector (temporal median) + Florence-2/GroundingDINO "watermark" | — | Masks tracked with SAM 2, dilated a few px |
| Inpaint | MiniMax-Remover (fast, Wan-1.3B), ProPainter (quality, non-commercial) | fal / Replicate inpaint endpoints | Per-shot, so masks don't bleed across cuts |
| Control maps | DWPose (whole-body), Depth Anything V2, Canny | — | Saved as control videos |
| Keyframe stylize | Qwen-Image-Edit or FLUX.1 Kontext + anime/food LoRA via ComfyUI | fal FLUX Kontext / Qwen-Image-Edit, Gemini/GPT image edit | Same seed/LoRA/prompt locked after approval |
| Video stylize | Wan 2.x VACE 14B (fp8/GGUF) via ComfyUI; Wan-Animate for face-heavy shots | fal / Replicate Wan VACE, Kling/Runway V2V | 720p, 81-frame windows, 8–16 f overlap |
| Upscale 1080p | SeedVR2 (temporal) | Topaz / fal video upscale | Only when `output.resolution: 1080p` |
| Interpolation | RIFE 4.x | — | 16 fps → 24/30 fps |
| Transcript | WhisperX | Cloud STT | Source of narration content |
| Script | Local LLM (Qwen) or Claude API | — | JP rewrite + Fish direction tags + timings |
| TTS | Fish Drama 3 via SGLang-Omni (≈3B active, needs a large GPU) or S2-Pro (4B, lighter) | Fish API `POST /v1/tts`, header `model: drama-3` | Japanese voice, emotion via `[...]` directions |
| Foley | HunyuanVideo-Foley / ControlFoley / MMAudio | fal MMAudio etc. | Prompted e.g. "sizzling oil, knife on wooden board" |
| Stem split | Demucs / BS-RoFormer | — | Reuse real ambience where it still syncs |
| Mix/mux | ffmpeg (loudnorm, sidechain duck under voice) | — | |

### 2.2 Code layout (planned)

```
cooking2anime/
  c2a/
    cli.py               # c2a run | preview | approve | resume | status
    pipeline.py          # stage graph, caching, resume
    config.py            # pydantic schema for configs/*.yaml
    stages/
      analyze.py  clean.py  preview.py  stylize.py  post.py  audio.py  qa.py
    providers/           # one interface per capability, many backends
      base.py            # ImageStylizer, VideoStylizer, Inpainter, TTS, Foley, Upscaler
      comfyui.py         # local: submits workflow JSON to ComfyUI HTTP/WebSocket API
      fal.py  replicate.py  fish.py  openai_compat.py
    workflows/           # ComfyUI API-format workflow JSONs (VACE, Kontext, ...)
    review/              # static HTML review page for the approval gate
  configs/
    default.yaml
    styles/*.yaml        # style presets (prompt, negative, LoRA, realism)
  runs/<run_id>/         # all artifacts, manifest.json (git-ignored)
```

### 2.3 Key design decisions

- **Stages are pure functions over files.** Each stage reads from and writes to
  `runs/<id>/<stage>/` and records the input hash in `manifest.json`. Re-running
  skips anything already done, a crash resumes where it stopped, and editing the
  prompt only re-runs the stages that depend on it.
- **One interface per capability.** For example, `VideoStylizer.stylize(shot, ref_image,
  controls, prompt, seed) -> video`. Backends are chosen in config
  (`providers.video_stylizer: comfyui | fal | replicate`), so moving a stage between
  local and cloud is a one-line change. The same interface works with a remote
  ComfyUI (e.g. a rented GPU) by setting `comfyui.url`.
- **The ComfyUI API is the local engine.** It already handles VRAM offload, fp8/GGUF
  and model caching. The pipeline uploads inputs, patches node values in a workflow
  JSON template, queues the job and downloads the outputs. No manual clicking.
- **Human gate = a pause between stages.** `c2a run` stops after `preview` with status
  `awaiting_approval` and writes `review/index.html` (a grid of keyframes × seeds).
  The person picks one (or edits the prompt and reruns the preview), then runs
  `c2a approve <run> --pick 3`. The chosen prompt, seed, LoRA and reference image are
  locked into `style.lock.json`, and everything after that runs unattended. A small
  web UI can replace the CLI later.
- **Shots run in parallel, each chunk in sequence.** Local GPU: a single queue.
  Cloud: run several shots at once, up to a set limit.
- **QA loop.** Each shot gets: style similarity (DINOv2/CLIP vs approved keyframe),
  pose similarity (keypoint OKS vs source), flicker (warped-frame error), and an OCR
  text count. If a score fails, retry up to `qa.max_retries` with a new seed or
  stronger control; if it still fails, flag the shot in the final report.

---

## 3. Example config

See [`configs/default.yaml`](../configs/default.yaml).

---

## 4. Hardware & cost guide (rough)

| Setup | What runs locally | Notes |
|-------|-------------------|-------|
| 24 GB GPU (RTX 4090) | Everything except Drama 3: Wan VACE 14B fp8/GGUF at 720p (several minutes per 5 s chunk), MiniMax-Remover, Kontext/Qwen-Edit, MMAudio | Use the Fish API for voice, or S2-Pro locally |
| 32 GB+ (RTX 5090) / 48–80 GB | Same, faster; ProPainter at 720p (~25 GB) | Drama 3 locally via SGLang-Omni on large cards |
| Cloud only | All stages via fal/Replicate + Fish API | Cost scales with seconds of video; the preview gate keeps spend low before the expensive stage |

A 60 s video is about 12–15 chunks of 5 s each, plus retries. Plan for 1–3 hours on
one 4090, or minutes with parallel cloud jobs.

---

## 5. Implementation plan

| Milestone | Scope | Done when |
|-----------|-------|-----------|
| **M0 Skeleton** | CLI, config schema, run folder + manifest/caching, ffmpeg probe/split/mux, provider base classes | `c2a run in.mp4` produces a remuxed copy with every stage stubbed |
| **M1 Analyze** | Shot detection, keyframes, OCR + logo masks with SAM 2 tracking, DWPose/depth/canny control videos, WhisperX | Debug overlay video shows masks + pose |
| **M2 Clean** | Inpainter interface + MiniMax-Remover (local) + one cloud backend; OCR check afterwards | No text found on the cleaned output |
| **M3 Preview + gate** | Keyframe stylize via ComfyUI (Kontext/Qwen-Edit + LoRA), style presets, review HTML, `approve`, `style.lock.json` | A person can pick a style in under 2 minutes |
| **M4 Stylize** | Wan VACE workflow, 81-frame windows with overlap + continuation, color match, shot stitching, cloud backend | A 30 s test clip stylized end-to-end with no manual steps |
| **M5 Post** | RIFE, SeedVR2 for 1080p, grade/LUT/grain, encode presets (H.264/H.265, CRF) | 720p and 1080p outputs |
| **M6 Audio** | Demucs, foley V2A, LLM narration script, Fish Drama 3 (API + local), alignment, BGM, loudness mix | Final mp4 with synced sound at -14 LUFS |
| **M7 QA + scale** | Automatic scores, retry policy, final report, parallel cloud jobs, batch folder mode, optional web UI | Unattended batch of 5 videos with a report |

Build M0 → M3 first. That alone shows whether the style is good, before any money
or GPU time goes into M4.

---

## 6. Risks & mitigations

| Risk | Mitigation |
|------|------------|
| Flicker / style drift across chunks | Same reference image for every chunk of a shot, overlap blending, last-frame continuation, color matching, temporal upscaler |
| Hands/knife distorted | Hand keypoints + canny control, higher control strength on close-ups, shot-type-aware presets |
| Food loses appetizing look | `realism` setting + food LoRA; lower denoise on food regions (segmentation-masked) |
| Long run fails midway | Stage caching + `c2a resume` |
| Leftover text after stylize | OCR on final frames → targeted re-inpaint pass |
| Voice timing does not fit the shots | Script generated per shot with a character budget based on shot length; small time-stretch (≤8%) with ffmpeg `atempo` |
| License / rights | License flag per backend, warning when a non-commercial model is used with `commercial: true` |
