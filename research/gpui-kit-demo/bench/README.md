# Canvas stress benchmark

The same scripted session runs on both stacks: GPUI Kit (`src/bin/canvas_bench.rs`) and
tldraw 5.6 (`web/src/main.jsx`).

- **Setup:** N image cards (500 distinct 384×216 thumbnails) on a 1280×800 window, with a
  1080p30 H.264 video playing in an overlay.
- **Script:** 3 s warm-up, then five phases of 4 s each:
  1. **idle**
  2. **pan**
  3. **zoom**, down to 0.08×, with most cards on screen
  4. **drag** 200 selected cards
  5. **duplicate** those 200 cards 8 times, adding 1600 cards
- **Measured, per phase:**
  - frame intervals: fps, p95, worst frame, frames over 33 ms;
  - CPU per thread group. *UI thread* means GPUI's main thread, or Chromium's renderer
    main thread (JS, React, DOM). *Raster* means Mesa llvmpipe for GPUI, or the GPU
    process and compositor for Chromium.
  - process memory (RSS).

```bash
# assets
mkdir -p assets && cd assets
ffmpeg -f lavfi -i "mandelbrot=s=384x216:rate=10" -frames:v 250 m%d.png
ffmpeg -f lavfi -i "testsrc2=s=384x216:rate=10" -frames:v 250 s%d.png
i=0; for f in m*.png s*.png; do mv "$f" "t$i.png"; i=$((i+1)); done
ffmpeg -f lavfi -i "testsrc2=s=1920x1080:r=30" -t 40 -c:v libx264 -pix_fmt yuv420p clip.mp4
cd ..
# builds
cargo build --release --bin canvas_bench
(cd bench/web && npm install && BENCH_ASSETS=$PWD/../../assets npx vite build && npm run preview &)
# run (one JSON line per configuration)
bench/run-all.sh assets
```

Results from the cloud container (4 vCPU, no GPU) are in
`results-container-2026-11-06.jsonl` and summarised in `../NOTES.md`.
