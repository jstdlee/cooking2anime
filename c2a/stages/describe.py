"""describe: VLM writes a schema-checked motion/emotion/sound description per chunk."""

from __future__ import annotations

import json
from typing import Any

from c2a.core import media
from c2a.core.context import Context, seed_for
from c2a.core.errors import StageError
from c2a.core.manifest import atomic_write_json, cache_key
from c2a.core.timeline import Chunk
from c2a.prompts.describe import ChunkDescription, instructions, validate_description

VERSION = 1
MAX_INVALID_RETRIES = 2
SCHEMA: dict[str, Any] = {}


def _schema() -> dict[str, Any]:
    if not SCHEMA:
        from importlib import resources
        SCHEMA.update(json.loads(
            resources.files("c2a.prompts").joinpath("describe.schema.json").read_text()))
    return SCHEMA


def desc_path(chunk: Chunk) -> str:
    return f"describe/{chunk.id}.json"


def load_description(ctx: Context, chunk: Chunk) -> ChunkDescription:
    p = ctx.run.path(desc_path(chunk))
    return ChunkDescription.model_validate(json.loads(p.read_text()))


def _clip(ctx: Context, chunk: Chunk, rel: str) -> None:
    dur = float(chunk.t_end - chunk.t_start)
    media.run([media._bin("ffmpeg"), "-y", "-v", "error", "-ss", f"{float(chunk.t_start):.6f}",
               "-i", str(ctx.normalized), "-t", f"{dur:.6f}",
               "-vf", f"fps={ctx.cfg.describe.sample_fps},scale=-2:480",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-an",
               str(ctx.run.path(rel))])


def describe(ctx: Context) -> None:
    cfg = ctx.cfg.describe
    for chunk in ctx.timeline.chunks:
        name = f"describe/{chunk.id}"
        clip_rel = f"describe/{chunk.id}.clip.mp4"
        params = {"describe": cfg.model_dump(), "vlm": ctx.stack.vlm.info.name,
                  "t": [str(chunk.t_start), str(chunk.t_end)]}
        key = cache_key("describe", VERSION, params, [ctx.normalized])
        if ctx.run.unit_is_fresh(name, key):
            continue
        ctx.run.path("describe").mkdir(parents=True, exist_ok=True)
        _clip(ctx, chunk, clip_rel)
        dur = float(chunk.t_end - chunk.t_start)
        text = instructions(dur, cfg.max_beats)
        errors: list[str] = []
        desc: ChunkDescription | None = None
        for attempt in range(MAX_INVALID_RETRIES + 1):
            raw = ctx.stack.vlm.describe(ctx.run.path(clip_rel), dur, text, _schema(),
                                         seed_for("describe", chunk.id, attempt))
            try:
                desc = validate_description(raw, dur, cfg.max_beats)
                break
            except ValueError as e:
                errors.append(str(e))
        if desc is None:
            raise StageError(f"VLM returned invalid descriptions for chunk {chunk.id} "
                             f"{MAX_INVALID_RETRIES + 1} times: {errors[-1]}")
        atomic_write_json(ctx.run.path(desc_path(chunk)), desc.model_dump())
        ctx.run.record_unit(name, key, "done",
                            {"description": desc_path(chunk), "clip": clip_rel},
                            {"invalid_attempts": len(errors)}, attempts=len(errors) + 1)
    ctx.say(f"describe: {len(ctx.timeline.chunks)} chunks described")
