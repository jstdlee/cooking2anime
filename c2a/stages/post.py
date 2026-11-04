"""post: join chunks onto the exact output timeline, upscale, grain, encode."""

from __future__ import annotations

from collections.abc import Iterator

from c2a.core import media
from c2a.core.context import Context
from c2a.core.errors import StageError
from c2a.core.manifest import cache_key
from c2a.stages.generate import chunk_video

VERSION = 1


def post(ctx: Context) -> None:
    tl = ctx.timeline
    inputs = [ctx.run.path(chunk_video(c)) for c in tl.chunks]
    # Exact-frame retiming. RIFE interpolation (cfg.post.interpolate == "rife") arrives with
    # the real stack in M6; until then frames are mapped by nearest generated frame.
    interp = "nearest"
    upscaler = "lanczos"  # SeedVR2 arrives in M6
    params = {"post": ctx.cfg.post.model_dump(), "out_size": ctx.out_size,
              "out_fps": str(tl.out_fps), "crf": ctx.cfg.output.crf, "interp": interp,
              "upscaler": upscaler}
    key = cache_key("post", VERSION, params, inputs)
    if ctx.run.unit_is_fresh("post", key):
        return
    size = ctx.gen_size

    def frames() -> Iterator[media.Frame]:
        for c in tl.chunks:
            gen = list(media.read_frames(ctx.run.path(chunk_video(c)), size))
            if len(gen) != c.gen_frames:
                raise StageError(f"chunk {c.id}: {len(gen)} frames, expected {c.gen_frames}")
            for j in range(c.n_out):
                yield gen[tl.gen_index_for_out(c, j)]

    grain = ctx.cfg.post.film_grain
    vf = f"noise=alls={round(grain * 20)}:allf=t" if grain > 0 else None
    rel = "post/video.mp4"
    n = media.write_video(frames(), ctx.run.path(rel), tl.out_fps, out_size=ctx.out_size,
                          crf=ctx.cfg.output.crf, extra_vf=vf)
    if n != tl.out_frames:
        raise StageError(f"post wrote {n} frames, timeline needs {tl.out_frames}")
    ctx.run.record_unit("post", key, "done", {"video": rel},
                        {"frames": n, "interp": interp, "upscaler": upscaler})
    ctx.say(f"post: {n} frames at {tl.out_fps} fps, {ctx.out_size[0]}x{ctx.out_size[1]}")
