"""preview: sample checked key images × seeds in the chosen style; approve locks one."""

from __future__ import annotations

import json
import shutil
import time
from typing import Any

from c2a.core.context import Context, effective_config, seed_for
from c2a.core.errors import GateError
from c2a.core.manifest import Run, atomic_write_json, cache_key, file_digest
from c2a.prompts.style import CLEAN_IMAGE, StyleSelection, compose
from c2a.stages.keys import key_image_path

VERSION = 2


def pick_samples(starts: list[int], n: int) -> list[int]:
    """n key images spread evenly over the video (first, last, and in between)."""
    if len(starts) <= n:
        return starts
    if n == 1:
        return [starts[len(starts) // 2]]
    return sorted({starts[round(i * (len(starts) - 1) / (n - 1))] for i in range(n)})


def preview(ctx: Context) -> None:
    if approval_is_current(ctx.run):
        return  # the style is locked; changing keys must not cost a new sample grid
    pc = ctx.cfg.style.preview
    style = ctx.style()
    prompt = f"{style.image_prompt}, {CLEAN_IMAGE}"
    samples = pick_samples([k.start for k in ctx.checked_keys()], pc.sample_keys)
    inputs = [ctx.run.path(key_image_path(s)) for s in samples]
    params = {"prompt": prompt, "negative": style.negative, "samples": samples,
              "k": pc.seeds_per_key, "edit": ctx.stack.image_edit.info.name}
    key = cache_key("preview", VERSION, params, inputs)
    if ctx.run.unit_is_fresh("preview", key):
        return
    ctx.say(f"preview: {len(samples)} key images × {pc.seeds_per_key} variations")
    cands: list[dict[str, Any]] = []
    outputs: dict[str, str] = {}
    for si, start in enumerate(samples):
        for k in range(pc.seeds_per_key):
            seed = seed_for("preview", start, k, style.key)
            rel = f"preview/s{si}_k{k}.png"
            ctx.stack.image_edit.edit(ctx.run.path(key_image_path(start)), [], prompt,
                                      style.negative, seed, ctx.run.path(rel))
            cands.append({"id": f"{si}:{k}", "sample": si, "seed_index": k, "key": start,
                          "seed": seed, "image": rel})
            outputs[f"s{si}_k{k}"] = rel
            ctx.tick("preview", f"{si}:{k}", len(cands) / (len(samples) * pc.seeds_per_key))
    atomic_write_json(ctx.run.path("preview", "candidates.json"), {
        "prompt": prompt, "negative": style.negative, "style_key": style.key,
        "layers": style.layers, "candidates": cands})
    outputs["candidates"] = "preview/candidates.json"
    ctx.run.record_unit("preview", key, "done", outputs, {"candidates": len(cands)})


def candidates(run: Run) -> dict[str, Any]:
    p = run.path("preview", "candidates.json")
    if not p.exists():
        raise GateError("no style samples yet; run the pipeline first")
    d: dict[str, Any] = json.loads(p.read_text())
    return d


def current_style_key(run: Run) -> str:
    cfg = effective_config(run)
    return compose(StyleSelection.model_validate(cfg.style.selection.model_dump())).key


def approval_is_current(run: Run) -> bool:
    """Approved, and for the style that is selected now (a new style needs a new approval)."""
    p = run.path("style.lock.json")
    if not p.exists() or run.read().get("approval") is None:
        return False
    return bool(json.loads(p.read_text()).get("style_key") == current_style_key(run))


def approve(run: Run, pick: str, via: str = "cli") -> dict[str, Any]:
    """Lock sample `pick` ("sample:variation") as the style for every clip."""
    data = candidates(run)
    if data.get("style_key") != current_style_key(run):
        raise GateError("the samples are for a different style; let the preview finish first")
    match = [c for c in data["candidates"] if c["id"] == pick]
    if not match:
        ids = ", ".join(c["id"] for c in data["candidates"])
        raise GateError(f"unknown sample {pick!r}; choose one of: {ids}")
    c = match[0]
    ref = run.path("style_ref.png")
    shutil.copyfile(run.path(c["image"]), ref)
    lock = {
        "version": 2, "prompt": data["prompt"], "negative": data["negative"],
        "style_key": data["style_key"], "layers": data["layers"], "seed": c["seed"],
        "candidate": c["id"], "key": c["key"], "reference_image": "style_ref.png",
        "reference_digest": file_digest(ref), "approved_via": via, "approved_at": time.time(),
    }
    atomic_write_json(run.path("style.lock.json"), lock)
    with run.update() as m:
        m["approval"] = {"candidate": c["id"], "via": via, "t": lock["approved_at"],
                         "style_key": data["style_key"]}
    return lock


def clear_approval(run: Run) -> None:
    with run.update() as m:
        m["approval"] = None
    run.path("style.lock.json").unlink(missing_ok=True)
