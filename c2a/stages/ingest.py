"""ingest: CFR normalize (video), keep the source audio aside, find cuts, build shots."""

from __future__ import annotations

from c2a.core import media
from c2a.core.context import Context
from c2a.core.errors import ProviderError
from c2a.core.manifest import atomic_write_json, cache_key
from c2a.core.timeline import build_shots

VERSION = 1
# ffmpeg's scene score is not a probability like TransNetV2's; hard cuts score ~0.4+
# while continuous motion stays well below 0.1.
FFMPEG_SCENE_THRESHOLD = 0.3


def ingest(ctx: Context) -> None:
    cfg = ctx.cfg
    params = {"ingest": cfg.ingest.model_dump(), "res": cfg.output.resolution}
    key = cache_key("ingest", VERSION, params, [ctx.source])
    if ctx.run.unit_is_fresh("ingest", key):
        return
    ctx.say("ingest: normalizing video to constant frame rate")
    src = media.probe(ctx.source)
    info = media.normalize(ctx.source, ctx.normalized, src.fps)
    outputs = {"normalized": "ingest/normalized.mp4", "meta": "ingest/meta.json"}
    if src.has_audio:
        # kept only for audio.mode=keep_voice; every other mode ignores it
        media.extract_audio(ctx.source, ctx.run.path("ingest", "source_audio.wav"))
        outputs["audio"] = "ingest/source_audio.wav"
    if cfg.ingest.shot_detector == "transnetv2" and cfg.run.stack != "fake":
        raise ProviderError("TransNetV2 shot detection lands with the real stack; use "
                            "--set ingest.shot_detector=ffmpeg_scene")
    cuts = media.detect_cuts_ffmpeg(ctx.normalized, info.fps, FFMPEG_SCENE_THRESHOLD)
    shots = build_shots(info.frames, info.fps, cuts)
    gw, gh = media.gen_size(info.width, info.height, "720p")  # video models generate ≤ 720p
    atomic_write_json(ctx.run.path("ingest", "meta.json"), {
        "src_w": info.width, "src_h": info.height, "src_fps": str(info.fps),
        "src_frames": info.frames, "source_had_audio": src.has_audio, "gen_w": gw, "gen_h": gh,
        "cuts": cuts, "shots": [[s.index, s.start, s.end] for s in shots],
        "shot_detector": "ffmpeg_scene",
    })
    ctx.run.record_unit("ingest", key, "done", outputs,
                        {"shots": len(shots), "src_frames": info.frames})
    ctx.say(f"ingest: {info.frames} frames at {info.fps} fps, {len(shots)} shots")
