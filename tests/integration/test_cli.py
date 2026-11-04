"""The CLI drives the same engine as the board (TESTING.md §3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from c2a.board import tldr
from c2a.cli import main


def run_id(workdir: Path) -> Path:
    (only,) = [p for p in workdir.iterdir() if p.is_dir()]
    return only


def test_cli_end_to_end(fixtures: dict[str, Path], tmp_path: Path,
                        capsys: pytest.CaptureFixture[str]) -> None:
    wd = tmp_path / "runs"
    assert main(["run", str(fixtures["cut"]), "--stack", "fake", "--workdir", str(wd),
                 "--set", "style.preview.sample_keyframes=2"]) == 0
    run = run_id(wd)
    assert "c2a approve" in capsys.readouterr().out

    assert main(["status", str(run), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "awaiting_approval"

    assert main(["approve", str(run), "--pick", "9:9"]) == 2  # unknown candidate
    assert "unknown candidate" in capsys.readouterr().err

    assert main(["approve", str(run), "--pick", "1:2"]) == 0
    out = capsys.readouterr().out
    assert "done:" in out

    assert main(["status", str(run)]) == 0
    out = capsys.readouterr().out
    assert "done" in out and "approved: 1:2 via cli" in out

    assert main(["open", str(run), "--print-only"]) == 0
    assert capsys.readouterr().out.strip().endswith("cut_720p.mp4")
    assert main(["open", str(run), "--chunk", "0.0", "--print-only"]) == 0
    assert capsys.readouterr().out.strip().endswith("generate/0.0.mp4")

    assert main(["rerun", str(run), "--chunk", "7.7"]) == 2
    assert main(["rerun", str(run), "--chunk", "1.0", "--no-resume"]) == 0
    assert "generate/1.0" in capsys.readouterr().out
    assert main(["resume", str(run)]) == 0

    assert main(["board", "rebuild", str(run)]) == 0
    assert (run / "board.tldr").exists()


def test_watch_once_applies_board_approval(fixtures: dict[str, Path], tmp_path: Path,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    wd = tmp_path / "runs"
    main(["run", str(fixtures["vertical"]), "--stack", "fake", "--workdir", str(wd)])
    run = run_id(wd)
    board = run / "board.tldr"
    doc = json.loads(board.read_text())
    recs = doc["records"]
    sticker = next(r for r in recs if tldr.key_of(r) == "approve-sticker")
    frame = next(r for r in recs if tldr.key_of(r) == "gate-cand-0:1")
    sticker["parentId"], sticker["x"], sticker["y"] = frame["id"], 4.0, 4.0
    board.write_text(json.dumps(doc))
    capsys.readouterr()
    assert main(["watch", str(run), "--once"]) == 0
    out = capsys.readouterr().out
    assert "board: approved candidate 0:1 on the board" in out
    assert json.loads((run / "manifest.json").read_text())["state"] == "done"


def test_doctor_and_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["doctor", "--stack", "fake"]) == 0
    assert "ffmpeg" in capsys.readouterr().out
    assert main(["doctor", "--stack", "local", "--strict"]) == 1
    assert main(["run", str(tmp_path / "missing.mp4"), "--stack", "fake"]) == 2
    assert main(["run", str(tmp_path / "missing.mp4"), "--stack", "local"]) == 2
    assert main(["status", str(tmp_path)]) == 2  # not a run folder
    assert main(["run", "x.mp4", "--set", "novalue"]) == 2


def test_local_stack_explains_what_is_missing(fixtures: dict[str, Path], tmp_path: Path,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", str(fixtures["cut"]), "--workdir", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "M3" in err and "--stack fake" in err
    assert not any(tmp_path.iterdir())  # failed fast: no half-made run folder
