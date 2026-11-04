"""Timeline invariants (TESTING.md §3). 1k examples per PR; set HYPOTHESIS_PROFILE=nightly
for 10k."""

import os
from fractions import Fraction

from hypothesis import given, settings
from hypothesis import strategies as st

from c2a.core.timeline import build_timeline, round_half_up

settings.register_profile("pr", max_examples=1000, deadline=None)
settings.register_profile("nightly", max_examples=10_000, deadline=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "pr"))

FPS = [Fraction(24000, 1001), Fraction(24), Fraction(25), Fraction(30000, 1001),
       Fraction(30), Fraction(50), Fraction(60000, 1001), Fraction(60)]
OUT_FPS = [Fraction(24), Fraction(25), Fraction(30), Fraction(30000, 1001)]


@st.composite
def inputs(draw: st.DrawFn) -> tuple[int, Fraction, list[int], Fraction]:
    fps = draw(st.sampled_from(FPS))
    dur = draw(st.integers(1, 180))
    n = max(2, int(dur * fps))
    cuts = draw(st.lists(st.integers(-5, n + 5), max_size=40))
    return n, fps, cuts, draw(st.sampled_from(OUT_FPS))


@given(inputs())
def test_invariants(args: tuple[int, Fraction, list[int], Fraction]) -> None:
    n, fps, cuts, out_fps = args
    tl = build_timeline(n, fps, cuts, out_fps)

    # shots tile the source
    assert tl.shots[0].start == 0 and tl.shots[-1].end == n
    for a, b in zip(tl.shots, tl.shots[1:], strict=False):
        assert a.end == b.start

    # output frames tile [0, out_frames) exactly; total matches duration to < 1 frame
    assert tl.chunks[0].out_start == 0
    assert tl.chunks[-1].out_end == tl.out_frames
    for a, b in zip(tl.chunks, tl.chunks[1:], strict=False):
        assert a.out_end == b.out_start
    assert tl.out_frames == round_half_up(Fraction(n) / fps * out_fps)
    assert abs(Fraction(tl.out_frames) / out_fps - tl.duration) <= Fraction(1, 2) / out_fps

    for c in tl.chunks:
        shot = tl.shots[c.shot]
        # never crosses a cut
        assert shot.start <= c.first_kf <= c.last_kf <= shot.end - 1
        # Wan length rule
        assert 17 <= c.gen_frames <= 81 and (c.gen_frames - 1) % 4 == 0
        # one chunk covers at most 5 s between keyframes
        assert Fraction(c.last_kf - c.first_kf) / fps <= Fraction(80, 16)
        assert c.n_out >= 0
        if c.n_out:
            idx = [tl.gen_index_for_out(c, j) for j in range(c.n_out)]
            assert idx == sorted(idx)
            assert idx[0] >= 0 and idx[-1] <= c.gen_frames - 1

    # neighbouring chunks inside a shot share their boundary keyframe
    for a, b in zip(tl.chunks, tl.chunks[1:], strict=False):
        if a.shot == b.shot:
            assert a.last_kf == b.first_kf and b.index == a.index + 1
        else:
            assert b.index == 0
