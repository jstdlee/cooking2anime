"""restyle: every checked key image in the locked style, with keyframe QA and retries."""

from __future__ import annotations

from c2a.core import media
from c2a.core.context import Context, seed_for
from c2a.core.manifest import cache_key
from c2a.qa.metrics import edge_ssim, style_sim_proxy
from c2a.stages.keys import key_image_path

VERSION = 2


def restyled_path(start: int) -> str:
    return f"restyle/k{start:06d}.png"


def restyle(ctx: Context) -> None:
    lock = ctx.style_lock()
    ref = ctx.run.path(lock["reference_image"])
    ref_img = media.load_png(ref)
    q = ctx.cfg.qa
    keys = ctx.checked_keys()
    flagged = 0
    shot_of = {k.start: next(s.index for s in ctx.shots if s.start <= k.start < s.end)
               for k in keys}
    for i, k in enumerate(keys):
        name = f"restyle/{k.start}"
        src = ctx.run.path(key_image_path(k.start))
        base_seed = seed_for(lock["seed"], "shot", shot_of[k.start])
        params = {"prompt": lock["prompt"], "negative": lock["negative"], "seed": base_seed,
                  "edit": ctx.stack.image_edit.info.name, "qa": q.model_dump(),
                  "retries": ctx.cfg.generate.max_retries}
        key = cache_key("restyle", VERSION, params, [src, ref])
        if ctx.run.unit_is_fresh(name, key):
            continue
        rel = restyled_path(k.start)
        dst = ctx.run.path(rel)
        src_img = media.load_png(src)
        metrics: dict[str, float | int] = {}
        status, attempts = "flagged", 0
        for attempt in range(ctx.cfg.generate.max_retries + 1):
            attempts = attempt + 1
            ctx.stack.image_edit.edit(src, [ref], lock["prompt"], lock["negative"],
                                      base_seed + attempt, dst)
            out = media.load_png(dst)
            text_boxes = ctx.stack.ocr.count_text_boxes(dst, 0.6)
            metrics = {"structure_ssim": round(edge_ssim(out, src_img), 4),
                       "style_sim_proxy": round(style_sim_proxy(out, ref_img), 4),
                       "text_boxes": text_boxes}
            if metrics["structure_ssim"] >= q.min_structure_ssim and \
                    text_boxes <= q.max_text_boxes:
                status = "done"
                break
        flagged += status == "flagged"
        ctx.run.record_unit(name, key, status, {"image": rel}, metrics, attempts)
        ctx.tick("restyle", f"k{k.start}", (i + 1) / len(keys))
    ctx.say(f"restyle: {len(keys)} key images ({flagged} flagged)")
