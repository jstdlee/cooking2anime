"""tag: the VLM tags every key image candidate (DESIGN_V2 §2.2)."""

from __future__ import annotations

from fractions import Fraction

from c2a.core import media
from c2a.core.context import Context, seed_for
from c2a.core.errors import StageError
from c2a.core.manifest import atomic_write_json, cache_key
from c2a.keys.state import candidates
from c2a.prompts.tags import KeyTags, tag_instructions, validate_tags
from c2a.stages.keys import key_image_path

VERSION = 1
MAX_INVALID_RETRIES = 2


def tag(ctx: Context) -> None:
    cands = candidates(ctx.run)
    fps = ctx.src_fps
    schema = KeyTags.model_json_schema()
    ends = [int(c["start"]) for c in cands[1:]] + [ctx.src_frames]
    for i, (c, end) in enumerate(zip(cands, ends, strict=True)):
        start = int(c["start"])
        shot = next(s for s in ctx.shots if s.start <= start < s.end)
        end = min(end, shot.end)
        name = f"tag/{start}"
        img = ctx.run.path(key_image_path(start))
        params = {"tag": ctx.cfg.tag.model_dump(), "vlm": ctx.stack.vlm.info.name, "end": end}
        key = cache_key("tag", VERSION, params, [img, ctx.normalized])
        if ctx.run.unit_is_fresh(name, key):
            continue
        dur = float(Fraction(end - start) / fps)
        clip = ctx.run.path("tag", f"k{start:06d}.clip.mp4")
        clip.parent.mkdir(parents=True, exist_ok=True)
        media.run([media._bin("ffmpeg"), "-y", "-v", "error", "-ss",
                   f"{float(Fraction(start) / fps):.6f}", "-i", str(ctx.normalized), "-t",
                   f"{max(dur, 0.1):.6f}", "-vf", f"fps={ctx.cfg.tag.sample_fps},scale=-2:480",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-an", str(clip)])
        errors: list[str] = []
        tags: KeyTags | None = None
        for attempt in range(MAX_INVALID_RETRIES + 1):
            raw = ctx.stack.vlm.tag(img, clip, dur, tag_instructions(dur), schema,
                                    seed_for("tag", start, attempt))
            try:
                tags = validate_tags(raw)
                break
            except ValueError as e:
                errors.append(str(e))
        if tags is None:
            raise StageError(f"VLM returned invalid tags for key image {start} "
                             f"{MAX_INVALID_RETRIES + 1} times: {errors[-1]}")
        rel = f"tag/k{start:06d}.json"
        atomic_write_json(ctx.run.path(rel), tags.model_dump())
        ctx.run.record_unit(name, key, "done",
                            {"tags": rel, "clip": f"tag/k{start:06d}.clip.mp4"},
                            {"invalid_attempts": len(errors)}, attempts=len(errors) + 1)
        ctx.tick("tag", f"k{start}", (i + 1) / len(cands))
    checked = len(ctx.checked_keys())
    ctx.say(f"tag: {len(cands)} key images tagged, {checked} checked")
