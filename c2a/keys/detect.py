"""Key image candidates (DESIGN_V2 §2.1). Pure numpy on small grey analysis frames.

Signals, per analysis frame t:
- motion change  |m(t+w) - m(t-w)|, with m the smoothed mean absolute frame difference
  (a stand-in for RAFT flow magnitude): motion starts and stops;
- direction change: angle between the mean phase-correlation shift before and after t,
  weighted by how much motion there is: "moving right" → "moving down";
- appearance change: 1 - cosine of colour/edge signatures 0.5 s apart.

Each signal is divided by a fixed reference level (REFS) and capped; the score is the
strongest of them. Peaks above
`sensitivity` survive non-maximum suppression with `min_gap_s`. Every shot start is a
locked candidate. Gaps longer than `max_gap_s` are filled at the best-scoring point.
The image for each candidate is the sharpest frame (variance of the Laplacian) within
±`refine_s`, so the still-to-video model gets a crisp main image.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

import numpy as np
from numpy.typing import NDArray

from c2a.core.timeline import Shot

F = NDArray[np.float64]
# Reference levels. A signal at its reference counts as one clear event; fixed levels (not
# per video) keep steady motion from being scaled up into false events. Motion change is
# relative (|after − before| / max(before, after)), so it does not depend on how big the
# moving thing is: a stop scores 1 (1.25 × ref), tripling the speed 0.67 (0.83 × ref);
# doubling (0.5) is not a key moment. A 90° turn scores ≈ 0.9.
REFS = {"motion": 0.8, "direction": 0.9, "appearance": 0.05}
MOTION_FLOOR = 0.05  # mean grey-level difference below which a frame counts as still


@dataclass(frozen=True)
class DetectParams:
    sensitivity: float = 0.7  # fraction of a reference-level event
    min_gap_s: float = 0.75
    max_gap_s: float = 5.0
    refine_s: float = 0.25
    window_s: float = 0.3


@dataclass
class Candidate:
    start: int  # source frame where its segment would start
    image: int  # source frame used as the key image
    score: float
    reasons: list[str] = field(default_factory=list)
    locked: bool = False


def _smooth(x: F, k: int) -> F:
    if k <= 1 or len(x) < 2:
        return x
    pad = np.pad(x, (k // 2, k - 1 - k // 2), mode="edge")
    return np.convolve(pad, np.ones(k) / k, mode="valid")


def _phase_shift(a: F, b: F) -> tuple[float, float]:
    """Global translation b relative to a (pixels), by phase correlation."""
    fa, fb = np.fft.fft2(a - a.mean()), np.fft.fft2(b - b.mean())
    r = fb * np.conj(fa)
    r /= np.abs(r) + 1e-9
    c = np.abs(np.fft.ifft2(r))
    y, x = np.unravel_index(int(np.argmax(c)), c.shape)
    h, w = c.shape
    dy = y - h if y > h // 2 else y
    dx = x - w if x > w // 2 else x
    return float(dx), float(dy)


def sharpness(g: F) -> float:
    lap = (-4 * g[1:-1, 1:-1] + g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:])
    return float(lap.var())


def _signature(g: F) -> F:
    hist, _ = np.histogram(g, bins=16, range=(0, 255))
    gx = np.abs(np.diff(g, axis=1)).mean(axis=0)
    gy = np.abs(np.diff(g, axis=0)).mean(axis=1)
    edges = np.concatenate([np.interp(np.linspace(0, len(gx) - 1, 16), np.arange(len(gx)), gx),
                            np.interp(np.linspace(0, len(gy) - 1, 16), np.arange(len(gy)), gy)])
    v = np.concatenate([hist / max(1.0, float(hist.sum())), edges / (edges.sum() + 1e-9)])
    return np.asarray(v / (np.linalg.norm(v) + 1e-12), dtype=np.float64)


def _step(x: NDArray[np.float64], w: int) -> NDArray[np.float64]:
    """|mean(x[t:t+w]) - mean(x[t-w:t])|: a triangular peak exactly at a step in x."""
    n = len(x)
    c = np.concatenate([[0.0], np.cumsum(x, axis=0)]) if x.ndim == 1 else None
    out = np.zeros(n)
    for t in range(1, n):
        a, b = max(0, t - w), min(n, t + w)
        if x.ndim == 1:
            assert c is not None
            before = (c[t] - c[a]) / (t - a)
            after = (c[b] - c[t]) / (b - t)
            out[t] = abs(after - before)
        else:
            before_v, after_v = x[a:t].mean(axis=0), x[t:b].mean(axis=0)
            nb, na = np.linalg.norm(before_v), np.linalg.norm(after_v)
            out[t] = 1 - float(np.dot(before_v, after_v) / (nb * na)) if nb and na else 0.0
    return out


def _step_ratio(x: NDArray[np.float64], w: int, floor: float) -> NDArray[np.float64]:
    """|mean after − mean before| over ±w (peaks exactly at a step), divided by the motion
    level max(before, after) over ±2w (wider, so it is steady and does not move the peak)."""
    n = len(x)
    c = np.concatenate([[0.0], np.cumsum(x)])

    def mean(a: int, b: int) -> float:
        return float((c[b] - c[a]) / (b - a)) if b > a else 0.0

    out = np.zeros(n)
    for t in range(1, n):
        step = abs(mean(t, min(n, t + w)) - mean(max(0, t - w), t))
        level = max(mean(max(0, t - 2 * w), t), mean(t, min(n, t + 2 * w)), floor)
        out[t] = step / level
    return out


def _centroid(d: NDArray[np.float64]) -> tuple[float, float, float]:
    mass = float(d.sum())
    if mass <= 1e-9:
        return 0.0, 0.0, 0.0
    ys, xs = np.indices(d.shape)
    return float((xs * d).sum() / mass), float((ys * d).sum() / mass), mass


def signals(frames: list[NDArray[np.uint8]] | NDArray[np.uint8],
            fps: float, window_s: float = 0.3) -> dict[str, F]:
    """Per-frame signals for one shot's analysis frames (grey uint8, all the same size)."""
    g = np.asarray(frames, dtype=np.float64)
    n = len(g)
    z = np.zeros(n)
    if n < 3:
        sharp = np.array([sharpness(x) for x in g]) if n else z
        return {"motion": z, "direction": z, "appearance": z, "sharp": sharp, "score": z}
    w = max(2, round(window_s * fps))
    absdiff = np.abs(np.diff(g, axis=0))
    m = np.concatenate([[0.0], absdiff.mean(axis=(1, 2))])
    motion = _step_ratio(m, w, MOTION_FLOOR)

    # motion direction per frame: camera shift if the whole frame moved, else the velocity
    # of the centroid of the changed pixels (an object moving over a still background)
    vel = np.zeros((n, 2))
    prev_c: tuple[float, float, float] | None = None
    for t in range(1, n):
        d = np.where(absdiff[t - 1] > 12, absdiff[t - 1], 0.0)
        c = _centroid(d)
        dx, dy = _phase_shift(g[t - 1], g[t])
        if abs(dx) + abs(dy) >= 1 and float(d.mean()) > 4:
            vel[t] = (dx, dy)
        elif prev_c is not None and prev_c[2] > 0 and c[2] > 0:
            vel[t] = (c[0] - prev_c[0], c[1] - prev_c[1])
        prev_c = c
    speed = np.linalg.norm(vel, axis=1)
    moving = speed > 0.2
    unit = np.where(moving[:, None], vel / np.maximum(speed, 1e-9)[:, None], 0.0)
    direction = _step(unit, w) * np.minimum(1.0, _smooth(speed, w))

    sig = np.array([_signature(x) for x in g])
    appearance = _step(sig, max(w, round(0.5 * fps)))
    sharp = np.array([sharpness(x) for x in g])
    parts = {"motion": motion / REFS["motion"], "direction": direction / REFS["direction"],
             "appearance": appearance / REFS["appearance"]}
    # one clear signal is enough: the score is the strongest signal relative to its reference
    score = np.max(np.stack([np.minimum(v, 2.0) for v in parts.values()]), axis=0)
    return {**parts, "sharp": sharp, "score": np.asarray(score, dtype=np.float64)}


