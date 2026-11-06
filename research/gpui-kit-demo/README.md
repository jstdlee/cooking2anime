# c2a-review — GPUI Kit research demo

A native review window for c2a runs, built with [GPUI Kit](https://gpui-kit.com/).
It covers video playback and review, a key-image and style-sample canvas, and AI
operations. Findings and recommendation: [NOTES.md](NOTES.md).

## Run

```bash
# Linux build deps (Debian/Ubuntu)
sudo apt-get install -y libxkbcommon-dev libxkbcommon-x11-dev libasound2-dev mesa-vulkan-drivers ffmpeg

# 1. Start the engine's Studio API (any existing run works)
export C2A_STUDIO_TOKEN=demo
c2a studio --port 8799            # from the repo root

# 2. Open a run in the native review window
cd research/gpui-kit-demo
cargo run --release -- --run-dir ../../runs/<run-id> --studio http://127.0.0.1:8799 --token demo
```

Without `--studio`, the window opens read-only: it plays the run's source and output
media, but shows no engine state and sends no commands.

Headless (as in CI): `Xvfb :99 -screen 0 1280x800x24 & DISPLAY=:99 cargo run …`. Rendering
falls back to Mesa lavapipe (software Vulkan).

## Test

```bash
cargo test     # view-model parsing, output→source time mapping, frame retirement
```

## Layout

- `src/main.rs` — the window: review panes, transport, canvas, AI operations panel
- `src/studio.rs` — client for `c2a studio` (`GET /api/runs/{id}`, `POST …/commands`)
- `src/video/` — video element and player, ported from `alhazen-gpui` 0.7.1 (Apache-2.0) onto `alhazen-core` so it runs on gpui-kit's GPUI
