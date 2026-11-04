"""audio (DESIGN_V2 §4): regenerate (foley + Drama 3 voice sounds) | keep_voice | silent."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from c2a.audio.voice import Placement, SegmentTime, group_phrases, place
from c2a.core import media
from c2a.core.context import Context, seed_for
from c2a.core.manifest import atomic_write_json, cache_key, file_digest
from c2a.core.timeline import Timeline
from c2a.prompts.vocab import sound_cues
from c2a.providers.base import TimedCue
from c2a.providers.fake import SR, read_wav, write_wav
from c2a.qa.limiter import limit_true_peak

VERSION = 6
TP_HEADROOM_DB = 0.5  # AAC adds inter-sample peaks; export measures and trims the rest
MAX_VOICE_RETRIES = 2

VOICE_DIRECTIONS: dict[str, str] = {  # Drama 3 directions: sounds only, never words
    "satisfied": "[soft satisfied sigh]",
    "delighted": '[quiet delighted "mm"]',
    "happy": "[small happy laugh under the breath]",
    "surprised": "[soft surprised breath in]",
    "calm": "[slow calm exhale]",
    "focused": "[quiet focused breath]",
}


@dataclass(frozen=True)
class VoiceEvent:
    t: float  # output seconds
    label: str
    intensity: int
    direction: str


def _seconds(tl: Timeline, frames: int) -> float:
    return float(frames / tl.src_fps)


def _out_s(tl: Timeline, out_frame: int) -> float:
    return float(out_frame / tl.out_fps)


def _cut_clip(video: Path, t0: float, dur: float, dst: Path) -> None:
    media.run([media._bin("ffmpeg"), "-y", "-v", "error", "-ss", f"{t0:.6f}", "-i", str(video),
               "-t", f"{dur:.6f}", "-an", "-c:v", "libx264", "-preset", "ultrafast",
               "-crf", "28", str(dst)])


def _foley(ctx: Context, tl: Timeline, video: Path, total: int) -> np.ndarray:
    phrases = sound_cues()
    keys = {k.start: k for k in ctx.checked_keys()}
    out = np.zeros((total, 2), np.float32)
    for seg in tl.segments:
        k = keys.get(seg.start) or next(v for v in keys.values() if v.image == seg.key)
        dur = float(seg.clip_s)
        cues = [TimedCue(0.0, dur, phrases[c], c) for c in (k.tags.sound_cues if k.tags else [])]
        clip = ctx.run.path("audio", f"{seg.id}.mp4")
        _cut_clip(video, _out_s(tl, seg.out_start), dur, clip)
        wav = ctx.run.path("audio", f"{seg.id}.foley.wav")
        ctx.stack.foley.foley(clip, cues, dur, seed_for("foley", seg.id), wav)
        data, _ = read_wav(wav)
        a = round(_out_s(tl, seg.out_start) * SR)
        n = min(len(data), total - a)
        if n > 0:
            out[a:a + n] += data[:n]
    return out


def _voice_fx(ctx: Context, tl: Timeline, total: int) -> tuple[np.ndarray, list[dict[str, object]],
                                                           list[dict[str, object]]]:
    vc = ctx.cfg.audio.voice
    keys = {k.start: k for k in ctx.checked_keys()}
    out = np.zeros((total, 2), np.float32)
    events: list[VoiceEvent] = []
    for seg in tl.segments:
        k = keys.get(seg.start) or next(v for v in keys.values() if v.image == seg.key)
        if k.tags is None or not k.tags.person_visible:
            continue
        e = k.tags.emotion
        if e.intensity < vc.min_intensity or e.label not in VOICE_DIRECTIONS:
            continue
        t = _out_s(tl, seg.out_start) + float(seg.clip_s) / 2
        if events and t - events[-1].t < vc.min_gap_s:
            continue
        events.append(VoiceEvent(round(t, 3), e.label, e.intensity, VOICE_DIRECTIONS[e.label]))
    gain = 10 ** (vc.gain_db / 20)
    placed, dropped = [], []
    for i, ev in enumerate(events):
        wav = ctx.run.path("audio", f"voicefx_{i}.wav")
        ok = False
        for attempt in range(MAX_VOICE_RETRIES + 1):
            ctx.stack.voice.vocal(ev.direction, vc.reference_id, seed_for("voice", i, attempt), wav)
            if not vc.word_guard or ctx.stack.asr.count_words(wav) == 0:
                ok = True
                break
        if not ok:
            dropped.append({"t": ev.t, "label": ev.label, "reason": "words detected"})
            continue
        data, _ = read_wav(wav)
        a = round(ev.t * SR)
        n = min(len(data), total - a)
        if n > 0:
            out[a:a + n] += data[:n] * gain
        placed.append({"t": ev.t, "label": ev.label, "direction": ev.direction})
    return out, placed, dropped


def _render_placement(stem: Path, p: Placement, fade_ms: int, dst: Path) -> np.ndarray:
    dur = p.src_end - p.src_start
    if dur <= 0.01:
        return np.zeros((0, 2), np.float32)
    af = [f"atrim=start={p.src_start:.4f}:end={p.src_end:.4f}", "asetpts=PTS-STARTPTS"]
    if abs(p.tempo - 1) > 1e-6:
        af.append(f"atempo={p.tempo:.6f}")
    if p.fade_out:
        out_len = dur / p.tempo
        f = min(fade_ms / 1000, out_len / 2)
        af.append(f"afade=t=out:st={max(0.0, out_len - f):.4f}:d={f:.4f}")
    media.run([media._bin("ffmpeg"), "-y", "-v", "error", "-i", str(stem), "-af", ",".join(af),
               "-ar", str(SR), "-ac", "2", "-c:a", "pcm_s16le", str(dst)])
    data, _ = read_wav(dst)
    return data


def _keep_voice(ctx: Context, tl: Timeline,
                total: int) -> tuple[np.ndarray, list[dict[str, object]]]:
    kv = ctx.cfg.audio.keep_voice
    src = ctx.run.path("ingest", "source_audio.wav")
    out = np.zeros((total, 2), np.float32)
    if not src.exists():
        return out, []
    stem = ctx.run.path("audio", "voice_stem.wav")
    ctx.stack.separator.voice(src, stem)
    words = ctx.stack.word_timer.words(stem)
    phrases = group_phrases(words, kv.phrase_gap_s)
    segs = [SegmentTime(s.index, _seconds(tl, s.start), _seconds(tl, s.end), float(s.clip_s),
                        _out_s(tl, s.out_start)) for s in tl.segments]
    placements = place(phrases, segs, kv.stretch_tolerance)
    report: list[dict[str, object]] = []
    xf = round(kv.crossfade_ms / 1000 * SR)
    for i, p in enumerate(placements):
        data = _render_placement(stem, p, kv.fade_ms, ctx.run.path("audio", f"phrase_{i}.wav"))
        seg = segs[p.segment]
        a = round(p.out_start * SR)
        limit = round((seg.out_start + seg.clip_s) * SR)  # never cross into the next clip
        n = max(0, min(len(data), limit - a, total - a))
        if n > 0:
            chunk = data[:n].copy()
            if xf and n > 2 * xf and a + n >= limit - xf:
                chunk[-xf:] *= np.linspace(1, 0, xf, dtype=np.float32)[:, None]
            out[a:a + n] += chunk
        report.append({**asdict(p), "out_end": round(p.out_start + n / SR, 4)})
    return out, report


def _envelope(x: np.ndarray, win: int) -> np.ndarray:
    mono = np.abs(x).max(axis=1) if x.ndim == 2 else np.abs(x)
    if win <= 1:
        return mono
    k = np.ones(win) / win
    return np.convolve(mono, k, mode="same")


def _loudnorm(src: Path, dst: Path, lufs: float, tp: float) -> None:
    ff = media._bin("ffmpeg")
    target = f"loudnorm=I={lufs}:TP={tp}:LRA=11"
    proc = media.run([ff, "-v", "info", "-nostats", "-i", str(src), "-af",
                      f"{target}:print_format=json", "-f", "null", "-"])
    err = proc.stderr.decode(errors="replace")
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", err, re.S)
    if not m:
        media.run([ff, "-y", "-v", "error", "-i", str(src), "-af", target, "-ar", str(SR),
                   str(dst)])
        return
    meas = json.loads(m.group(0))
    af = (f"{target}:measured_I={meas['input_i']}:measured_TP={meas['input_tp']}:"
          f"measured_LRA={meas['input_lra']}:measured_thresh={meas['input_thresh']}:"
          f"offset={meas['target_offset']}:linear=true")
    media.run([ff, "-y", "-v", "error", "-i", str(src), "-af", af, "-ar", str(SR),
               "-c:a", "pcm_s16le", str(dst)])


def audio(ctx: Context) -> None:
    ac = ctx.cfg.audio
    tl = ctx.timeline()
    video = ctx.run.path("post", "video.mp4")
    tag_files = sorted(ctx.run.path("tag").glob("k*.json"))
    inputs = [video, *tag_files]
    src_audio = ctx.run.path("ingest", "source_audio.wav")
    if ac.mode == "keep_voice" and src_audio.exists():
        inputs.append(src_audio)
    params = {"audio": ac.model_dump(), "providers": [p.info.name for p in ctx.stack.all()],
              "keys": [(k.start, k.tags.model_dump() if k.tags else None)
                       for k in ctx.checked_keys()],
              "timeline": [(s.id, s.out_start, str(s.clip_s)) for s in tl.segments]}
    key = cache_key("audio", VERSION, params, inputs)
    if ctx.run.unit_is_fresh("audio", key):
        return
    ctx.run.path("audio").mkdir(parents=True, exist_ok=True)
    if ac.mode == "silent":
        atomic_write_json(ctx.run.path("audio", "none.json"), {"audio": None})
        ctx.run.record_unit("audio", key, "done", {"none": "audio/none.json"}, {"mode": "silent"})
        ctx.say("audio: silent")
        return

    total = round(float(tl.out_duration) * SR)
    metrics: dict[str, object] = {"mode": ac.mode}
    sfx = np.zeros((total, 2), np.float32)
    if ac.sfx.enabled and (ac.mode == "regenerate" or ac.keep_voice.foley_under):
        sfx = _foley(ctx, tl, video, total)
    if ac.mode == "regenerate":
        vox, placed, dropped = (_voice_fx(ctx, tl, total) if ac.voice.enabled
                                else (np.zeros((total, 2), np.float32), [], []))
        metrics.update(voice_events=placed, voice_dropped=dropped)
        mix = sfx + vox
    else:
        vox, report = _keep_voice(ctx, tl, total)
        env = _envelope(vox, round(0.05 * SR))
        duck = 1 - (1 - 10 ** (-ac.keep_voice.duck_db / 20)) * np.clip(env / (env.max() + 1e-9)
                                                                        * 4, 0, 1)
        mix = sfx * duck[:, None].astype(np.float32) + vox
        metrics.update(phrases=report,
                       cut_words=[w for r in report for w in r["cut_words"]],  # type: ignore[attr-defined]
                       has_source_audio=src_audio.exists())
    if float(np.abs(mix).max()) < 1e-6:
        atomic_write_json(ctx.run.path("audio", "none.json"), {"audio": None})
        ctx.run.record_unit("audio", key, "done", {"none": "audio/none.json"},
                            {**metrics, "track": "none (nothing to play)"})
        ctx.say("audio: nothing to play, no audio track")
        return
    raw = ctx.run.path("audio", "mix_raw.wav")
    write_wav(raw, np.clip(mix, -1, 1))
    normed = ctx.run.path("audio", "mix_normed.wav")
    _loudnorm(raw, normed, ac.loudness_lufs, ac.true_peak_dbtp - TP_HEADROOM_DB)
    data, _ = read_wav(normed)
    out = ctx.run.path("audio", "mix.wav")
    write_wav(out, limit_true_peak(data, ac.true_peak_dbtp - TP_HEADROOM_DB, SR))
    metrics["mix_digest"] = file_digest(out)
    ctx.run.record_unit("audio", key, "done", {"mix": "audio/mix.wav"}, metrics)
    ctx.say(f"audio: {ac.mode}")
