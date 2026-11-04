# cooking2anime
transfer the cooking video to anime styling

Input a cooking video. Get it back as Japanese anime with the same length, cuts,
motion, gestures and emotion. It has no text or logos, no narration, and a new
soundtrack (foley kitchen sounds + Fish Audio Drama 3 voice sounds). 720p or 1080p.

One human step: approve the style on a **GoDraw** canvas. Everything else is automatic,
on a local GPU (ComfyUI) or in the cloud (fal + Fish API).

```
engine (c2a.core)  ←  CLI (c2a run / watch / approve / rerun …)
        ↕ runs/<id>/board.tldr
GoDraw canvas (review, drag ✓ to approve, edit prompt notes)
```

- Design: [docs/DESIGN.md](docs/DESIGN.md)
- Development plan: [docs/DEV_PLAN.md](docs/DEV_PLAN.md)
- Test rules: [docs/TESTING.md](docs/TESTING.md)
- Default config: [configs/default.yaml](configs/default.yaml)

Status: design phase. Implementation starts with the spikes in DEV_PLAN Phase 0.
