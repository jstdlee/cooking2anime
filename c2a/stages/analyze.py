"""analyze: normalize to CFR (audio dropped), find cuts, build timeline, extract keyframes."""

from __future__ import annotations

from c2a.core import media
from c2a.core.context import Context
from c2a.core.errors import ProviderError
from c2a.core.manifest import atomic_write_json, cache_key
from c2a.core.timeline import MAX_GEN_FRAMES, MIN_GEN_FRAMES, build_timeline, timeline_to_json

VERSION = 1
# ffmpeg's scene score is not a probability like TransNetV2's; hard cuts score ~0.4+
# while continuous motion stays well below 0.1.
FFMPEG_SCENE_THRESHOLD = 0.3


def kf_path(ctx: Context, frame: int) -> str:
    return f"analyze/keyframes/kf_{frame:06d}.png"


def analyze(ctx: Context) -> None:
    cfg = ctx.cfg
    params = {"analyze": cfg.analyze.model_dump(), "out_fps": cfg.output.fps,
              "resolution": cfg.output.resolution}
    key = cache_key("analyze", VERSION, params, [ctx.source])
    if ctx.run.unit_is_fresh("analyze", key):
        return
    ctx.say("analyze: normalizing to constant frame rate (source audio dropped)")
    src = media.probe(ctx.source)
    info = media.normalize(ctx.source, ctx.normalized, src.fps)

    if cfg.analyze.shot_detector == "transnetv2" and cfg.run.stack != "fake":
        raise ProviderError("TransNetV2 shot detection lands in M3; use "
                            "--set analyze.shot_detector=ffmpeg_scene")
    detector = "ffmpeg_scene"  # TransNetV2 backend lands with the real stack (M3)
    cuts = media.detect_cuts_ffmpeg(ctx.normalized, info.fps, FFMPEG_SCENE_THRESHOLD)

    lens = sorted(cfg.analyze.chunk_frames)
    tl = build_timeline(info.frames, info.fps, cuts, ctx.out_fps(info.fps),
                        gen_fps=cfg.analyze.gen_fps,
                        min_gen_frames=lens[0] if lens else MIN_GEN_FRAMES,
                        max_gen_frames=lens[-1] if lens else MAX_GEN_FRAMES)
    atomic_write_json(ctx.run.path("analyze", "timeline.json"), timeline_to_json(tl))
    gw, gh = media.gen_size(info.width, info.height, "720p")  # Wan generates ≤ 720p
    ow, oh = media.gen_size(info.width, info.height, cfg.output.resolution)
    atomic_write_json(ctx.run.path("analyze", "meta.json"), {
        "src_w": info.width, "src_h": info.height, "src_fps": str(info.fps),
        "src_frames": info.frames, "source_had_audio": src.has_audio,
        "gen_w": gw, "gen_h": gh, "out_w": ow, "out_h": oh, "cuts": cuts,
        "shot_detector": detector,
    })
    ctx.reset_timeline()

    frames = media.extract_frames(ctx.normalized, tl.keyframes, (gw, gh))
    outputs = {"normalized": "analyze/normalized.mp4", "timeline": "analyze/timeline.json",
               "meta": "analyze/meta.json"}
    for f, img in frames.items():
        rel = kf_path(ctx, f)
        media.save_png(img, ctx.run.path(rel))
        outputs[f"kf_{f}"] = rel
    ctx.run.record_unit("analyze", key, "done", outputs,
                        {"shots": len(tl.shots), "chunks": len(tl.chunks),
                         "keyframes": len(tl.keyframes), "src_frames": info.frames})
    ctx.say(f"analyze: {len(tl.shots)} shots, {len(tl.chunks)} chunks, "
            f"{len(tl.keyframes)} keyframes")
