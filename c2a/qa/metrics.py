"""QA metrics (design §7).

Implemented exactly: SSIM, edge-SSIM, loudness/true-peak (ffmpeg ebur128), duration.
Stand-ins until the model-based versions land (M4/M5), each named `*_proxy`:
- style_sim_proxy: HSV-histogram cosine (→ DINOv2-L cosine)
- flicker_proxy: mean absolute frame difference (→ RAFT-warped error)
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from c2a.core import media

F = NDArray[np.float64]


def _gray(img: NDArray[np.uint8], max_side: int = 256) -> F:
    g = img.astype(np.float64) @ np.array([0.299, 0.587, 0.114])
    step = max(1, int(np.ceil(max(g.shape) / max_side)))
    if step > 1:
        h, w = (g.shape[0] // step) * step, (g.shape[1] // step) * step
        g = g[:h, :w].reshape(h // step, step, w // step, step).mean(axis=(1, 3))
    return np.asarray(g, dtype=np.float64)


def _box(x: F, k: int) -> F:
    """Mean over k×k windows ('valid'), via integral image."""
    c = np.pad(x, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    s = c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]
    return s / (k * k)


def ssim_gray(a: F, b: F, k: int = 7, data_range: float = 255.0) -> float:
    if a.shape != b.shape:
        raise ValueError("ssim: shape mismatch")
    if min(a.shape) < k:
        k = max(1, min(a.shape))
    c1, c2 = (0.01 * data_range) ** 2, (0.03 * data_range) ** 2
    mu_a, mu_b = _box(a, k), _box(b, k)
    saa = _box(a * a, k) - mu_a**2
    sbb = _box(b * b, k) - mu_b**2
    sab = _box(a * b, k) - mu_a * mu_b
    num = (2 * mu_a * mu_b + c1) * (2 * sab + c2)
    den = (mu_a**2 + mu_b**2 + c1) * (saa + sbb + c2)
    return float(np.mean(num / den))


def ssim(a: NDArray[np.uint8], b: NDArray[np.uint8]) -> float:
    return ssim_gray(_gray(a), _gray(b))


def _sobel_mag(g: F) -> F:
    p = np.pad(g, 1, mode="edge")
    gx = (p[1:-1, 2:] - p[1:-1, :-2]) * 2 + (p[:-2, 2:] - p[:-2, :-2]) + (p[2:, 2:] - p[2:, :-2])
    gy = (p[2:, 1:-1] - p[:-2, 1:-1]) * 2 + (p[2:, :-2] - p[:-2, :-2]) + (p[2:, 2:] - p[:-2, 2:])
    m = np.hypot(gx, gy)
    peak = m.max()
    return m / peak * 255 if peak > 0 else m


def edge_ssim(a: NDArray[np.uint8], b: NDArray[np.uint8]) -> float:
    """Structure kept: SSIM of Sobel edge maps (composition, not colour)."""
    return ssim_gray(_sobel_mag(_gray(a)), _sobel_mag(_gray(b)))


def _hsv_hist(img: NDArray[np.uint8], bins: int = 12, max_side: int = 160) -> F:
    step = max(1, int(np.ceil(max(img.shape[:2]) / max_side)))
    x = img[::step, ::step].astype(np.float64) / 255
    mx, mn = x.max(-1), x.min(-1)
    d = mx - mn + 1e-9
    r, g, b = x[..., 0], x[..., 1], x[..., 2]
    h = np.where(mx == r, (g - b) / d % 6, np.where(mx == g, (b - r) / d + 2, (r - g) / d + 4))
    h = h / 6
    s = np.where(mx > 0, d / (mx + 1e-9), 0)
    hist, _ = np.histogramdd(np.stack([h.ravel(), s.ravel(), mx.ravel()], 1),
                             bins=(bins, 4, 4), range=((0, 1), (0, 1), (0, 1)))
    v = np.asarray(hist.ravel(), dtype=np.float64)
    return np.asarray(v / (np.linalg.norm(v) + 1e-12), dtype=np.float64)


def style_sim_proxy(a: NDArray[np.uint8], b: NDArray[np.uint8]) -> float:
    return float(np.dot(_hsv_hist(a), _hsv_hist(b)))


def flicker_proxy(frames: list[NDArray[np.uint8]]) -> float:
    """Mean per-pixel temporal second difference |f[t+1] - 2 f[t] + f[t-1]| / 255.

    0 for static frames and for linear change (fades, steady drift); high for
    brightness that jumps back and forth. Replaced by RAFT-warped error in M5.
    """
    if len(frames) < 3:
        return 0.0
    g = np.stack([_gray(f, 128) for f in frames])
    acc = np.abs(g[2:] - 2 * g[1:-1] + g[:-2])
    return float(acc.mean() / 255)


def loudness(path: Path) -> tuple[float, float]:
    """(integrated LUFS, true peak dBTP) via ffmpeg ebur128."""
    proc = media.run([media._bin("ffmpeg"), "-v", "info", "-nostats", "-i", str(path),
                      "-filter_complex", "ebur128=peak=true", "-f", "null", "-"])
    err = proc.stderr.decode(errors="replace")
    summary = err[err.rfind("Summary:"):]
    i = re.search(r"I:\s+(-?[0-9.]+|-inf) LUFS", summary)
    tp = re.search(r"Peak:\s+(-?[0-9.]+|-inf) dBFS", summary)
    if not i or not tp:
        raise ValueError("could not parse ebur128 output")
    return float(i.group(1)), float(tp.group(1))
