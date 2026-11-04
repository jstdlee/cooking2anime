"""post: clips onto the output timeline at their chosen lengths, film finish, encode."""

from __future__ import annotations

from collections.abc import Iterator

from c2a.core import media
from c2a.core.context import Context
from c2a.core.errors import StageError
from c2a.core.manifest import cache_key
from c2a.prompts.style import post_filter
from c2a.stages.generate import clip_video

VERSION = 2


def post(ctx: Context) -> None:
    tl = ctx.timeline()
    inputs = [ctx.run.path(clip_video(s)) for s in tl.segments]
    style = ctx.style()
    vf = post_filter(style.post)
    # Exact-frame retiming; RIFE (cfg.post.interpolate == "rife") arrives with the real
    # stack; until then frames are mapped to the nearest generated frame.
    interp, upscaler = "nearest", "lanczos"
    params = {"post": ctx.cfg.post.model_dump(), "out_size": ctx.out_size, "vf": vf,
              "out_fps": str(tl.out_fps), "crf": ctx.cfg.output.crf, "interp": interp,
              "timeline": [(s.id, s.n_out, s.gen_frames) for s in tl.segments]}
    key = cache_key("post", VERSION, params, inputs)
    if ctx.run.unit_is_fresh("post", key):
        return
    size = ctx.gen_size

    def frames() -> Iterator[media.Frame]:
        for s in tl.segments:
            gen = list(media.read_frames(ctx.run.path(clip_video(s)), size))
            if len(gen) != s.gen_frames:
                raise StageError(f"clip {s.id}: {len(gen)} frames, expected {s.gen_frames}")
            for j in range(s.n_out):
                yield gen[tl.gen_index_for_out(s, j)]

    rel = "post/video.mp4"
    n = media.write_video(frames(), ctx.run.path(rel), tl.out_fps, out_size=ctx.out_size,
                          crf=ctx.cfg.output.crf, extra_vf=vf)
    if n != tl.out_frames:
        raise StageError(f"post wrote {n} frames, timeline needs {tl.out_frames}")
    ctx.run.record_unit("post", key, "done", {"video": rel},
                        {"frames": n, "interp": interp, "upscaler": upscaler, "finish": vf})
    ctx.say(f"post: {n} frames at {tl.out_fps} fps, {ctx.out_size[0]}x{ctx.out_size[1]}")
