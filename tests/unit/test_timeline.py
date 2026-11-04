from fractions import Fraction

import pytest

from c2a.core.timeline import (
    build_timeline,
    merge_short_shots,
    round_half_up,
    snap_gen_frames,
    timeline_from_json,
    timeline_to_json,
)

NTSC = Fraction(30000, 1001)


def test_round_half_up_is_not_bankers() -> None:
    assert round_half_up(Fraction(1, 2)) == 1
    assert round_half_up(Fraction(5, 2)) == 3
    assert round_half_up(Fraction(-1, 2)) == 0


@pytest.mark.parametrize(("span", "expected"), [
    (Fraction(0), 17), (Fraction(1), 17), (Fraction(5), 81), (Fraction(9), 81),
    (Fraction(3), 49), (Fraction(2), 33), (Fraction(49, 16), 49), (Fraction(25, 8), 53),
])
def test_snap_gen_frames(span: Fraction, expected: int) -> None:
    n = snap_gen_frames(span, 16)
    assert n == expected
    assert (n - 1) % 4 == 0


def test_snap_rejects_bad_bounds() -> None:
    with pytest.raises(ValueError):
        snap_gen_frames(Fraction(1), 16, min_len=16)


def test_merge_short_shots() -> None:
    assert merge_short_shots([10, 12, 50], 100, 15) == [50]
    assert merge_short_shots([95], 100, 15) == []  # last shot too short
    assert merge_short_shots([0, 100, 150], 100, 15) == []  # out of range ignored


def test_known_fixture_timeline() -> None:
    # 179 frames @ 29.97 with a cut at frame 96 → the fixture clip in tests/fixtures
    tl = build_timeline(179, NTSC, [96], Fraction(24))
    assert [(s.start, s.end) for s in tl.shots] == [(0, 96), (96, 179)]
    assert [(c.first_kf, c.last_kf, c.gen_frames) for c in tl.chunks] == [
        (0, 95, 53), (96, 178, 45)]
    assert tl.out_frames == 143
    assert tl.chunks[0].out_end == tl.chunks[1].out_start == 77


def test_long_shot_splits_into_shared_boundary_chunks() -> None:
    tl = build_timeline(30 * 12, Fraction(30), [], Fraction(24))  # 12 s, no cuts
    assert len(tl.chunks) == 3
    for a, b in zip(tl.chunks, tl.chunks[1:], strict=False):
        assert a.last_kf == b.first_kf
    assert all(c.gen_frames <= 81 for c in tl.chunks)


def test_gen_index_mapping_hits_both_keyframes() -> None:
    tl = build_timeline(30 * 4, Fraction(30), [], Fraction(30))
    c = tl.chunks[0]
    assert tl.gen_index_for_out(c, 0) == 0
    # the frame shown at the last keyframe's time is the last generated frame
    j = c.last_kf - c.first_kf  # out fps == src fps here
    assert tl.gen_index_for_out(c, j) == c.gen_frames - 1
    with pytest.raises(IndexError):
        tl.gen_index_for_out(c, c.n_out)


def test_json_roundtrip() -> None:
    tl = build_timeline(500, NTSC, [120, 300], Fraction(24))
    assert timeline_from_json(timeline_to_json(tl)) == tl


def test_rejects_degenerate_input() -> None:
    with pytest.raises(ValueError):
        build_timeline(1, NTSC, [], Fraction(24))
    with pytest.raises(ValueError):
        build_timeline(100, Fraction(0), [], Fraction(24))
