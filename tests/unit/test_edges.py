"""Error paths and edge branches of core, keys and prompts."""

from __future__ import annotations

import subprocess
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

from c2a.config import check_settings, load_config
from c2a.core import media
from c2a.core.context import Context, seed_for
from c2a.core.errors import ConfigError, GateError, MediaError, StageError
from c2a.core.manifest import Run
from c2a.core.pipeline import make_context, run_pipeline, runner_lock
from c2a.keys import state as ks
from c2a.prompts.vocab import emotion_phrase
from c2a.providers.fake import fake_stack


def test_probe_errors(tmp_path: Path) -> None:
    with pytest.raises(MediaError, match="no such file"):
        media.probe(tmp_path / "nope.mp4")
    wav = tmp_path / "a.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "sine=duration=0.2", str(wav)], check=True)
    with pytest.raises(MediaError, match="no video stream"):
        media.probe(wav)
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video")
    with pytest.raises(MediaError, match="ffprobe failed"):
        media.probe(bad)


def test_write_video_errors(tmp_path: Path) -> None:
    with pytest.raises(MediaError, match="no frames"):
        media.write_video(iter([]), tmp_path / "x.mp4", Fraction(16))
    a = np.zeros((32, 32, 3), np.uint8)
    with pytest.raises(MediaError, match="size changed"):
        media.write_video(iter([a, np.zeros((16, 16, 3), np.uint8)]), tmp_path / "x.mp4",
                          Fraction(16))


def test_extract_missing_frames_and_gray(fixtures: dict[str, Path]) -> None:
    with pytest.raises(MediaError, match="frames not found"):
        media.extract_frames(fixtures["vertical"], [10_000], (90, 160))
    assert media.extract_frames(fixtures["vertical"], [], (90, 160)) == {}
    g = next(media.read_gray(fixtures["vertical"], 64))
    assert g.dtype == np.uint8 and g.shape[1] == 64 and g.shape[0] % 2 == 0


def test_gen_size_orientation() -> None:
    assert media.gen_size(1920, 1080, "720p") == (1280, 720)
    assert media.gen_size(1080, 1920, "1080p") == (1080, 1920)


def test_emotion_phrase_rules() -> None:
    assert emotion_phrase("happy", 0) == "" and emotion_phrase("neutral", 3) == ""
    assert emotion_phrase("happy", 3) == "the cook looks very happy"
    with pytest.raises(ValueError):
        emotion_phrase("angry", 1)
    with pytest.raises(ValueError):
        emotion_phrase("happy", 4)


def test_settings_whitelist() -> None:
    check_settings({"style": {"selection": {}}, "audio": {"mode": "silent"}})
    with pytest.raises(ConfigError, match="cannot be changed"):
        check_settings({"run": {"stack": "local"}})
    with pytest.raises(ConfigError, match="not changeable"):
        check_settings({"audio": {"loudness_lufs": -20}})


def _run(tmp_path: Path) -> Run:
    src = tmp_path / "in.mp4"
    src.write_bytes(b"x")
    return Run.create(tmp_path / "runs", src,
                      load_config(None, {"run": {"stack": "fake"}}).model_dump(mode="json"))


def test_context_errors(tmp_path: Path) -> None:
    run = _run(tmp_path)
    ctx = make_context(run, fake_stack())
    assert isinstance(ctx, Context)
    with pytest.raises(StageError, match="ingest"):
        ctx.meta()
    with pytest.raises(StageError, match="not approved"):
        ctx.style_lock()
    assert seed_for("a", 1) == seed_for("a", 1) != seed_for("a", 2)
    with pytest.raises(StageError, match="not a run folder"):
        Run.open(tmp_path)


def test_key_state_errors(tmp_path: Path) -> None:
    run = _run(tmp_path)
    with pytest.raises(GateError, match="no key image"):
        ks.set_checked(run, 5, True)
    with pytest.raises(GateError, match="no key image"):
        ks.set_motion(run, 5, "x")
    assert ks.key_states(run, 0.6) == []


def test_unexpected_exception_marks_run_failed(tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run(tmp_path)

    def boom(ctx: Context) -> None:
        raise ZeroDivisionError("bug")

    import c2a.core.pipeline as pl
    monkeypatch.setattr(pl, "PIPELINE", [("ingesting", boom)])
    with pytest.raises(ZeroDivisionError):
        run_pipeline(run, fake_stack())
    assert run.read()["state"] == "failed"
    assert "ZeroDivisionError" in run.read()["events"][-1]["note"]


def test_cancel_stops_at_the_next_unit(tmp_path: Path) -> None:
    run = _run(tmp_path)
    ctx = make_context(run, fake_stack(), cancelled=lambda: True)
    with pytest.raises(StageError, match="cancelled"):
        ctx.tick("x", "y", 0.5)


def test_second_runner_is_refused(tmp_path: Path) -> None:
    run = _run(tmp_path)
    with runner_lock(run), pytest.raises(StageError, match="already running"), \
            runner_lock(run):
        pass
