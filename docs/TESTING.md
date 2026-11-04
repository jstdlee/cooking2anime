# cooking2anime — Test Rules

These rules are binding. A PR that breaks one does not merge.

## 1. Test layers

| Layer | Folder | Runs | Speed budget | What it proves |
|-------|--------|------|--------------|----------------|
| Unit | `tests/unit` | every PR | < 60 s total | Pure logic: config, manifest, cache keys, prompt builder, cue mapping, metrics maths |
| Property | `tests/property` | every PR (1k examples), nightly (10k) | < 90 s | Invariants: timeline, board merge, ownership |
| Contract | `tests/contract` | every PR | < 60 s | Each provider backend sends and parses the right wire format, using recorded HTTP/WS fixtures (no network) |
| Integration (fake) | `tests/integration` | every PR | < 4 min | The whole pipeline on fixture clips with fake providers |
| Board | `tests/board` | every PR (file level), nightly macOS (real GoDraw) | < 2 min | `.tldr` validity, simulated user edits, real-app e2e |
| GPU | `tests/gpu` | nightly on the self-hosted GPU runner, and before each release | < 2 h | Real models on fixture clips; VRAM and time budgets |
| Golden | `tests/golden` | weekly, before each release, and on PRs labelled `quality` | < 6 h | Quality metrics on the golden set vs the frozen baseline |

## 2. General rules

1. **No network in PR tests.** `pytest-socket` is on by default. Contract tests replay
   recorded fixtures. Re-record only with `--record` plus a reviewer note.
2. **Deterministic by default.** Every test seeds `random`, `numpy` and `torch`, and
   fake providers are pure functions of their inputs. A flaky test is quarantined
   within 24 h *and* gets an issue. A quarantined test still runs and reports, but
   only blocks nightly; it must be fixed or deleted within one week.
3. **Every bug fix comes with a regression test** that fails before the fix. The PR
   template has a checkbox for it.
4. **Coverage gates:** `c2a/core`, `c2a/board` and `c2a/prompts` ≥ 90% line and 85%
   branch; the whole repo ≥ 80%. A PR may not lower coverage of a file it touches.
5. **Types and lint:** `mypy --strict` and `ruff` pass with zero ignores added unless
   a reason is given inline.
6. **Snapshot tests** (prompts, board layout, report JSON) are updated only with
   `pytest --update-snapshots`. The PR must explain why the snapshot changed.
7. **Golden baselines** are frozen per release (`tests/golden/baseline/v*.json`). A
   change to a baseline needs its own PR, with before/after metrics and sample frames.
8. **TDD is required** for `core/timeline.py`, `board/sync.py`, `board/reader.py` and
   `prompts/builder.py`: the test is written first, in the same PR.

## 3. Required tests per area

### Timeline (`core/timeline.py`)
- Property: for any source duration in 1–180 s, fps in {23.976, 24, 25, 29.97, 30,
  50, 59.94, 60}, and any set of cuts:
  - The sum of chunk durations equals the source duration ± 1 output frame.
  - No chunk crosses a cut.
  - Every chunk length is `4n+1` and within [17, 81].
  - Boundary keyframes are shared by neighbouring chunks.
- Unit: VFR input → monotonic CFR timestamps; leftover-merge edge cases (a remainder
  of exactly 16, 17 and 18 frames).

### Manifest & cache (`core/manifest.py`, `core/cache.py`)
- Kill the process in the middle of a write (fault injection) → the manifest is either
  the old version or the new one, never corrupt.
- Changing one chunk's prompt invalidates exactly that chunk's generate, post and
  audio work (the cache-key graph is tested).

### Prompt builder (`prompts/builder.py`)
- Snapshots for every golden-clip chunk.
- Token budget: output ≤ 512 tokens on the real Wan tokenizer, with drops in the
  documented order; beats and emotion are never dropped.
- An override replaces only the beats sentence.

### VLM describe
- Contract: schema-invalid responses trigger a retry, and the stage fails after 2.
- Closed vocabularies: an unknown `camera`, `emotion` or `cue` value is rejected.

