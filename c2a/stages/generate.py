"""generate: one clip per checked key image (image-to-video, end-anchored when possible)."""

from __future__ import annotations

import io
import json
from pathlib import Path

from c2a.core import media
from c2a.core.context import Context, seed_for
from c2a.core.manifest import atomic_write_bytes, atomic_write_json, cache_key
from c2a.core.timeline import Segment, timeline_to_json
from c2a.prompts.builder import build_clip_prompt
from c2a.qa.metrics import flicker_proxy, ssim, style_sim_proxy
from c2a.stages.restyle import restyled_path

VERSION = 3


def clip_video(seg: Segment) -> str:
    return f"generate/{seg.id}.mp4"


def clip_strip(seg: Segment) -> str:
    return f"generate/{seg.id}.strip.jpg"


def clip_poster(seg: Segment) -> str:
    return f"generate/{seg.id}.poster.jpg"


def _strip(frames: list[media.Frame], dst: Path, poster: Path, max_px: int = 512) -> None:
    from PIL import Image

    tiles = [Image.fromarray(f) for f in (frames[0], frames[len(frames) // 2], frames[-1])]
    for t in tiles:
        t.thumbnail((max_px // 2, max_px // 2))
    canvas = Image.new("RGB", (sum(t.width for t in tiles), tiles[0].height))
    x = 0
    for t in tiles:
        canvas.paste(t, (x, 0))
        x += t.width
    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=80)
    atomic_write_bytes(dst, buf.getvalue())
    p = Image.fromarray(frames[0])
    p.thumbnail((max_px, max_px))
    buf = io.BytesIO()
    p.save(buf, format="JPEG", quality=82)
    atomic_write_bytes(poster, buf.getvalue())


def generate(ctx: Context) -> None:
    lock = ctx.style_lock()
    ref_img = media.load_png(ctx.run.path(lock["reference_image"]))
    style = ctx.style()
    q = ctx.cfg.qa
    tl = ctx.timeline()
    atomic_write_json(ctx.run.path("generate", "timeline.json"), timeline_to_json(tl))
    size = ctx.gen_size
    keys = {k.start: k for k in ctx.checked_keys()}
    reruns: dict[str, int] = ctx.run.read().get("reruns", {})
    prompts: list[dict[str, object]] = []
    flagged = 0
    for seg in tl.segments:
        name = f"generate/{seg.id}"
        k = keys.get(seg.start) or next(v for v in keys.values() if v.image == seg.key)
        if k.tags is None:
            raise ValueError(f"key image {k.start} has no tags")
        built = build_clip_prompt(k.tags, style, motion_override=k.motion_override)
        first = ctx.run.path(restyled_path(k.start))
        nxt = next((v for v in keys.values() if v.image == seg.next_key), None) \
            if seg.anchored else None
        last = ctx.run.path(restyled_path(nxt.start)) if nxt else None
        seed = seed_for(lock["seed"], "clip", seg.id, reruns.get(seg.id, 0))
        prompts.append({"clip": seg.id, "key": k.start, "positive": built.positive,
                        "negative": built.negative, "camera": built.camera,
                        "tokens": built.tokens, "dropped": list(built.dropped),
                        "override": k.motion_override, "clip_s": str(seg.clip_s),
                        "anchored": last is not None})
        params = {"prompt": built.positive, "negative": built.negative, "seed": seed,
                  "frames": seg.gen_frames, "fps": tl.gen_fps, "anchored": last is not None,
                  "gen": ctx.stack.video_gen.info.name, "qa": q.model_dump(),
                  "retries": ctx.cfg.generate.max_retries}
        key = cache_key("generate", VERSION, params, [first] + ([last] if last else []))
        if ctx.run.unit_is_fresh(name, key):
            continue
        rel = clip_video(seg)
        dst = ctx.run.path(rel)
        a = media.load_png(first)
        b = media.load_png(last) if last else None
        status, metrics, attempts = "flagged", {}, 0
        frames: list[media.Frame] = []
        for attempt in range(ctx.cfg.generate.max_retries + 1):
            attempts = attempt + 1
            if last is not None:
                ctx.stack.video_gen.flf2v(first, last, seg.gen_frames, tl.gen_fps,
                                          built.positive, built.negative, seed + attempt, dst)
            else:
                ctx.stack.video_gen.i2v(first, seg.gen_frames, tl.gen_fps, built.positive,
                                        built.negative, seed + attempt, dst)
            frames = list(media.read_frames(dst, size))
            mid = ctx.run.path("generate", f"{seg.id}.mid.png")
            media.save_png(frames[len(frames) // 2], mid)
            motion = ctx.stack.vlm.judge_motion(dst, [built.positive], seed)
            metrics = {
                "frames": len(frames),
                "boundary_ssim_first": round(ssim(frames[0], a), 4),
                "boundary_ssim_last": round(ssim(frames[-1], b), 4) if b is not None else None,
                "motion_match": round(motion, 4),
                "flicker_proxy": round(flicker_proxy(frames), 5),
                "style_sim_proxy": round(style_sim_proxy(frames[len(frames) // 2], ref_img), 4),
                "text_boxes": ctx.stack.ocr.count_text_boxes(mid, 0.6),
            }
            last_ok = b is None or ssim(frames[-1], b) >= q.min_boundary_ssim
            ok = (len(frames) == seg.gen_frames
                  and ssim(frames[0], a) >= q.min_boundary_ssim and last_ok
                  and motion >= q.min_motion_match
                  and int(metrics["text_boxes"] or 0) <= q.max_text_boxes)
            if ok:
                status = "done"
                break
        flagged += status == "flagged"
        _strip(frames, ctx.run.path(clip_strip(seg)), ctx.run.path(clip_poster(seg)))
        ctx.run.record_unit(name, key, status, {"video": rel, "strip": clip_strip(seg),
                                                "poster": clip_poster(seg)}, metrics, attempts)
        ctx.tick("generate", seg.id, (seg.index + 1) / len(tl.segments))
    atomic_write_bytes(ctx.run.path("generate", "prompts.jsonl"),
                       "".join(json.dumps(p, sort_keys=True) + "\n" for p in prompts).encode())
    ctx.say(f"generate: {len(tl.segments)} clips ({flagged} flagged), "
            f"{float(tl.out_duration):.2f} s of output")
