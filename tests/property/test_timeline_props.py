"""Timeline invariants (TESTING.md §3). 1k examples per PR; HYPOTHESIS_PROFILE=nightly → 10k."""

import os
from fractions import Fraction

from hypothesis import given, settings
from hypothesis import strategies as st

from c2a.core.timeline import ClipSettings, build_shots, build_timeline, round_half_up

settings.register_profile("pr", max_examples=1000, deadline=None)
settings.register_profile("nightly", max_examples=10_000, deadline=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "pr"))

FPS = [Fraction(24000, 1001), Fraction(24), Fraction(25), Fraction(30000, 1001),
       Fraction(30), Fraction(60)]
OUT_FPS = [Fraction(24), Fraction(25), Fraction(30), Fraction(30000, 1001)]


@st.composite
def inputs(draw: st.DrawFn) -> tuple[int, Fraction, list[int], list[tuple[int, int]],
                                      Fraction, ClipSettings]:
    fps = draw(st.sampled_from(FPS))
    n = max(2, int(draw(st.integers(1, 120)) * fps))
    cuts = draw(st.lists(st.integers(-5, n + 5), max_size=20))
    starts = draw(st.lists(st.integers(0, n - 1), max_size=40))
    mode = draw(st.sampled_from(["match", "fixed", "per_clip"]))
    per = {s: Fraction(draw(st.integers(5, 80)), 10) for s in starts[:5]}
    cfg = ClipSettings(mode=mode, fixed_s=Fraction(draw(st.integers(10, 50)), 10), per_clip=per,
                       anchor_end=draw(st.booleans()))
    return n, fps, cuts, [(s, s) for s in starts], draw(st.sampled_from(OUT_FPS)), cfg


@given(inputs())
def test_invariants(args: tuple[int, Fraction, list[int], list[tuple[int, int]], Fraction,
                                ClipSettings]) -> None:
    n, fps, cuts, keys, out_fps, cfg = args
    shots = build_shots(n, fps, cuts)
    # keys must lie in a shot together with their image (same frame here)
    tl = build_timeline(n, fps, shots, keys, out_fps, cfg)

    # segments tile every shot exactly and never cross a cut
    for shot in shots:
        segs = [s for s in tl.segments if s.shot == shot.index]
        assert segs and segs[0].start == shot.start and segs[-1].end == shot.end
        for a, b in zip(segs, segs[1:], strict=False):
            assert a.end == b.start
            assert a.next_key == (b.key if cfg.anchor_end else None)
        assert segs[-1].next_key is None
        for s in segs:
            assert shot.start <= s.key < shot.end

    # clip lengths respect the bounds; frame counts follow the model rule
    for s in tl.segments:
        assert cfg.min_clip_s <= s.clip_s <= cfg.max_clip_s
        assert 17 <= s.gen_frames <= 81 and (s.gen_frames - 1) % 4 == 0
        if cfg.mode == "fixed":
            assert s.clip_s == min(max(cfg.fixed_s, cfg.min_clip_s), cfg.max_clip_s)

    # output frames tile [0, total) and total equals the rounded sum of clip lengths
    assert tl.segments[0].out_start == 0 and tl.segments[-1].out_end == tl.out_frames
    for a, b in zip(tl.segments, tl.segments[1:], strict=False):
        assert a.out_end == b.out_start
    assert tl.out_frames == round_half_up(sum(s.clip_s for s in tl.segments) * out_fps)
