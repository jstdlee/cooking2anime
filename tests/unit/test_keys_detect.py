"""Key image detection on synthetic clips with known change points (DESIGN_V2 §8)."""

from __future__ import annotations

from fractions import Fraction

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from c2a.core.timeline import Shot
from c2a.keys.detect import DetectParams, detect_shot, sharpness

H, W = 120, 160


def box_clip(segments: list[tuple[int, tuple[float, float]]], seed: int = 0,
             blur_frames: set[int] | None = None) -> list[np.ndarray]:
    """A bright box over a still textured background; each (n_frames, (vx, vy)) segment
    moves the box at a constant velocity."""
    r = np.random.default_rng(seed)
    bg = r.random((H, W)) * 60 + 80
    x, y = 70.0, 50.0
    frames = []
    for n, (vx, vy) in segments:
        for _ in range(n):
            img = bg.copy()
            xi, yi = round(x), round(y)
            img[yi:yi + 14, xi:xi + 14] = 240
            frames.append(img.astype(np.uint8))
            x = min(max(x + vx, 0), W - 15)
            y = min(max(y + vy, 0), H - 15)
    if blur_frames:
        for i in blur_frames:
            f = frames[i].astype(np.float64)
            frames[i] = ((f + np.roll(f, 1, 1) + np.roll(f, -1, 1) + np.roll(f, 1, 0)
                          + np.roll(f, -1, 0)) / 5).astype(np.uint8)
    return frames


def starts(frames: list[np.ndarray], **kw: float) -> list[int]:
    c = detect_shot(frames, Shot(0, 0, len(frames)), Fraction(30), DetectParams(**kw))
    return [k.start for k in c]


def test_direction_change_and_stop_found_within_two_frames() -> None:
    frames = box_clip([(30, (1.5, 0)), (30, (0, 1)), (30, (0, 0))])
    s = starts(frames)
    assert s[0] == 0
    assert any(abs(x - 30) <= 2 for x in s), s
    assert any(abs(x - 60) <= 2 for x in s), s
    assert len(s) == 3


def test_steady_motion_has_no_extra_keys() -> None:
    assert starts(box_clip([(120, (0.5, 0.2))])) == [0]


def test_min_gap_is_respected() -> None:
    frames = box_clip([(10, (1, 0)), (10, (0, 1)), (10, (-1, 0)), (10, (0, -1)), (30, (0, 0))])
    s = starts(frames, min_gap_s=0.75)
    assert all(b - a >= round(0.75 * 30) for a, b in zip(s, s[1:], strict=False))


def test_shot_start_is_locked_and_image_is_the_sharpest_nearby() -> None:
    frames = box_clip([(60, (1, 0))], blur_frames={0, 1, 2, 3, 4})
    c = detect_shot(frames, Shot(0, 100, 160), Fraction(30), DetectParams())
    assert c[0].locked and c[0].start == 100
    assert c[0].image - 100 >= 5  # skipped the blurred first frames
    best = max(range(0, 8), key=lambda i: sharpness(frames[i].astype(np.float64)))
    assert c[0].image - 100 == best


def test_coverage_fills_long_gaps() -> None:
    frames = box_clip([(330, (0.3, 0.1))])  # 11 s of steady motion: no change points
    s = starts(frames, max_gap_s=5.0)
    gaps = [b - a for a, b in zip([*s, 330], [*s[1:], 330], strict=False)] + [330 - s[-1]]
    assert max(gaps) <= 5 * 30
    assert len(s) >= 3


@settings(max_examples=20, deadline=None)
@given(st.lists(st.integers(25, 45), min_size=2, max_size=4), st.integers(0, 3))
def test_random_direction_changes(lengths: list[int], seed: int) -> None:
    dirs = [(1, 0), (0, 1), (-1, 0), (0, -1)]  # the box stays inside the frame
    segs = [(n, dirs[i % 4]) for i, n in enumerate(lengths)]
    frames = box_clip(segs, seed)
    s = starts(frames)
    truth = np.cumsum(lengths)[:-1]
    for t in truth:
        assert any(abs(x - t) <= 2 for x in s), (truth, s)


@pytest.mark.parametrize("n", [1, 2])
def test_tiny_shots(n: int) -> None:
    frames = box_clip([(n, (1, 0))])
    assert starts(frames) == [0]
