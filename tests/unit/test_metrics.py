import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from c2a.qa.metrics import edge_ssim, flicker_proxy, loudness, ssim, style_sim_proxy


def img(seed: int = 0, h: int = 90, w: int = 160) -> np.ndarray:
    r = np.random.default_rng(seed)
    base = np.linspace(0, 255, w)[None, :, None] * np.ones((h, 1, 3))
    return np.clip(base + r.normal(0, 20, (h, w, 3)), 0, 255).astype(np.uint8)


def test_ssim_identity_and_difference() -> None:
    a = img(0)
    assert ssim(a, a) == pytest.approx(1.0)
    assert ssim(a, img(1)) < 0.9
    assert ssim(a, 255 - a) < 0.2


def test_edge_ssim_ignores_colour_shift() -> None:
    a = img(0)
    shifted = np.clip(a.astype(int) * [1.0, 0.8, 0.6], 0, 255).astype(np.uint8)
    assert edge_ssim(a, shifted) > 0.9
    assert edge_ssim(a, img(5)) < edge_ssim(a, shifted)


def test_style_proxy_bounds() -> None:
    a = img(0)
    assert style_sim_proxy(a, a) == pytest.approx(1.0)
    assert 0 <= style_sim_proxy(a, np.zeros_like(a)) < 0.5


def test_flicker_zero_for_static_and_smooth() -> None:
    a = img(0)
    assert flicker_proxy([a] * 5) == 0.0
    ramp = [np.full((32, 32, 3), v, np.uint8) for v in range(0, 100, 10)]
    assert flicker_proxy(ramp) == pytest.approx(0.0, abs=1e-9)
    flick = [np.full((32, 32, 3), v, np.uint8) for v in [0, 100] * 5]
    assert flicker_proxy(flick) == pytest.approx(200 / 255, rel=0.01)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg missing")
def test_loudness_of_reference_tone(tmp_path: Path) -> None:
    # 1 kHz sine at -20 dBFS in both channels reads -20 LUFS (ITU-R BS.1770 calibration).
    # ffmpeg's sine source peaks at 1/8 (-18.06 dBFS), so -1.94 dB gives -20 dBFS.
    p = tmp_path / "tone.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=1000:sample_rate=48000:duration=5",
                    "-af", "volume=-1.94dB,pan=stereo|c0=c0|c1=c0", str(p)], check=True)
    lufs, tp = loudness(p)
    assert lufs == pytest.approx(-20.0, abs=0.3)
    assert tp == pytest.approx(-20.0, abs=0.5)