def _nms(score: F, thr: float, gap: int, keep_out: int) -> list[int]:
    """Indices of local maxima ≥ thr, greedy by score, ≥ gap apart, ≥ keep_out from ends."""
    n = len(score)
    cand = [i for i in range(keep_out, n - keep_out)
            if score[i] >= thr and score[i] == score[max(0, i - 1):i + 2].max()]
    chosen: list[int] = []
    for i in sorted(cand, key=lambda i: (-score[i], i)):
        if all(abs(i - j) >= gap for j in chosen):
            chosen.append(i)
    return sorted(chosen)


def detect_shot(frames: list[NDArray[np.uint8]] | NDArray[np.uint8], shot: Shot,
                fps: Fraction, params: DetectParams) -> list[Candidate]:
    """Candidates for one shot. `frames[i]` is source frame `shot.start + i`."""
    f = float(fps)
    s = signals(frames, f, params.window_s)
    n = len(s["score"])
    gap = max(1, round(params.min_gap_s * f))
    refine = max(0, round(params.refine_s * f))

    def best_image(i: int, lo_ok: int = 0) -> int:
        a, b = max(lo_ok, i - refine), min(n - 1, i + refine)
        return int(a + np.argmax(s["sharp"][a:b + 1])) if b >= a else i

    out = [Candidate(shot.start, shot.start + best_image(0, 0), 1.0, ["shot start"], True)]
    peaks = _nms(s["score"], params.sensitivity, gap, keep_out=gap)
    for i in peaks:
        parts = sorted(REFS, key=lambda k: -s[k][i])
        why = [f"{p} change" for p in parts if s[p][i] >= params.sensitivity / 2][:2] \
            or ["change"]
        out.append(Candidate(shot.start + i, shot.start + best_image(i, i), float(s["score"][i]),
                             why))
    return fill_gaps(out, shot, s, f, params)


def fill_gaps(cands: list[Candidate], shot: Shot, s: dict[str, F], fps: float,
              params: DetectParams) -> list[Candidate]:
    """Insert candidates until no segment is longer than max_gap_s."""
    max_gap = max(1, int(params.max_gap_s * fps))
    refine = max(0, round(params.refine_s * fps))
    cands = sorted(cands, key=lambda c: c.start)
    changed = True
    while changed:
        changed = False
        bounds = [c.start for c in cands] + [shot.end]
        for a, b in zip(bounds, bounds[1:], strict=False):
            if b - a > max_gap:
                q = max(1, (b - a) // 4)
                lo, hi = a - shot.start + q, b - shot.start - q
                mid = (a + b) // 2 - shot.start
                i = int(lo + np.argmax(s["score"][lo:hi])) if hi > lo else mid
                ra, rb = i, min(len(s["sharp"]) - 1, i + refine)
                img = int(ra + np.argmax(s["sharp"][ra:rb + 1]))
                cands.append(Candidate(shot.start + i, shot.start + img, float(s["score"][i]),
                                       ["coverage"]))
                cands.sort(key=lambda c: c.start)
                changed = True
                break
    return cands
