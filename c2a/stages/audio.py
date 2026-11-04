"""audio: new soundtrack only (source audio never used). Foley per shot + Drama 3 voice FX."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import numpy as np

from c2a.core import media
from c2a.core.context import Context, seed_for
from c2a.core.manifest import atomic_write_json, cache_key, file_digest
from c2a.core.timeline import Shot
from c2a.prompts.vocab import sound_cues
from c2a.providers.base import TimedCue
from c2a.providers.fake import SR, read_wav, write_wav
from c2a.qa.limiter import limit_true_peak
from c2a.stages.describe import desc_path, load_description

VERSION = 5
# AAC encoding adds inter-sample peaks; mix this much below the true-peak target.
TP_HEADROOM_DB = 0.5
MAX_VOICE_RETRIES = 2

# emotion label → Drama 3 direction. Non-speech only: no words inside the brackets' scope.
VOICE_DIRECTIONS: dict[str, str] = {
    "satisfied": "[soft satisfied sigh]",
    "delighted": '[quiet delighted "mm"]',
    "happy": "[small happy laugh under the breath]",
    "surprised": "[soft surprised breath in]",
    "calm": "[slow calm exhale]",
    "focused": "[quiet focused breath]",
}


@dataclass(frozen=True)
class VoiceEvent:
    t: float  # seconds on the output timeline
    label: str
    intensity: int
    direction: str


def shot_windows(duration: float, window: float, crossfade: float) -> list[tuple[float, float]]:
    """Overlapping windows covering [0, duration]; overlap = crossfade."""
    if duration <= window:
        return [(0.0, duration)]
    step = window - crossfade
    out: list[tuple[float, float]] = []
    t = 0.0
    while True:
        end = min(duration, t + window)
        out.append((t, end))
        if end >= duration:
            break
        t += step
    return out


def crossfade_concat(parts: list[np.ndarray], starts: list[float], total: int,
                     crossfade: float) -> np.ndarray:
    """Overlap-add windows with linear crossfades (equal-gain in overlaps)."""
    out = np.zeros((total, 2), np.float32)
    xf = round(crossfade * SR)
    for i, (p, s) in enumerate(zip(parts, starts, strict=True)):
        a = round(s * SR)
        p = p[: max(0, total - a)].copy()
        if xf > 0:
            if i > 0:
                n = min(xf, len(p))
                p[:n] *= np.linspace(0, 1, n, dtype=np.float32)[:, None]
            if i < len(parts) - 1:
                n = min(xf, len(p))
                p[len(p) - n:] *= np.linspace(1, 0, n, dtype=np.float32)[:, None]
        out[a:a + len(p)] += p
    return out


def shot_cues(ctx: Context, shot: Shot) -> list[TimedCue]:
    phrases = sound_cues()
    cues: list[TimedCue] = []
    shot_t0 = Fraction(shot.start) / ctx.timeline.src_fps
    for c in ctx.timeline.chunks:
        if c.shot != shot.index:
            continue
        off = float(c.t_start - shot_t0)
        for sc in load_description(ctx, c).sound_cues:
            cues.append(TimedCue(off + sc.t0, off + sc.t1, phrases[sc.cue], sc.cue))
    return cues


def voice_events(ctx: Context) -> list[VoiceEvent]:
    vcfg = ctx.cfg.audio.voice
    events: list[VoiceEvent] = []
    for c in ctx.timeline.chunks:
        d = load_description(ctx, c)
        e = d.emotion
        if not d.person_visible or e.intensity < vcfg.min_intensity:
            continue
        if e.label not in VOICE_DIRECTIONS:
            continue
        t = float(c.t_start) + min(e.t, float(c.t_end - c.t_start))
        if events and t - events[-1].t < vcfg.min_gap_s:
            continue
        events.append(VoiceEvent(round(t, 3), e.label, e.intensity, VOICE_DIRECTIONS[e.label]))
    return events


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
    # linear=true falls back to loudnorm's own true-peak limiter when the target cannot be
    # met with a plain gain; export then measures the encoded file and trims if needed.
    af = (f"{target}:measured_I={meas['input_i']}:measured_TP={meas['input_tp']}:"
          f"measured_LRA={meas['input_lra']}:measured_thresh={meas['input_thresh']}:"
          f"offset={meas['target_offset']}:linear=true")
    media.run([ff, "-y", "-v", "error", "-i", str(src), "-af", af, "-ar", str(SR),
               "-c:a", "pcm_s16le", str(dst)])


def audio(ctx: Context) -> None:
    acfg = ctx.cfg.audio
    tl = ctx.timeline
    video = ctx.run.path("post", "video.mp4")
    descs = [ctx.run.path(desc_path(c)) for c in tl.chunks]
    params = {"audio": acfg.model_dump(), "foley": ctx.stack.foley.info.name,
              "voice": ctx.stack.voice.info.name}
    key = cache_key("audio", VERSION, params, [video, *descs])
    if ctx.run.unit_is_fresh("audio", key):
        return
    if not acfg.sfx.enabled and not acfg.voice.enabled:
        ctx.run.path("audio").mkdir(parents=True, exist_ok=True)
        atomic_write_json(ctx.run.path("audio", "none.json"), {"audio": None})
        ctx.run.record_unit("audio", key, "done", {"none": "audio/none.json"},
                            {"track": "none"})
        ctx.say("audio: both layers disabled → silent video")
        return

    ctx.run.path("audio").mkdir(parents=True, exist_ok=True)
    total = round(float(tl.duration) * SR)
    sfx = np.zeros((total, 2), np.float32)
    voice = np.zeros((total, 2), np.float32)
    metrics: dict[str, object] = {"track": "new"}

    if acfg.sfx.enabled:
        for shot in tl.shots:
            t0 = float(Fraction(shot.start) / tl.src_fps)
            dur = float(Fraction(shot.n_frames) / tl.src_fps)
            cues = shot_cues(ctx, shot)
            parts, starts = [], []
            for wi, (a, b) in enumerate(shot_windows(dur, acfg.sfx.window_s,
                                                     acfg.sfx.crossfade_s)):
                wcues = [TimedCue(max(c.t0, a) - a, min(c.t1, b) - a, c.phrase, c.cue)
                         for c in cues if c.t1 > a and c.t0 < b]
                clip = ctx.run.path("audio", f"shot{shot.index}_w{wi}.mp4")
                media.run([media._bin("ffmpeg"), "-y", "-v", "error", "-ss",
                           f"{t0 + a:.6f}", "-i", str(video), "-t", f"{b - a:.6f}", "-an",
                           "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", str(clip)])
                wav = ctx.run.path("audio", f"shot{shot.index}_w{wi}.wav")
                ctx.stack.foley.foley(clip, wcues, b - a, seed_for("foley", shot.index, wi),
                                      wav)
                data, _ = read_wav(wav)
                parts.append(data)
                starts.append(a)
            seg = crossfade_concat(parts, starts, round(dur * SR), acfg.sfx.crossfade_s)
            a0 = round(t0 * SR)
            sfx[a0:a0 + len(seg)] += seg[: total - a0]

    placed, dropped = [], []
    if acfg.voice.enabled:
        gain = 10 ** (acfg.voice.gain_db / 20)
        for i, ev in enumerate(voice_events(ctx)):
            wav = ctx.run.path("audio", f"voice_{i}.wav")
            ok = False
            for attempt in range(MAX_VOICE_RETRIES + 1):
                ctx.stack.voice.vocal(ev.direction, acfg.voice.reference_id,
                                      seed_for("voice", i, attempt), wav)
                if not acfg.voice.word_guard or ctx.stack.asr.count_words(wav) == 0:
                    ok = True
                    break
            if not ok:
                dropped.append({"t": ev.t, "label": ev.label, "reason": "words detected"})
                continue
            data, _ = read_wav(wav)
            a = round(ev.t * SR)
            n = min(len(data), total - a)
            if n > 0:
                voice[a:a + n] += data[:n] * gain
            placed.append({"t": ev.t, "label": ev.label, "direction": ev.direction,
                           "file": f"audio/voice_{i}.wav"})
    metrics["voice_events"] = placed
    metrics["voice_dropped"] = dropped

    raw = ctx.run.path("audio", "mix_raw.wav")
    write_wav(raw, np.clip(sfx + voice, -1, 1))
    normed = ctx.run.path("audio", "mix_normed.wav")
    _loudnorm(raw, normed, acfg.loudness_lufs, acfg.true_peak_dbtp - TP_HEADROOM_DB)
    data, _ = read_wav(normed)
    out = ctx.run.path("audio", "mix.wav")
    write_wav(out, limit_true_peak(data, acfg.true_peak_dbtp - TP_HEADROOM_DB, SR))
    metrics["mix_digest"] = file_digest(out)
    ctx.run.record_unit("audio", key, "done", {"mix": "audio/mix.wav"}, metrics)
    ctx.say(f"audio: foley for {len(tl.shots)} shots, {len(placed)} voice sounds "
            f"({len(dropped)} dropped)")
