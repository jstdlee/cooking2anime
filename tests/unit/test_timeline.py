from fractions import Fraction

import pytest

from c2a.core.timeline import (
    ClipSettings,
    build_shots,
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


@pytest.mark.parametrize(("span", "expected"), [
    (Fraction(0), 17), (Fraction(1), 17), (Fraction(5), 81), (Fraction(9), 81),
    (Fraction(3), 49), (Fraction(2), 33), (Fraction(49, 16), 49), (Fraction(25, 8), 53),
])
def test_snap_gen_frames(span: Fraction, expected: int) -> None:
    assert snap_gen_frames(span, 16) == expected


def test_merge_short_shots() -> None:
    assert merge_short_shots([10, 12, 50], 100, 15) == [50]
    assert merge_short_shots([95], 100, 15) == []


def test_match_mode_follows_key_spacing() -> None:
    shots = build_shots(300, Fraction(30), [150])  # two 5 s shots
    tl = build_timeline(300, Fraction(30), shots, [(0, 2), (60, 64), (150, 151), (240, 240)],
                        Fraction(24))
    assert [(s.start, s.end, s.key) for s in tl.segments] == [
        (0, 60, 2), (60, 150, 64), (150, 240, 151), (240, 300, 240)]
    assert [s.clip_s for s in tl.segments] == [2, 3, 3, 2]
    # end anchor = next key in the same shot only
    assert [s.next_key for s in tl.segments] == [64, None, 240, None]
    assert tl.out_duration == 10 and tl.out_frames == 240


def test_shot_without_keys_and_moved_first_key() -> None:
    shots = build_shots(300, Fraction(30), [150])
    tl = build_timeline(300, Fraction(30), shots, [(30, 33)], Fraction(24))
    # shot 0's only key moves to the shot start (image kept); shot 1 gets its first frame
    assert [(s.start, s.key) for s in tl.segments] == [(0, 33), (150, 150)]


def test_fixed_and_per_clip_modes() -> None:
    shots = build_shots(300, Fraction(30), [])
    keys = [(0, 0), (90, 90), (210, 210)]
    fixed = build_timeline(300, Fraction(30), shots, keys, Fraction(24),
                           ClipSettings(mode="fixed", fixed_s=Fraction(2)))
    assert [s.clip_s for s in fixed.segments] == [2, 2, 2]
    per = build_timeline(300, Fraction(30), shots, keys, Fraction(24),
                         ClipSettings(mode="per_clip", per_clip={90: Fraction(5, 2)}))
    # segments are 3 s, 4 s, 3 s; only the key at frame 90 has an override
    assert [s.clip_s for s in per.segments] == [3, Fraction(5, 2), 3]


def test_clamping_and_no_anchor() -> None:
    shots = build_shots(600, Fraction(30), [])
    tl = build_timeline(600, Fraction(30), shots, [(0, 0), (15, 15)], Fraction(24),
                        ClipSettings(anchor_end=False))
    assert tl.segments[0].clip_s == 1  # 0.5 s segment → min 1 s
    assert tl.segments[1].clip_s == 5  # 19.5 s segment → max 5 s
    assert all(not s.anchored for s in tl.segments)


def test_key_image_outside_its_shot_is_rejected() -> None:
    shots = build_shots(300, Fraction(30), [150])
    with pytest.raises(ValueError):
        build_timeline(300, Fraction(30), shots, [(140, 160)], Fraction(24))


def test_gen_index_mapping_bounds() -> None:
    shots = build_shots(120, Fraction(30), [])
    tl = build_timeline(120, Fraction(30), shots, [(0, 0)], Fraction(24))
    s = tl.segments[0]
    idx = [tl.gen_index_for_out(s, j) for j in range(s.n_out)]
    assert idx[0] == 0 and idx[-1] <= s.gen_frames - 1 and idx == sorted(idx)
    with pytest.raises(IndexError):
        tl.gen_index_for_out(s, s.n_out)


def test_json_roundtrip() -> None:
    shots = build_shots(500, NTSC, [120, 300])
    tl = build_timeline(500, NTSC, shots, [(0, 1), (60, 61), (120, 130), (300, 300)],
                        Fraction(24))
    assert timeline_from_json(timeline_to_json(tl)) == tl


def test_rejects_degenerate_input() -> None:
    with pytest.raises(ValueError):
        build_shots(1, NTSC, [])
    with pytest.raises(ValueError):
        build_shots(100, Fraction(0), [])
