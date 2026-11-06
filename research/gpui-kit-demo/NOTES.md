# Research: GPUI Kit for a native c2a review app

Branch `claude/research-gpui-kit-demo`. Question: can [GPUI Kit](https://gpui-kit.com/)
(Rust, GPU-drawn UI) host the three things c2a Studio does — **play and review video**,
**an image canvas** of key images and style samples, and **AI operations** on the engine?

**Answer: yes, for all three.** The demo (`c2a-review`) runs against a real `c2a studio`
and drives the engine with the same typed commands the web Studio uses. Video was
the only hard part, and it needed a small port (below).

![review](docs/review.png)

## What the demo does

| Area | Built with | Verified |
|------|-----------|----------|
| Play / review | Two synced players (source, anime output), Play/Pause, a scrub slider, per-clip seek buttons. Output time is mapped to source time per clip (clips may be shorter or longer than their source span). | Plays under Xvfb, the slider follows playback, and the source re-seeks when it drifts more than 0.3 s from the clip mapping |
| Image canvas | Key-image cards (row 1, tags shown) and style-sample cards (row 2) on a pan/zoom surface: drag to pan, wheel zooms about the cursor, click selects | Selecting a card fills the AI-operations panel |
| AI operations | Check or uncheck a key image (`toggle_key`), approve a sample (`approve`), director preset (`set_style`), sound mode (`set_audio`), re-run a clip (`rerun`) | Checking the 0.90 s key made the engine re-plan from 2 to 3 clips and start generating ([screenshot](docs/ai-operation.png)) |

The demo reimplements no engine logic. It reads `GET /api/runs/{id}` (the same view model
the tldraw Studio draws) every 1.5 s, reads media files from the run folder, and
sends `POST /api/runs/{id}/commands`. These calls go through `c2a.app`, as the CLI does.

## Findings

1. **GPUI Kit 0.9.0** (Apache-2.0) is a facade over **`gpui-pre =0.3.11`**:
   - `gpui_kit::*` re-exports GPUI;
   - components live under `gpui_kit::component::*`;
   - `gpui_kit::open_window` wraps the root view.

   The component set covers everything this app needs: Button, Slider, Checkbox,
   scroll areas, theming. Gaps were small:
   - `Disableable` and `Selectable` must be imported to call `.disabled()` and `.selected()`;
   - `on_scroll` / `ScrollEvent` replace the deprecated wheel API;
   - `paint_image` takes the bounds first.
2. **No video player ships with GPUI Kit.** Every published GPUI video crate pins a
   different GPUI, so none links next to gpui-kit 0.9:

   | Crate | GPUI it pins |
   |-------|--------------|
   | `alhazen-gpui` 0.7.1 | `gpui` 0.2.2 |
   | `alhazen-gpui-pre` | `gpui-pre =0.3.8` |
   | `alhazen-ms-gpui` | `gpui-pre =0.3.10` |

   **Fix used:** a port of `alhazen-gpui`'s element and player, about 570 lines in
   `src/video/` (Apache-2.0, attribution kept), onto the UI-agnostic `alhazen-core 0.7.1`.
   The port must be bumped each time GPUI Kit bumps gpui-pre. **This is the main maintenance risk.**
3. **Decoding.** `alhazen-core` decodes H.264 on Linux through the system `ffmpeg`
   (feature `ffmpeg-cli`), or NVDEC when present. c2a already requires ffmpeg, so this
   adds nothing new. Audio goes through ALSA. In a container with no sound card, it logs
   "Unknown PCM default" and plays silently.
4. **Linux build.** Needs:
   - `libxkbcommon-dev`, `libxkbcommon-x11-dev`, `libasound2-dev`;
   - a Vulkan driver at run time.

   The first build of the dependency tree takes several minutes and `target/` is about
   1.5 GB. The incremental rebuild of the app itself takes a few seconds.
5. **Headless and CI.** It renders under `Xvfb` with Mesa lavapipe (`mesa-vulkan-drivers`,
   software Vulkan). That is enough for screenshot tests in GitHub Actions. Clicks were
   scripted with `xdotool`.
6. **Bug found and fixed.** `cx.active_window()` returns `None` when the window is not
   focused, which is always the case under Xvfb without a window manager. The slider then
   never moved. Fix: run the playback tick with `spawn_in(window)` / `update_in`, so it
   always gets its own window.

## Compared with the tldraw Studio (v0.2.0)

| | GPUI Kit native app | tldraw web Studio |
|---|---|---|
| Video | Frame-accurate GPU decode and drawing. Two synced players are easy, and long clips stay smooth. | `<video>` elements in shapes. Fine, but sync and frame stepping rely on browser seeking. |
| Canvas | Built by hand: pan, zoom and cards are about 100 lines. Arrows, free drawing, notes, undo and multiplayer would all have to be written. | All of that comes free (arrows, notes, undo, persistence). |
| Distribution | One native binary per OS. Linux arm64 needs a Vulkan driver. | Any browser; ships inside the `c2a` binary. |
| Tests | Rust unit tests, plus Xvfb screenshot tests | Playwright e2e, already in CI |
| Maintenance | The video port follows gpui-pre bumps. GPUI Kit is pre-1.0. | tldraw is mature, but licensing needs a key in production (ADR-006). |

## Recommendation

**Keep tldraw as the Studio.** It covers the free-form board (notes, arrows, layout) that
GPUI would make us rebuild.

**Adopt GPUI Kit only for a focused native "review" window**, launched as `c2a review` next
to the Studio, if users need frame-accurate side-by-side playback or long 1080p clips the
browser struggles with. It costs little, because all logic stays in the engine and both
front ends speak the same `/api/runs` contract.

Before adopting it:
- move the video port into its own crate, pinned with GPUI Kit;
- add an Xvfb + lavapipe screenshot job to CI;
- check macOS (Metal) and Windows builds.
