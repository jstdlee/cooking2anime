"""preview: restyle N sample keyframes × K seeds; approve: lock one candidate as the style."""

from __future__ import annotations

import json
import shutil
import time
from typing import Any

import numpy as np

from c2a.core import media
from c2a.core.context import Context, seed_for
from c2a.core.errors import GateError
from c2a.core.manifest import Run, atomic_write_json, cache_key, file_digest
from c2a.qa.metrics import _hsv_hist
from c2a.stages.analyze import kf_path

VERSION = 1


def pick_samples(ctx: Context, n: int) -> list[int]:
    """Farthest-point sampling over keyframe colour signatures, one per shot first."""
    tl = ctx.timeline
    shot_of: dict[int, int] = {}
    for c in tl.chunks:
        shot_of.setdefault(c.first_kf, c.shot)
        shot_of.setdefault(c.last_kf, c.shot)
    kfs = tl.keyframes
    sig = {f: _hsv_hist(media.load_png(ctx.run.path(kf_path(ctx, f)))) for f in kfs}
    mid = tl.src_frames / 2
    chosen = [min(kfs, key=lambda f: (abs(f - mid), f))]
    while len(chosen) < min(n, len(kfs)):
        used_shots = {shot_of[f] for f in chosen}
        pool = [f for f in kfs if f not in chosen and shot_of[f] not in used_shots]
        if not pool:
            pool = [f for f in kfs if f not in chosen]

        def dist(f: int) -> float:
            return min(1 - float(np.dot(sig[f], sig[c])) for c in chosen)

        chosen.append(max(pool, key=lambda f: (dist(f), -f)))
    return sorted(chosen)


def preview(ctx: Context) -> None:
    pcfg = ctx.cfg.style.preview
    prompt = f"{ctx.style_prompt().strip()}. {ctx.cfg.style.clean_prompt}"
    negative = f"{ctx.preset.negative.strip()}, {ctx.cfg.style.negative.strip()}"
    samples = pick_samples(ctx, pcfg.sample_keyframes)
    inputs = [ctx.run.path(kf_path(ctx, f)) for f in samples]
    params = {"prompt": prompt, "negative": negative, "samples": samples,
              "k": pcfg.seeds_per_keyframe, "edit": ctx.stack.image_edit.info.name}
    key = cache_key("preview", VERSION, params, inputs)
    if ctx.run.unit_is_fresh("preview", key):
        return
    ctx.say(f"preview: {len(samples)} keyframes × {pcfg.seeds_per_keyframe} seeds")
    candidates: list[dict[str, Any]] = []
    outputs: dict[str, str] = {}
    for si, f in enumerate(samples):
        for k in range(pcfg.seeds_per_keyframe):
            seed = seed_for("preview", f, k)
            rel = f"preview/s{si}_k{k}.png"
            ctx.stack.image_edit.edit(ctx.run.path(kf_path(ctx, f)), [], prompt, negative,
                                      seed, ctx.run.path(rel))
            candidates.append({"id": f"{si}:{k}", "sample": si, "seed_index": k, "kf": f,
                               "seed": seed, "image": rel})
            outputs[f"s{si}_k{k}"] = rel
    atomic_write_json(ctx.run.path("preview", "candidates.json"),
                      {"prompt": prompt, "negative": negative, "candidates": candidates})
    outputs["candidates"] = "preview/candidates.json"
    ctx.run.record_unit("preview", key, "done", outputs, {"candidates": len(candidates)})


def candidates(run: Run) -> dict[str, Any]:
    p = run.path("preview", "candidates.json")
    if not p.exists():
        raise GateError("no preview candidates yet; run the pipeline first")
    d: dict[str, Any] = json.loads(p.read_text())
    return d


def approve(run: Run, pick: str, via: str = "cli") -> dict[str, Any]:
    """Lock candidate `pick` ("sample:seed_index") as the style for the whole video."""
    data = candidates(run)
    match = [c for c in data["candidates"] if c["id"] == pick]
    if not match:
        ids = ", ".join(c["id"] for c in data["candidates"])
        raise GateError(f"unknown candidate {pick!r}; choose one of: {ids}")
    c = match[0]
    ref = run.path("style_ref.png")
    shutil.copyfile(run.path(c["image"]), ref)
    lock = {
        "version": 1,
        "prompt": data["prompt"],
        "negative": data["negative"],
        "seed": c["seed"],
        "candidate": c["id"],
        "keyframe": c["kf"],
        "reference_image": "style_ref.png",
        "reference_digest": file_digest(ref),
        "approved_via": via,
        "approved_at": time.time(),
    }
    atomic_write_json(run.path("style.lock.json"), lock)
    with run.update() as m:
        m["approval"] = {"candidate": c["id"], "via": via, "t": lock["approved_at"]}
    return lock


def clear_approval(run: Run) -> None:
    with run.update() as m:
        m["approval"] = None
    run.path("style.lock.json").unlink(missing_ok=True)
