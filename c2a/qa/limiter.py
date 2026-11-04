"""True-peak measurement and limiting in numpy (no dependence on the ffmpeg version).

True peak follows ITU-R BS.1770: the signal is upsampled 4x and the absolute maximum of the
upsampled signal is taken. The limiter computes, per sample, the gain needed to keep the
4x-oversampled peak under the limit, holds it over a look-ahead window and smooths it,
so only transients are reduced and loudness barely moves.
"""

from __future__ import annotations

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from numpy.typing import NDArray

F32 = NDArray[np.float32]
F64 = NDArray[np.float64]
OVERSAMPLE = 4
BLOCK = 1 << 18
MARGIN = 2048


def _upsample(block: F64, factor: int) -> F64:
    n = block.shape[0]
    spec = np.fft.rfft(block, axis=0)
    padded = np.zeros((n * factor // 2 + 1, block.shape[1]), dtype=complex)
    padded[: spec.shape[0]] = spec
    return np.asarray(np.fft.irfft(padded, n=n * factor, axis=0) * factor, dtype=np.float64)


def peak_envelope(x: F32 | F64) -> F64:
    """Per-sample 4x-oversampled absolute peak across channels, shape (n,)."""
    x64 = np.asarray(x, dtype=np.float64)
    if x64.ndim == 1:
        x64 = x64[:, None]
    n = x64.shape[0]
    env = np.zeros(n)
    for start in range(0, n, BLOCK):
        a, b = max(0, start - MARGIN), min(n, start + BLOCK + MARGIN)
        up = np.abs(_upsample(x64[a:b], OVERSAMPLE)).max(axis=1)
        up = up.reshape(-1, OVERSAMPLE).max(axis=1)
        lo, hi = start - a, start - a + min(BLOCK, n - start)
        env[start:start + (hi - lo)] = up[lo:hi]
    return env


def true_peak_db(x: F32 | F64) -> float:
    p = float(peak_envelope(x).max()) if len(x) else 0.0
    return float(20 * np.log10(max(p, 1e-12)))


def limit_true_peak(x: F32, limit_db: float, sr: int, lookahead_ms: float = 1.5) -> F32:
    """Return x with its true peak ≤ limit_db (dBTP)."""
    if not len(x):
        return x
    lim = 10 ** (limit_db / 20)
    for _ in range(3):
        env = peak_envelope(x)
        if env.max() <= lim:
            return x
        need = np.minimum(1.0, lim / np.maximum(env, 1e-12))
        w = max(2, round(lookahead_ms * sr / 1000))
        padded = np.pad(need, (w, w), constant_values=1.0)
        gmin = sliding_window_view(padded, 2 * w + 1).min(axis=1)  # hold ±w around peaks
        k = np.ones(w) / w
        gain = np.convolve(np.pad(gmin, (w // 2, w - 1 - w // 2), mode="edge"), k, "valid")
        gain = np.minimum(gain, 1.0)
        x = np.asarray(x * gain[:, None].astype(np.float32), dtype=np.float32)
    # final safety: a plain gain for anything the smooth gain missed
    over = peak_envelope(x).max() / lim
    if over > 1:
        x = np.asarray(x / np.float32(over * 1.0001), dtype=np.float32)
    return x
