# cooking2anime
transfer the cooking video to anime styling

Pipeline (fully prompt-driven): input video → shots & keyframes → VLM writes a
motion/emotion prompt per shot → anime style preview (one human approval) →
first-last-frame video generation (text/logos excluded by prompt) →
upscale/finish (720p / 1080p) → original sound removed, new soundtrack
(foley kitchen sounds + Fish Audio Drama 3 voice sounds, no narration).

Interface: one Python pipeline engine, driven by a CLI and a local web GUI.

- Design & plan: [docs/DESIGN.md](docs/DESIGN.md)
- Example config: [configs/default.yaml](configs/default.yaml)
