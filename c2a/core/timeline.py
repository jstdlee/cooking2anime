"""Frame-exact v2 timeline: shots, checked key images → segments → clips.

Positions are source frame indices of the normalized constant-frame-rate video; times are
exact `Fraction`s so rounding happens in one place.

A *segment* starts at a checked key image (or its shot's first frame) and runs to the next
checked key image in the same shot, or to the shot end. Each segment becomes one generated
*clip* whose length depends on the length mode:

- ``match``:    clip length = segment length, clamped to [min_clip_s, max_clip_s]
- ``fixed``:    every clip is ``fixed_s`` long
- ``per_clip``: a per-key override, falling back to ``match``

Invariants (property-tested in tests/property/test_timeline_props.py):
- segments tile every shot exactly and never cross a cut;
- every shot's first segment starts at the shot start;
- generated frame counts are 4n+1 within [min_gen, max_gen];
- output frame ranges tile [0, total) with no gap or overlap, and total equals the
  rounded sum of clip lengths.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Literal

MIN_GEN_FRAMES = 17
MAX_GEN_FRAMES = 81

LengthMode = Literal["match", "fixed", "per_clip"]


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


def snap_gen_frames(span_s: Fraction, gen_fps: int,
                    min_len: int = MIN_GEN_FRAMES, max_len: int = MAX_GEN_FRAMES) -> int:
    """Nearest 4n+1 frame count for a clip length, clamped to [min_len, max_len]."""
    if (min_len - 1) % 4 or (max_len - 1) % 4:
        raise ValueError("min_len and max_len must be 4n+1")
    n = round_half_up(span_s * gen_fps / 4)
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
    while out and n_frames - out[-1] < min_shot_frames:
        out.pop()
    return out


def build_shots(src_frames: int, src_fps: Fraction, cuts: list[int],
                min_shot_s: Fraction = Fraction(1, 2)) -> tuple[Shot, ...]:
    if src_frames < 2:
        raise ValueError("video must have at least 2 frames")
    if src_fps <= 0:
        raise ValueError("fps must be positive")
    min_shot_frames = max(2, math.ceil(min_shot_s * src_fps))
    cuts = merge_short_shots(cuts, src_frames, min_shot_frames)
    bounds = [0, *cuts, src_frames]
    return tuple(Shot(i, a, b) for i, (a, b) in enumerate(zip(bounds, bounds[1:], strict=False)))


def shot_of(shots: tuple[Shot, ...], frame: int) -> Shot:
    for s in shots:
        if s.start <= frame < s.end:
            return s
    raise ValueError(f"frame {frame} outside the video")


@dataclass(frozen=True)
class ClipSettings:
    mode: LengthMode = "match"
    fixed_s: Fraction = Fraction(3)
    min_clip_s: Fraction = Fraction(1)
    max_clip_s: Fraction = Fraction(5)
    anchor_end: bool = True  # first-last-frame to the next key when it is in the same shot
    per_clip: dict[int, Fraction] = field(default_factory=dict)  # key start frame → seconds


@dataclass(frozen=True)
class Segment:
    index: int
    shot: int
    start: int  # first source frame (inclusive) = key time (or shot start)
    end: int  # last source frame + 1 (exclusive)
    key: int  # source frame of the key image (the clip's main image)
    next_key: int | None  # key image of the next segment in the same shot (end anchor)
    clip_s: Fraction  # generated clip length (seconds)
    gen_frames: int  # frames to generate (4n+1)
    out_start: int
    out_end: int

    @property
    def id(self) -> str:
        return f"k{self.start:06d}"

    @property
    def n_out(self) -> int:
        return self.out_end - self.out_start

    @property
    def anchored(self) -> bool:
        return self.next_key is not None


@dataclass(frozen=True)
class Timeline:
    src_fps: Fraction
    src_frames: int
    out_fps: Fraction
    gen_fps: int
    shots: tuple[Shot, ...]
    segments: tuple[Segment, ...]

    @property
    def src_duration(self) -> Fraction:
        return Fraction(self.src_frames) / self.src_fps

    @property
    def out_duration(self) -> Fraction:
        return sum((s.clip_s for s in self.segments), Fraction(0))

    @property
    def out_frames(self) -> int:
        return round_half_up(self.out_duration * self.out_fps)

    def segment(self, seg_id: str) -> Segment:
        for s in self.segments:
            if s.id == seg_id:
                return s
        raise KeyError(seg_id)

    def gen_index_for_out(self, seg: Segment, j: int) -> int:
        """Generated frame shown at the j-th output frame of `seg` (linear in time)."""
        if not 0 <= j < seg.n_out:
            raise IndexError(j)
        return min(seg.gen_frames - 1, round_half_up(Fraction(j * (seg.gen_frames - 1), seg.n_out)))


def clip_length(seg_s: Fraction, key: int, cfg: ClipSettings) -> Fraction:
    if cfg.mode == "fixed":
        want = cfg.fixed_s
    elif cfg.mode == "per_clip" and key in cfg.per_clip:
        want = cfg.per_clip[key]
    else:
        want = seg_s
    return min(max(want, cfg.min_clip_s), cfg.max_clip_s)


def build_timeline(
    src_frames: int,
    src_fps: Fraction,
    shots: tuple[Shot, ...],
    keys: list[tuple[int, int]],
    out_fps: Fraction,
    clips: ClipSettings | None = None,
    gen_fps: int = 16,
    min_gen_frames: int = MIN_GEN_FRAMES,
    max_gen_frames: int = MAX_GEN_FRAMES,
) -> Timeline:
    """Segments from checked keys.

    `keys` holds (segment start frame, key image frame) pairs. Each shot always gets a
    segment at its first frame: if no key starts there, the first key of the shot is moved
    to the shot start (its image is kept); a shot without keys uses its first frame.
    """
    if out_fps <= 0:
        raise ValueError("fps must be positive")
    cfg = clips or ClipSettings()
    by_shot: dict[int, list[tuple[int, int]]] = {s.index: [] for s in shots}
    for start, image in sorted(set(keys)):
        s = shot_of(shots, start)
        if not s.start <= image < s.end:
            raise ValueError(f"key image {image} is not in the shot of its start {start}")
        by_shot[s.index].append((start, image))

    segments: list[Segment] = []
    t_out = Fraction(0)
    for shot in shots:
        ks = by_shot[shot.index]
        if not ks:
            ks = [(shot.start, shot.start)]
        elif ks[0][0] != shot.start:
            ks[0] = (shot.start, ks[0][1])
        # drop duplicate starts (keep the first image)
        dedup: list[tuple[int, int]] = []
        for k in ks:
            if not dedup or dedup[-1][0] != k[0]:
                dedup.append(k)
        for i, (start, image) in enumerate(dedup):
            end = dedup[i + 1][0] if i + 1 < len(dedup) else shot.end
            nxt = dedup[i + 1][1] if (i + 1 < len(dedup) and cfg.anchor_end) else None
            seg_s = Fraction(end - start) / src_fps
            clip_s = clip_length(seg_s, start, cfg)
            gen = snap_gen_frames(clip_s, gen_fps, min_gen_frames, max_gen_frames)
            o0 = round_half_up(t_out * out_fps)
            t_out += clip_s
            o1 = round_half_up(t_out * out_fps)
            segments.append(Segment(len(segments), shot.index, start, end, image, nxt, clip_s,
                                    gen, o0, o1))
    return Timeline(src_fps, src_frames, out_fps, gen_fps, shots, tuple(segments))


def timeline_to_json(t: Timeline) -> dict[str, Any]:
    return {
        "src_fps": str(t.src_fps), "src_frames": t.src_frames, "out_fps": str(t.out_fps),
        "gen_fps": t.gen_fps, "shots": [[s.index, s.start, s.end] for s in t.shots],
        "segments": [
            {"index": s.index, "shot": s.shot, "start": s.start, "end": s.end, "key": s.key,
             "next_key": s.next_key, "clip_s": str(s.clip_s), "gen_frames": s.gen_frames,
             "out_start": s.out_start, "out_end": s.out_end}
            for s in t.segments
        ],
    }


def timeline_from_json(d: dict[str, Any]) -> Timeline:
    return Timeline(
        src_fps=Fraction(d["src_fps"]), src_frames=int(d["src_frames"]),
        out_fps=Fraction(d["out_fps"]), gen_fps=int(d["gen_fps"]),
        shots=tuple(Shot(int(i), int(a), int(b)) for i, a, b in d["shots"]),
        segments=tuple(
            Segment(int(s["index"]), int(s["shot"]), int(s["start"]), int(s["end"]),
                    int(s["key"]), None if s["next_key"] is None else int(s["next_key"]),
                    Fraction(s["clip_s"]), int(s["gen_frames"]), int(s["out_start"]),
                    int(s["out_end"]))
            for s in d["segments"]
        ),
    )
