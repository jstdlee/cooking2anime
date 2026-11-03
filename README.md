# cooking2anime
transfer the cooking video to anime styling

Pipeline (fully prompt-driven): input video → shots & keyframes → VLM writes a
motion/emotion prompt per shot → anime style preview (one human approval) →
first-last-frame video generation (text/logos excluded by prompt) →
upscale/finish (720p / 1080p) → Fish Audio voice + foley.

- Design & plan: [docs/DESIGN.md](docs/DESIGN.md)
- Example config: [configs/default.yaml](configs/default.yaml)