### Board (`board/*`)
- Every produced board passes `tools/tldr-validate` (pinned `@tldraw/tlschema`).
- Property: with random user records (shapes, arrows, notes, pages) mixed in, 100
  projector writes leave every non-engine record **byte-for-byte unchanged**.
- Optimistic write: if the file changes between read and write, the engine retries,
  and the user's change survives.
- Reader: sticker inside a candidate frame → approval; sticker on the page or in a
  non-candidate frame → ignored with a board notice; two stickers → ambiguous, so no
  approval and a notice; prompt note edited → override recorded with the board hash.
- Rebuild: deleting `board.tldr` and running `c2a board rebuild` gives the same
  engine-owned records.
- Real GoDraw (nightly, macOS, pinned commit): open → drag the sticker over the
  automation socket → approval within 3 s. A manual checklist is also signed off once
  per release.

### Providers
- Contract test per backend: request shape, auth header, retry on 429/5xx with
  backoff, timeout, output download, and the license flag.
- ComfyUI: every `workflows/*.json` has a `bindings.yaml`, and every bound node ID
  exists in the workflow (static check).
- `commercial_ok=False` together with `run.commercial: true` → the engine refuses.

### QA metrics (`qa/*`)
- Every metric has unit tests on synthetic inputs with known answers. Examples:
  identical frames → flicker 0; a frame shifted by known optical flow → expected warp
  error; a rendered "TEXT" image → OCR count ≥ 1; a −14 LUFS reference tone →
  −14 ± 0.1.
- Word guard: a speech clip fails, and a sigh or hum clip passes (fixtures checked in).

### Integration (fake)
- The fixture clips (CFR, VFR, vertical, with a cut, with burned-in text) all reach
  export. Then assert:
  - Duration within ±1 frame; resolution, codec and pixel format via `ffprobe`.
  - The source audio stream is gone; there is a new audio stream, or none in no-audio
    mode.
  - `report.json` is valid against its schema.
- Resume: kill after generate chunk 2 → `c2a run` again → finishes without
  re-running chunks 0–2.

## 4. GPU and golden tests

- **GPU runner:** a self-hosted GitHub Actions runner labelled `gpu-24g`, with models
  pinned by hash in `models.lock`. `c2a doctor --strict` must pass before the
  tests run.
- **Budgets checked nightly:** VRAM peak per stage, seconds per chunk (L = 81, 720p),
  cloud cost per output minute. A slowdown of more than 15% fails the job.
- **Golden set:** `data/golden/` (not in git; synced from storage with checksums).
  - 10 clips you own or that are CC0, covering hands-only, a face visible, steam/oil,
    fast chopping, a pan camera move, vertical video, and burned-in text.
  - Each clip has a reference description, plus human ratings from the last release.
- **Golden gates** (vs the frozen baseline):

| Metric | Gate |
|--------|------|
| Boundary SSIM | 100% of chunks ≥ 0.90 |
| Flagged chunks | ≤ 15% and no worse than baseline + 2 points |
| Style similarity (median) | ≥ baseline − 0.02 |
| Motion match (median) | ≥ baseline − 0.03 |
| OCR text boxes | 0 |
| Duration error | ≤ 1 frame |
| Voice-clip words | 0 |
| Loudness | −14 ± 1 LUFS, TP ≤ −1 dBTP |

- **Human evaluation per release:** 2 raters score each golden output (1–5) on
  motion likeness, emotion likeness, style, food appeal, sound sync and artifacts.
  The release needs a mean ≥ 4.0 on every axis and no axis lower than the previous
  release. Rubric: `tests/golden/RUBRIC.md`.

## 5. CI pipeline

```
PR:      ruff → mypy → unit → property(1k) → contract → integration-fake → board(file) → coverage gate
nightly: property(10k) → gpu → board(real GoDraw, macOS)
weekly / release / label:quality: golden → human-eval checklist (release only)
```
`main` is protected: every PR job is required, plus one review.
