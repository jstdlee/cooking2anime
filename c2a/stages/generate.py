"""generate: first+last-frame video per chunk from the motion prompt, with QA and retries."""

from __future__ import annotations

import json
from pathlib import Path

from c2a.core import media
from c2a.core.context import Context, seed_for
from c2a.core.manifest import atomic_write_bytes, atomic_write_json, cache_key
from c2a.core.timeline import Chunk
from c2a.prompts.builder import build_prompt
from c2a.prompts.describe import ChunkDescription
from c2a.qa.metrics import flicker_proxy, ssim, style_sim_proxy
from c2a.stages.describe import desc_path, load_description
from c2a.stages.restyle import restyled_path

VERSION = 2
SIMPLIFY_AFTER = 2  # attempts; after this many failures the beats are simplified


def chunk_video(chunk: Chunk) -> str:
    return f"generate/{chunk.id}.mp4"


def chunk_strip(chunk: Chunk) -> str:
    return f"generate/{chunk.id}.strip.jpg"


def simplify_beats(d: ChunkDescription, max_words: int = 8) -> str:
    acts = [" ".join(b.action.split()[:max_words]) for b in d.beats]
    return ", then ".join(acts)


def _read_all(path: Path, size: tuple[int, int]) -> list[media.Frame]:
    return list(media.read_frames(path, size))


def _strip(frames: list[media.Frame], dst: Path, max_px: int) -> None:
    from PIL import Image

    picks = [frames[0], frames[len(frames) // 2], frames[-1]]
    tiles = [Image.fromarray(f) for f in picks]
    for t in tiles:
        t.thumbnail((max_px // 2, max_px // 2))
    w = sum(t.width for t in tiles)
    canvas = Image.new("RGB", (w, tiles[0].height))
    x = 0
    for t in tiles:
        canvas.paste(t, (x, 0))
        x += t.width
    import io
    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=80)
    atomic_write_bytes(dst, buf.getvalue())


def generate(ctx: Context) -> None:
    lock = ctx.style_lock()
    ref_img = media.load_png(ctx.run.path(lock["reference_image"]))
    q = ctx.cfg.qa
    overrides: dict[str, str] = ctx.run.read()["overrides"].get("chunks", {})
    size = ctx.gen_size
    prompts: list[dict[str, object]] = []
    flagged = 0
    for chunk in ctx.timeline.chunks:
        name = f"generate/{chunk.id}"
        d = load_description(ctx, chunk)
        first = ctx.run.path(restyled_path(chunk.first_kf))
        last = ctx.run.path(restyled_path(chunk.last_kf))
        override = overrides.get(chunk.id)
        built = build_prompt(d, ctx.style_prompt(), lock["negative"], ctx.preset,
                             beats_override=override)
        reruns = int(ctx.run.read().get("reruns", {}).get(chunk.id, 0))
        seed = seed_for(lock["seed"], "chunk", chunk.id, reruns)
        prompts.append({"chunk": chunk.id, "positive": built.positive,
                        "negative": built.negative, "tokens": built.tokens,
                        "dropped": list(built.dropped), "override": override,
                        "template_version": built.template_version})
        params = {"prompt": built.positive, "negative": built.negative, "seed": seed,
                  "frames": chunk.gen_frames, "fps": ctx.timeline.gen_fps,
                  "gen": ctx.stack.video_gen.info.name, "qa": q.model_dump(),
                  "retries": ctx.cfg.generate.max_retries}
        key = cache_key("generate", VERSION, params,
                        [first, last, ctx.run.path(desc_path(chunk))])
        if ctx.run.unit_is_fresh(name, key):
            continue
        rel = chunk_video(chunk)
        dst = ctx.run.path(rel)
        a, b = media.load_png(first), media.load_png(last)
        status, metrics, attempts = "flagged", {}, 0
        for attempt in range(ctx.cfg.generate.max_retries + 1):
            attempts = attempt + 1
            positive = built.positive
            if attempt >= SIMPLIFY_AFTER:
                positive = build_prompt(d, ctx.style_prompt(), lock["negative"], ctx.preset,
                                        beats_override=simplify_beats(d)).positive
            ctx.stack.video_gen.flf2v(first, last, chunk.gen_frames, ctx.timeline.gen_fps,
                                      positive, built.negative, seed + attempt, dst)
            frames = _read_all(dst, size)
            mid_png = ctx.run.path("generate", f"{chunk.id}.mid.png")
            media.save_png(frames[len(frames) // 2], mid_png)
            motion = ctx.stack.vlm.judge_motion(dst, [x.action for x in d.beats], seed)
            metrics = {
                "frames": len(frames),
                "boundary_ssim_first": round(ssim(frames[0], a), 4),
                "boundary_ssim_last": round(ssim(frames[-1], b), 4),
                "motion_match": round(motion, 4),
                "flicker_proxy": round(flicker_proxy(frames), 5),
                "style_sim_proxy": round(style_sim_proxy(frames[len(frames) // 2], ref_img), 4),
                "text_boxes": ctx.stack.ocr.count_text_boxes(mid_png, 0.6),
                "simplified": attempt >= SIMPLIFY_AFTER,
            }
            ok = (len(frames) == chunk.gen_frames
                  and metrics["boundary_ssim_first"] >= q.min_boundary_ssim
                  and metrics["boundary_ssim_last"] >= q.min_boundary_ssim
                  and metrics["motion_match"] >= q.min_motion_match
                  and metrics["text_boxes"] <= q.max_text_boxes)
            if ok:
                status = "done"
                _strip(frames, ctx.run.path(chunk_strip(chunk)), ctx.cfg.canvas.thumb_max_px)
                break
        if status == "flagged":
            flagged += 1
            _strip(_read_all(dst, size), ctx.run.path(chunk_strip(chunk)),
                   ctx.cfg.canvas.thumb_max_px)
        ctx.run.record_unit(name, key, status,
                            {"video": rel, "strip": chunk_strip(chunk)}, metrics, attempts)
    atomic_write_bytes(ctx.run.path("generate", "prompts.jsonl"),
                       "".join(json.dumps(p, sort_keys=True) + "\n" for p in prompts).encode())
    atomic_write_json(ctx.run.path("generate", "summary.json"),
                      {"chunks": len(ctx.timeline.chunks), "flagged": flagged})
    ctx.say(f"generate: {len(ctx.timeline.chunks)} chunks ({flagged} flagged)")

