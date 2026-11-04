import numpy as np
import pytest

from c2a.providers.fake import SR
from c2a.stages.audio import VOICE_DIRECTIONS, crossfade_concat, shot_windows


@pytest.mark.parametrize("dur", [0.5, 8.0, 8.1, 15.5, 31.0])
def test_windows_cover_and_overlap_by_crossfade(dur: float) -> None:
    w = shot_windows(dur, 8.0, 0.5)
    assert w[0][0] == 0.0 and w[-1][1] == pytest.approx(dur)
    for (a0, a1), (b0, _b1) in zip(w, w[1:], strict=False):
        assert a1 - b0 == pytest.approx(0.5)
        assert a1 - a0 <= 8.0 + 1e-9


def test_crossfade_is_unity_gain() -> None:
    dur = 20.0
    w = shot_windows(dur, 8.0, 0.5)
    parts = [np.ones((round((b - a) * SR), 2), np.float32) for a, b in w]
    out = crossfade_concat(parts, [a for a, _ in w], round(dur * SR), 0.5)
    assert np.allclose(out, 1.0, atol=2e-3)


def test_voice_directions_are_sound_only() -> None:
    # Drama 3 must not speak words: directions are a single bracketed cue, nothing else
    for d in VOICE_DIRECTIONS.values():
        assert d.startswith("[") and d.endswith("]") and d.count("[") == 1
