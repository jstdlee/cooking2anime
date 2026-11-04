"""keys: key image candidates per shot (DESIGN_V2 §2.1) + the person's added keys."""

from __future__ import annotations

from fractions import Fraction

import numpy as np

from c2a.core import media
from c2a.core.context import Context
from c2a.core.manifest import atomic_write_json, cache_key
from c2a.keys.detect import Candidate, DetectParams, detect_shot, sharpness

VERSION = 1


def key_image_path(start: int) -> str:
    return f"keys/img_{start:06d}.png"


def keys(ctx: Context) -> None:
    kc = ctx.cfg.keys
    added = sorted(int(f) for f in ctx.run.read().get("keys_user", {}).get("added", []))
    params = {"keys": kc.model_dump(), "added": added, "gen": ctx.gen_size,
              "max_clip_s": ctx.cfg.clips.max_clip_s}
    key = cache_key("keys", VERSION, params, [ctx.normalized])
    if ctx.run.unit_is_fresh("keys", key):
        return
    ctx.say("keys: finding key images")
    fps = ctx.src_fps
    gray = np.stack(list(media.read_gray(ctx.normalized, kc.analysis_width)))
    dp = DetectParams(sensitivity=kc.sensitivity, min_gap_s=kc.min_gap_s,
                      max_gap_s=min(kc.max_gap_s, ctx.cfg.clips.max_clip_s),
                      refine_s=kc.refine_s)
    found: list[Candidate] = []
    for shot in ctx.shots:
        found += detect_shot(gray[shot.start:shot.end], shot, fps, dp)
        ctx.tick("keys", f"shot{shot.index}", (shot.index + 1) / len(ctx.shots))
    starts = {c.start for c in found}
    refine = round(kc.refine_s * float(fps))
    for f in added:
        if f in starts or not 0 <= f < len(gray):
            continue
        shot = next(s for s in ctx.shots if s.start <= f < s.end)
        lo, hi = f, min(shot.end - 1, f + refine)
        sharp = [sharpness(gray[i].astype(np.float64)) for i in range(lo, hi + 1)]
        img = lo + int(np.argmax(sharp))
        found.append(Candidate(f, img, 0.0, ["added by you"], False))
        starts.add(f)
    found.sort(key=lambda c: c.start)

    frames = media.extract_frames(ctx.normalized, [c.image for c in found], ctx.gen_size)
    outputs = {"candidates": "keys/candidates.json"}
    for c in found:
        rel = key_image_path(c.start)
        media.save_png(frames[c.image], ctx.run.path(rel))
        outputs[f"img_{c.start}"] = rel
    atomic_write_json(ctx.run.path("keys", "candidates.json"), {"candidates": [
        {"start": c.start, "image": c.image, "score": round(c.score, 4), "reasons": c.reasons,
         "locked": c.locked, "user_added": c.start in added,
         "t": float(Fraction(c.start) / fps)} for c in found]})
    ctx.run.record_unit("keys", key, "done", outputs, {"candidates": len(found)})
    ctx.say(f"keys: {len(found)} key image candidates in {len(ctx.shots)} shots")
