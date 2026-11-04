"""Error paths and edge branches of core, board and prompts."""

from __future__ import annotations

import json
import subprocess
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

from c2a.board import tldr
from c2a.board.projector import Projection
from c2a.board.sync import merge, read_board
from c2a.core import media
from c2a.core.context import Context, load_preset, seed_for
from c2a.core.errors import BoardError, MediaError, StageError
from c2a.core.manifest import Run
from c2a.core.pipeline import make_context, run_pipeline
from c2a.prompts.describe import validate_description
from c2a.prompts.vocab import emotion_phrase
from c2a.providers.fake import fake_stack
from tests.unit.test_prompts import good


# ---------------------------------------------------------------- media
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
    b = np.zeros((16, 16, 3), np.uint8)
    with pytest.raises(MediaError, match="size changed"):
        media.write_video(iter([a, b]), tmp_path / "x.mp4", Fraction(16))


def test_extract_missing_frames(fixtures: dict[str, Path]) -> None:
    with pytest.raises(MediaError, match="frames not found"):
        media.extract_frames(fixtures["vertical"], [10_000], (90, 160))
    assert media.extract_frames(fixtures["vertical"], [], (90, 160)) == {}


def test_gen_size_orientation() -> None:
    assert media.gen_size(1920, 1080, "720p") == (1280, 720)
    assert media.gen_size(1080, 1920, "1080p") == (1080, 1920)


# ---------------------------------------------------------------- tldr
def test_index_keys_are_strictly_increasing_across_ranges() -> None:
    keys = [tldr.index_key(n) for n in [*range(0, 70), 3900, 3905, 250_000]]
    assert keys == sorted(keys) and len(set(keys)) == len(keys)
    assert keys[0] == "a1" and tldr.index_key(61).startswith("b")
    with pytest.raises(ValueError):
        tldr.index_key(-1)
    with pytest.raises(ValueError):
        tldr.index_key(62**5)


def test_rich_text_roundtrip_and_robustness() -> None:
    text = "line one\n\nline three"
    assert tldr.plain_text(tldr.rich_text(text)) == text
    assert tldr.plain_text(None) == ""
    assert tldr.plain_text({"type": "doc", "content": [{"type": "paragraph",
                                                         "content": ["junk"]}]}) == ""
    assert tldr.owner_of({"meta": "x"}) is None and tldr.key_of({}) is None


def test_note_grows_with_text() -> None:
    assert tldr.note_grow_y("short") == 0
    assert tldr.note_grow_y("x" * 300) > 100


# ---------------------------------------------------------------- vocab / describe
def test_emotion_phrase_rules() -> None:
    assert emotion_phrase("happy", 0) == "" and emotion_phrase("neutral", 3) == ""
    assert emotion_phrase("happy", 3) == "the cook looks very happy"
    with pytest.raises(ValueError):
        emotion_phrase("angry", 1)
    with pytest.raises(ValueError):
        emotion_phrase("happy", 4)


@pytest.mark.parametrize(("mutate", "match"), [
    (lambda r: r["sound_cues"][0].update(t1=9.0), "beyond clip end"),
    (lambda r: r["sound_cues"][0].update(t0=2.0, t1=1.0), "t1 must be"),
    (lambda r: r["emotion"].update(t=9.0), "emotion time"),
    (lambda r: r["beats"][0].update(t0=1.0, t1=0.5), "t1 must be"),
])
def test_more_invalid_descriptions(mutate: object, match: str) -> None:
    raw = good()
    mutate(raw)  # type: ignore[operator]
    with pytest.raises(ValueError, match=match):
        validate_description(raw, 3.4, 4)


# ---------------------------------------------------------------- board sync
def test_read_board_rejects_garbage(tmp_path: Path) -> None:
    p = tmp_path / "b.tldr"
    assert read_board(p) == ([], None)
    p.write_text("{not json")
    with pytest.raises(BoardError):
        read_board(p)
    p.write_text(json.dumps({"records": "nope"}))
    with pytest.raises(BoardError):
        read_board(p)


def test_merge_adds_document_and_keeps_user_edited_input() -> None:
    page = tldr.page("p", "P", 0)
    proj = Projection(engine=[page], inputs=[tldr.note("n", page["id"], 0, 0, "engine", 1)],
                      input_text={"n": "engine v2"})
    edited = tldr.note("n", page["id"], 0, 0, "person wrote this", 1)
    out, written = merge([edited], proj, {"n": "engine"})
    assert out[0]["id"] == tldr.DOCUMENT_ID
    n = next(r for r in out if tldr.key_of(r) == "n")
    assert tldr.plain_text(n["props"]["richText"]) == "person wrote this"
    assert written["n"] == "engine"  # unchanged: the engine did not write
    # untouched note follows the engine
    out, written = merge([tldr.note("n", page["id"], 0, 0, "engine", 1)], proj, {"n": "engine"})
    n = next(r for r in out if tldr.key_of(r) == "n")
    assert tldr.plain_text(n["props"]["richText"]) == "engine v2" and written["n"] == "engine v2"


# ---------------------------------------------------------------- context / pipeline
def test_context_errors(tmp_path: Path) -> None:
    with pytest.raises(StageError, match="unknown style preset"):
        load_preset("nope")
    src = tmp_path / "in.mp4"
    src.write_bytes(b"x")
    run = Run.create(tmp_path / "runs", src, {"run": {"stack": "fake"}})
    ctx = make_context(run, fake_stack())
    assert isinstance(ctx, Context)
    with pytest.raises(StageError, match="timeline"):
        _ = ctx.timeline
    with pytest.raises(StageError, match="not approved"):
        ctx.style_lock()
    assert seed_for("a", 1) == seed_for("a", 1) != seed_for("a", 2)
    with pytest.raises(StageError, match="not a run folder"):
        Run.open(tmp_path)


def test_unexpected_exception_marks_run_failed(tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "in.mp4"
    src.write_bytes(b"x")
    run = Run.create(tmp_path / "runs", src, {"run": {"stack": "fake"}})

    def boom(ctx: Context) -> None:
        raise ZeroDivisionError("bug")

    import c2a.core.pipeline as pl
    monkeypatch.setattr(pl, "PIPELINE", [("analyzing", boom)])
    with pytest.raises(ZeroDivisionError):
        run_pipeline(run, fake_stack(), project=False)
    assert run.read()["state"] == "failed"
    assert "ZeroDivisionError" in run.read()["events"][-1]["note"]


def test_second_runner_is_refused(tmp_path: Path) -> None:
    from c2a.core.pipeline import runner_lock

    src = tmp_path / "in.mp4"
    src.write_bytes(b"x")
    run = Run.create(tmp_path / "runs", src, {})
    with runner_lock(run), pytest.raises(StageError, match="already running"), \
            runner_lock(run):
        pass
