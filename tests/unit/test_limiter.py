import numpy as np
import pytest

from c2a.qa.limiter import limit_true_peak, peak_envelope, true_peak_db

SR = 48_000


def test_true_peak_sees_inter_sample_peaks() -> None:
    # a sine at fs/4 sampled at 45° phase: sample peak is 0.707 of the true peak
    n = np.arange(4800)
    x = np.sin(2 * np.pi * (SR / 4) * n / SR + np.pi / 4).astype(np.float32)[:, None]
    assert np.abs(x).max() == pytest.approx(0.7071, abs=1e-3)
    assert true_peak_db(x) == pytest.approx(0.0, abs=0.05)


def test_limiter_meets_limit_and_keeps_quiet_parts() -> None:
    r = np.random.default_rng(0)
    x = (0.1 * r.standard_normal((SR * 3, 2))).astype(np.float32)
    x[SR : SR + 50] *= 12  # a sharp transient
    y = limit_true_peak(x, -1.5, SR)
    assert true_peak_db(y) <= -1.5 + 1e-6
    quiet = slice(2 * SR, 3 * SR)
    assert np.allclose(y[quiet], x[quiet], rtol=1e-3, atol=1e-6)  # ~untouched far away
    rms = lambda a: float(np.sqrt(np.mean(a.astype(np.float64) ** 2)))  # noqa: E731
    assert rms(y) / rms(x) > 0.97


def test_limiter_is_identity_under_limit_and_handles_empty() -> None:
    x = (0.01 * np.ones((1000, 2))).astype(np.float32)
    assert limit_true_peak(x, -1.0, SR) is x
    empty = np.zeros((0, 2), np.float32)
    assert limit_true_peak(empty, -1.0, SR) is empty
    assert peak_envelope(np.zeros(10)).shape == (10,)
