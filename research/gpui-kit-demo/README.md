# c2a Review — GPUI Kit research app

A native desktop app for c2a, built with [GPUI Kit](https://gpui-kit.com/): a start screen
with your runs ([screenshot](docs/start-screen.png)), new runs from a video
([screenshot](docs/new-run.png)), and for each run video review, a key-image and
style-sample canvas, and AI operations ([screenshot](docs/review.png)). Findings and
recommendation: [NOTES.md](NOTES.md).

## Run

A desktop app: open it from the app menu (after `./install.sh` in the release folder) or run
`c2a-review`. It starts the c2a engine (`c2a studio`) itself and shows the start screen:

- **Runs** in the runs folder (default `~/c2a/runs`; "Change folder…"), newest first, with
  state and first key image. Click one to review it; "← Runs" goes back.
- **New run from video…** uploads a video to the engine, which finds key images and makes
  style samples; the run opens and fills in live. Approve a sample to generate.
- **Open run folder…**, **Locate c2a…** (if the engine isn't found automatically), and a path
  box for desktops without a file-dialog portal.

The engine is found as: the setting or `--c2a`, `$C2A_BIN`, next to the app, `$PATH`, or an
unpacked `c2a-*` release folder beside the app or in your home folder. It stops when the app
quits (and if the app crashes). Settings: `~/.config/c2a-review/settings.json`, e.g.
`{"engine_args": ["--stack", "cloud"]}`.

```bash
# Linux build deps (Debian/Ubuntu)
sudo apt-get install -y libxkbcommon-dev libxkbcommon-x11-dev libasound2-dev mesa-vulkan-drivers ffmpeg nasm
cd research/gpui-kit-demo
cargo run --release                                   # start screen, engine started for you
cargo run --release -- --run-dir ../../runs/<run-id>  # straight into one run
cargo run --release -- --studio http://127.0.0.1:8799 --token demo   # use a running engine
```

Release builds for Linux arm64: GitHub releases tagged `gpui-demo-v*` (built by
`.github/workflows/build-gpui-demo-arm64.yml`; bump `version` in `Cargo.toml` to publish).

## Test

```bash
cargo test     # view model, time mapping, run list, settings, engine lookup, frame retirement
```

## Layout

- `src/main.rs` — the window: start screen (runs, new run, engine) and the run view (review panes, transport, canvas, AI operations panel)
- `src/engine.rs` — finds, starts and stops `c2a studio`
- `src/runs.rs` — the runs in a runs folder, read from their manifests
- `src/settings.rs` — `~/.config/c2a-review/settings.json`
- `packaging/` — app-menu installer and icon (shipped in the release archive)
- `src/studio.rs` — client for `c2a studio` (`GET /api/runs/{id}`, `POST …/commands`)
- `src/video/` — video element and player, ported from `alhazen-gpui` 0.7.1 (Apache-2.0) onto `alhazen-core` so it runs on gpui-kit's GPUI
