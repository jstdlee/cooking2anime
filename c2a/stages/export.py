"""export: mux, verify the output contract, write report.json (v2)."""

from __future__ import annotations

import time
from fractions import Fraction
from pathlib import Path

from c2a.core import media
from c2a.core.context import Context
from c2a.core.errors import StageError
from c2a.core.manifest import atomic_write_json, cache_key
from c2a.qa.metrics import loudness

VERSION = 3
MAX_TP_PASSES = 3


def output_name(ctx: Context) -> str:
    return f"out/{Path(ctx.source).stem}_{ctx.cfg.output.resolution}.mp4"


def export(ctx: Context) -> None:
    tl = ctx.timeline()
    video = ctx.run.path("post", "video.mp4")
    mix = ctx.run.path("audio", "mix.wav")
    au = ctx.run.unit("audio") or {}
    has_audio = "mix" in au.get("outputs", {}) and mix.exists()
    inputs = [video, *([mix] if has_audio else [])]
    key = cache_key("export", VERSION, {"res": ctx.cfg.output.resolution}, inputs)
    if ctx.run.unit_is_fresh("export", key):
        return
    rel = output_name(ctx)
    dst = ctx.run.path(rel)
    dst.parent.mkdir(parents=True, exist_ok=True)
    media.mux(video, mix if has_audio else None, dst, tl.out_duration)
    tp_trim_db = 0.0
    if has_audio:
        limit = ctx.cfg.audio.true_peak_dbtp
        for _ in range(MAX_TP_PASSES):
            _, tp = loudness(dst)
            if tp <= limit:
                break
            tp_trim_db += tp - limit + 0.1
            trimmed = ctx.run.path("audio", "mix_trimmed.wav")
            media.run([media._bin("ffmpeg"), "-y", "-v", "error", "-i", str(mix), "-af",
                       f"volume=-{tp_trim_db:.2f}dB", "-c:a", "pcm_s16le", str(trimmed)])
            media.mux(video, trimmed, dst, tl.out_duration)

    info = media.probe(dst, count_frames=True)
    one = Fraction(1) / tl.out_fps
    err = abs(Fraction(info.frames) / tl.out_fps - tl.out_duration)
    checks: dict[str, object] = {
        "frames": info.frames, "frames_expected": tl.out_frames,
        "duration_s": float(tl.out_duration), "duration_error_s": float(err),
        "size": [info.width, info.height], "has_audio": info.has_audio,
        "audio_mode": ctx.cfg.audio.mode, "true_peak_trim_db": round(tp_trim_db, 2),
    }
    problems = []
    if info.frames != tl.out_frames:
        problems.append(f"frame count {info.frames} != {tl.out_frames}")
    if err > one:
        problems.append("duration off by more than one output frame")
    if (info.width, info.height) != ctx.out_size:
        problems.append(f"size {info.width}x{info.height} != {ctx.out_size}")
    if info.has_audio != has_audio:
        problems.append("audio track presence does not match the audio stage")
    if has_audio:
        lufs, tp = loudness(dst)
        checks["loudness_lufs"] = round(lufs, 2)
        checks["true_peak_dbtp"] = round(tp, 2)
        a = ctx.cfg.audio
        if abs(lufs - a.loudness_lufs) > 1.0:
            problems.append(f"loudness {lufs:.2f} LUFS outside {a.loudness_lufs}±1")
        if tp > a.true_peak_dbtp:
            problems.append(f"true peak {tp:.2f} dBTP above {a.true_peak_dbtp}")
    if problems:
        raise StageError("output contract failed: " + "; ".join(problems))

    units = ctx.run.read()["units"]
    clips = []
    for s in tl.segments:
        u = units.get(f"generate/{s.id}", {})
        clips.append({"id": s.id, "key": s.key, "status": u.get("status"),
                      "attempts": u.get("attempts"), "metrics": u.get("metrics", {}),
                      "clip_s": float(s.clip_s), "anchored": s.anchored,
                      "src": [float(s.start / tl.src_fps), float(s.end / tl.src_fps)]})
    style = ctx.style()
    report = {
        "run": ctx.run.id, "source": str(ctx.source), "output": rel, "created": time.time(),
        "stack": ctx.cfg.run.stack,
        "providers": {p.info.name: {"version": p.info.version, "license": p.info.license,
                                    "commercial_ok": p.info.commercial_ok}
                      for p in ctx.stack.all()},
        "style": style.layers, "clips_mode": ctx.cfg.clips.length_mode,
        "checks": checks,
        "flagged_clips": [c["id"] for c in clips if c["status"] == "flagged"],
        "flagged_keys": [k.split("/")[1] for k, u in units.items()
                         if k.startswith("restyle/") and u["status"] == "flagged"],
        "clips": clips, "audio": au.get("metrics", {}),
    }
    atomic_write_json(ctx.run.path("out", "report.json"), report)
    ctx.run.record_unit("export", key, "done", {"video": rel, "report": "out/report.json"}, checks)
    ctx.say(f"export: {rel} ({info.width}x{info.height}, {info.frames} frames, "
            f"{float(tl.out_duration):.2f} s, audio={ctx.cfg.audio.mode if has_audio else 'none'})")
