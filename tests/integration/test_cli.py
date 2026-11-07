"""CLI parity: every Studio action is a command (same c2a.app functions)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from c2a.cli import main


def only_run(workdir: Path) -> Path:
    (d,) = [p for p in workdir.iterdir() if p.is_dir() and (p / "manifest.json").exists()]
    return d


FAST = ["--stack", "fake", "--set", "ingest.shot_detector=ffmpeg_scene",
        "--set", "style.preview.sample_keys=1", "--set", "style.preview.seeds_per_key=2"]


def test_cli_end_to_end(fixtures: dict[str, Path], tmp_path: Path,
                        capsys: pytest.CaptureFixture[str]) -> None:
    wd = tmp_path / "runs"
    assert main(["run", str(fixtures["cut"]), "--workdir", str(wd), *FAST]) == 0
    run = only_run(wd)
    assert "c2a approve" in capsys.readouterr().out

    assert main(["keys", str(run), "--json"]) == 0
    keys = json.loads(capsys.readouterr().out)
    extra = next(k for k in keys if not k["checked"])
    assert main(["keys", str(run), "--check", str(extra["start"]), "--no-resume"]) == 0
    assert main(["keys", str(run), "--uncheck", "0"]) == 2  # locked shot start
    assert "cannot be unchecked" in capsys.readouterr().err
    assert main(["keys", str(run), "--add", "1.0", "--no-resume"]) == 0
    assert main(["keys", str(run), "--motion", "96", "toss the rice", "--no-resume"]) == 0
    assert main(["keys", str(run), "--tags", "0", '{"action": "stir"}', "--no-resume"]) == 0

    assert main(["style", "--list"]) == 0
    assert "ozu_inspired" in capsys.readouterr().out
    assert main(["style", str(run), "--director", "ozu_inspired", "--finish", "none",
                 "--prompt", "miso soup", "--no-resume"]) == 0
    assert main(["clips", str(run), "--mode", "fixed", "--fixed", "2", "--no-resume"]) == 0
    assert main(["audio", str(run), "silent", "--no-resume"]) == 0
    assert main(["resume", str(run)]) == 0
    capsys.readouterr()

    assert main(["approve", str(run), "--pick", "9:9"]) == 2
    assert main(["approve", str(run), "--pick", "0:1"]) == 0
    assert "done:" in capsys.readouterr().out

    assert main(["status", str(run), "--json"]) == 0
    v = json.loads(capsys.readouterr().out)
    assert v["state"] == "done" and v["settings"]["style"]["director"] == "ozu_inspired"
    assert {c["clip_s"] for c in v["plan"]["clips"]} == {2.0}
    assert v["output"]["checks"]["has_audio"] is False
    added = next(k for k in v["keys"] if k["user_added"])
    assert added["checked"]

    assert main(["clips", str(run)]) == 0
    assert "→ 2.00s" in capsys.readouterr().out
    clip = v["clips"][0]["id"]
    assert main(["open", str(run), "--print-only"]) == 0
    assert capsys.readouterr().out.strip().endswith("cut_720p.mp4")
    assert main(["open", str(run), "--clip", clip, "--print-only"]) == 0
    assert main(["rerun", str(run), "--clip", "k999999"]) == 2
    assert main(["rerun", str(run), "--clip", clip]) == 0


def test_doctor_and_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["doctor", "--stack", "fake"]) == 0
    assert "ffmpeg" in capsys.readouterr().out
    assert main(["doctor", "--stack", "local", "--strict"]) == 1
    assert main(["run", str(tmp_path / "missing.mp4"), "--stack", "fake"]) == 2
    assert main(["status", str(tmp_path)]) == 2
    assert main(["run", "x.mp4", "--set", "novalue"]) == 2


def test_default_stack_explains_what_is_missing(fixtures: dict[str, Path], tmp_path: Path,
                                                capsys: pytest.CaptureFixture[str],
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("GEMINI_API_KEY", "FAL_KEY", "FISH_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert main(["run", str(fixtures["cut"]), "--workdir", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "GEMINI_API_KEY" in err and "FAL_KEY" in err  # the cloud stack names its keys
    assert "--stack fake" in err
    assert not any(tmp_path.iterdir())
