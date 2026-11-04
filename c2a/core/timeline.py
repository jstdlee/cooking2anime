"""Frame-exact timeline: shots, chunks, shared boundary keyframes, output mapping.

All positions are source frame indices of the normalized constant-frame-rate video.
Times are exact `Fraction`s so that rounding happens in exactly one place.

Invariants (property-tested in tests/property/test_timeline.py):
- chunks never cross a cut;
- neighbouring chunks inside a shot share their boundary keyframe;
- every generated chunk length is 4n+1 frames within [min_len, max_len];
- the output frame ranges of all chunks tile [0, total_out_frames) with no gap or overlap,
  and total_out_frames == round(source_duration * out_fps).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

MIN_GEN_FRAMES = 17
MAX_GEN_FRAMES = 81


def round_half_up(x: Fraction) -> int:
    """Deterministic rounding (Python's round() is banker's rounding)."""
    return math.floor(x + Fraction(1, 2))


@dataclass(frozen=True)
class Shot:
    index: int
    start: int  # first source frame (inclusive)
    end: int  # last source frame + 1 (exclusive)

    @property
    def n_frames(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class Chunk:
    shot: int
    index: int  # index within the shot
    first_kf: int  # source frame of the first keyframe
    last_kf: int  # source frame of the last keyframe (shared with the next chunk)
    gen_frames: int  # frames to generate (4n+1)
    out_start: int  # first output frame (inclusive)
    out_end: int  # last output frame + 1 (exclusive)
    t_start: Fraction  # source time this chunk starts (seconds)
    t_end: Fraction  # source time this chunk ends (seconds); last chunk of a shot = shot end

    @property
    def id(self) -> str:
        return f"{self.shot}.{self.index}"

    @property
    def n_out(self) -> int:
        return self.out_end - self.out_start


@dataclass(frozen=True)
class Timeline:
    src_fps: Fraction
    src_frames: int
    out_fps: Fraction
    gen_fps: int
    shots: tuple[Shot, ...]
    chunks: tuple[Chunk, ...]

    @property
    def duration(self) -> Fraction:
        return Fraction(self.src_frames) / self.src_fps

    @property
    def out_frames(self) -> int:
        return round_half_up(self.duration * self.out_fps)

    @property
    def keyframes(self) -> list[int]:
        """Unique boundary keyframes in order."""
        seen: dict[int, None] = {}
        for c in self.chunks:
            seen[c.first_kf] = None
            seen[c.last_kf] = None
        return list(seen)

    def chunk(self, chunk_id: str) -> Chunk:
        for c in self.chunks:
            if c.id == chunk_id:
                return c
        raise KeyError(chunk_id)

    def gen_index_for_out(self, chunk: Chunk, j: int) -> int:
        """Which generated frame of `chunk` is shown at its j-th output frame.

        Output frame time is mapped linearly onto the keyframe span; time after the last
        keyframe (the tail of a shot's final source frame) holds the last generated frame.
        """
        if not 0 <= j < chunk.n_out:
            raise IndexError(j)
        span = Fraction(chunk.last_kf - chunk.first_kf) / self.src_fps
        if span == 0:
            return 0
        t = Fraction(chunk.out_start + j) / self.out_fps
        r = (t - Fraction(chunk.first_kf) / self.src_fps) / span
        r = min(max(r, Fraction(0)), Fraction(1))
        return round_half_up(r * (chunk.gen_frames - 1))


def snap_gen_frames(span_s: Fraction, gen_fps: int,
                    min_len: int = MIN_GEN_FRAMES, max_len: int = MAX_GEN_FRAMES) -> int:
    """Nearest 4n+1 frame count for a keyframe span, clamped to [min_len, max_len]."""
    if (min_len - 1) % 4 or (max_len - 1) % 4:
        raise ValueError("min_len and max_len must be 4n+1")
    intervals = span_s * gen_fps
    n = round_half_up(intervals / 4)
    return max(min_len, min(max_len, 4 * n + 1))


def merge_short_shots(cuts: list[int], n_frames: int, min_shot_frames: int) -> list[int]:
    """Drop cuts that would create shots shorter than `min_shot_frames`."""
    clean = sorted({c for c in cuts if 0 < c < n_frames})
    out: list[int] = []
    prev = 0
    for c in clean:
        if c - prev >= min_shot_frames:
            out.append(c)
            prev = c
    # the last shot must also be long enough; otherwise merge it into the previous one
    while out and n_frames - out[-1] < min_shot_frames:
        out.pop()
    return out


def build_timeline(
    src_frames: int,
    src_fps: Fraction,
    cuts: list[int],
    out_fps: Fraction,
    gen_fps: int = 16,
    max_gen_frames: int = MAX_GEN_FRAMES,
    min_gen_frames: int = MIN_GEN_FRAMES,
    min_shot_s: Fraction = Fraction(1, 2),
) -> Timeline:
    if src_frames < 2:
        raise ValueError("video must have at least 2 frames")
    if src_fps <= 0 or out_fps <= 0:
        raise ValueError("fps must be positive")
    min_shot_frames = max(2, math.ceil(min_shot_s * src_fps))
    cuts = merge_short_shots(cuts, src_frames, min_shot_frames)
    bounds = [0, *cuts, src_frames]
    shots = tuple(Shot(i, a, b) for i, (a, b) in enumerate(zip(bounds, bounds[1:], strict=False)))

    max_span = Fraction(max_gen_frames - 1, gen_fps)  # seconds one chunk can cover
    # whole source frames one chunk may span; even splitting keeps every chunk ≤ this
    max_chunk_frames = max(1, math.floor(max_span * src_fps))
    chunks: list[Chunk] = []
    for shot in shots:
        last = shot.end - 1
        span_frames = last - shot.start
        k = max(1, math.ceil(span_frames / max_chunk_frames))
        kfs = [shot.start + round_half_up(Fraction(i * span_frames, k)) for i in range(k + 1)]
        for i in range(k):
            a, b = kfs[i], kfs[i + 1]
            t_start = Fraction(a) / src_fps
            t_end = Fraction(shot.end) / src_fps if i == k - 1 else Fraction(b) / src_fps
            gen = snap_gen_frames(Fraction(b - a) / src_fps, gen_fps, min_gen_frames,
                                  max_gen_frames)
            out_start = round_half_up(t_start * out_fps)
            out_end = round_half_up(t_end * out_fps)
            chunks.append(Chunk(shot.index, i, a, b, gen, out_start, out_end, t_start, t_end))

    return Timeline(src_fps, src_frames, out_fps, gen_fps, shots, tuple(chunks))


def _frac(s: str) -> Fraction:
    return Fraction(s)


def timeline_to_json(t: Timeline) -> dict[str, object]:
    return {
        "src_fps": str(t.src_fps),
        "src_frames": t.src_frames,
        "out_fps": str(t.out_fps),
        "gen_fps": t.gen_fps,
        "shots": [[s.index, s.start, s.end] for s in t.shots],
        "chunks": [
            {"shot": c.shot, "index": c.index, "first_kf": c.first_kf, "last_kf": c.last_kf,
             "gen_frames": c.gen_frames, "out_start": c.out_start, "out_end": c.out_end,
             "t_start": str(c.t_start), "t_end": str(c.t_end)}
            for c in t.chunks
        ],
    }


def timeline_from_json(d: dict[str, Any]) -> Timeline:
    return Timeline(
        src_fps=_frac(d["src_fps"]),
        src_frames=int(d["src_frames"]),
        out_fps=_frac(d["out_fps"]),
        gen_fps=int(d["gen_fps"]),
        shots=tuple(Shot(int(i), int(a), int(b)) for i, a, b in d["shots"]),
        chunks=tuple(
            Chunk(int(c["shot"]), int(c["index"]), int(c["first_kf"]), int(c["last_kf"]),
                  int(c["gen_frames"]), int(c["out_start"]), int(c["out_end"]),
                  _frac(c["t_start"]), _frac(c["t_end"]))
            for c in d["chunks"]
        ),
    )
