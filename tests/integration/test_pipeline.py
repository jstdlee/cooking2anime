"""Whole pipeline on fake providers (TESTING.md §3 Integration)."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

from c2a.core import media
from c2a.core.errors import GateError, StageError
from c2a.core.manifest import Run
from c2a.core.pipeline import rerun_chunk, run_pipeline
from c2a.providers.fake import FakeASR, FakeOCR, fake_stack, read_wav
from c2a.stages.preview import approve
from tests.integration.helpers import clone, to_gate


@pytest.fixture(scope="module")
def gated(fixtures: dict[str, Path], tmp_path_factory: pytest.TempPathFactory) -> Run:
    return to_gate(tmp_path_factory.mktemp("runs"), fixtures["cut"])


def finish(run: Run, pick: str = "0:0", **kw: object) -> dict[str, object]:
    approve(run, pick)
    assert run_pipeline(run, kw.get("stack") or fake_stack()) == "done"  # type: ignore[arg-type]
    report: dict[str, object] = json.loads(run.path("out", "report.json").read_text())
    return report


def peak_freq(path: Path) -> float:
    wav = path.with_suffix(".probe.wav")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-ac", "1", "-ar", "48000",
                    str(wav)], check=True)
    data, sr = read_wav(wav)
    spec = np.abs(np.fft.rfft(data[:, 0]))
    return float(np.fft.rfftfreq(len(data), 1 / sr)[int(np.argmax(spec))])


def test_gate_stops_and_lists_candidates(gated: Run) -> None:
    m = gated.read()
    assert m["state"] == "awaiting_approval"
    cands = json.loads(gated.path("preview", "candidates.json").read_text())["candidates"]
    assert len(cands) == 4 * 3 or len(cands) == len({c["kf"] for c in cands}) * 3
    with pytest.raises(GateError, match="unknown candidate"):
        approve(gated, "9:9")


def test_full_run_output_contract(gated: Run, tmp_path: Path, fixtures: dict[str, Path]) -> None:
    run = clone(gated, tmp_path)
    report = finish(run)
    ch = report["checks"]
    assert isinstance(ch, dict)
    src = media.probe(fixtures["cut"], count_frames=True)
    expected = round(Fraction(src.frames) / src.fps * 24)
    assert ch["frames"] == ch["frames_expected"] == expected
    assert ch["duration_error_s"] <= 1 / 24
    assert ch["size"] == [1280, 720]
    assert ch["has_audio"] is True
    assert abs(ch["loudness_lufs"] + 14) <= 1 and ch["true_peak_dbtp"] <= -1
    # the source's 440 Hz tone must not survive: the soundtrack is new
    out = run.path(str(report["output"]))
    assert abs(peak_freq(fixtures["cut"]) - 440) < 5
    assert abs(peak_freq(out) - 440) > 20
    assert not report["flagged_chunks"]


def test_resume_reuses_cached_work(gated: Run, tmp_path: Path) -> None:
    run = clone(gated, tmp_path)
    finish(run)
    before = {k: v["t"] for k, v in run.read()["units"].items()}
    assert run_pipeline(run, fake_stack()) == "done"
    after = {k: v["t"] for k, v in run.read()["units"].items()}
    assert before == after  # nothing recomputed


def test_rerun_chunk_only_touches_that_chunk(gated: Run, tmp_path: Path) -> None:
    run = clone(gated, tmp_path)
    finish(run)
    units = run.read()["units"]
    chunk_ids = [k.split("/")[1] for k in units if k.startswith("generate/")]
    target, other = chunk_ids[0], chunk_ids[-1]
    t_other = units[f"generate/{other}"]["t"]
    t_target = units[f"generate/{target}"]["t"]
    gone = rerun_chunk(run, target)
    assert f"generate/{target}" in gone and f"generate/{other}" not in gone
    assert run_pipeline(run, fake_stack()) == "done"
    units = run.read()["units"]
    assert units[f"generate/{other}"]["t"] == t_other
    assert units[f"generate/{target}"]["t"] > t_target
    assert run.read()["reruns"][target] == 1


def test_crash_mid_generate_then_resume(gated: Run, tmp_path: Path) -> None:
    run = clone(gated, tmp_path)
    approve(run, "0:0")

    class Boom(Exception):
        pass

    base = fake_stack()
    calls = {"n": 0}
    real = base.video_gen.flf2v

    def flaky(*a: object, **k: object) -> Path:
        calls["n"] += 1
        if calls["n"] == 2:
            raise Boom("simulated crash")
        return real(*a, **k)  # type: ignore[arg-type]

    vg = base.video_gen
    object.__setattr__(vg, "flf2v", flaky)
    with pytest.raises(Boom):
        run_pipeline(run, base)
    assert run.read()["state"] == "failed"
    first = run.read()["units"]["generate/0.0"]["t"]
    assert run_pipeline(run, fake_stack()) == "done"
    assert run.read()["units"]["generate/0.0"]["t"] == first  # not regenerated


def test_qa_retry_then_pass_and_flag(gated: Run, tmp_path: Path) -> None:
    class OCRFailsOnce(FakeOCR):
        def __init__(self) -> None:
            self.seen: set[str] = set()

        def count_text_boxes(self, image: Path, min_conf: float) -> int:
            if "generate" in str(image) and image.name not in self.seen:
                self.seen.add(image.name)
                return 1
            return 0

    class OCRAlways(FakeOCR):
        def count_text_boxes(self, image: Path, min_conf: float) -> int:
            return 1 if "generate" in str(image) else 0

    run = clone(gated, tmp_path / "a")
    report = finish(run, stack=replace(fake_stack(), ocr=OCRFailsOnce()))
    assert all(c["attempts"] == 2 and c["status"] == "done" for c in report["chunks"])  # type: ignore[index, union-attr]

    run = clone(gated, tmp_path / "b")
    report = finish(run, stack=replace(fake_stack(), ocr=OCRAlways()))
    chunks = report["chunks"]
    assert isinstance(chunks, list)
    assert {c["status"] for c in chunks} == {"flagged"}
    assert all(c["attempts"] == 3 and c["metrics"]["simplified"] for c in chunks)
    assert report["flagged_chunks"] == [c["id"] for c in chunks]


def test_voice_word_guard_drops_speaking_clips(gated: Run, tmp_path: Path) -> None:
    class Talks(FakeASR):
        def count_words(self, wav: Path) -> int:
            return 2

    run = clone(gated, tmp_path)
    with run.update() as m:
        m["config"]["audio"]["voice"]["min_intensity"] = 0
    # make every chunk a visible, emotional moment
    for p in run.path("describe").glob("*.json"):
        d = json.loads(p.read_text())
        d.update(person_visible=True, emotion={"label": "satisfied", "intensity": 3, "t": 0.5})
        p.write_text(json.dumps(d))
    report = finish(run, stack=replace(fake_stack(), asr=Talks()))
    audio = report["audio"]
    assert isinstance(audio, dict)
    assert audio["voice_events"] == [] and len(audio["voice_dropped"]) >= 1


def test_silent_mode_has_no_audio_track(gated: Run, tmp_path: Path) -> None:
    run = clone(gated, tmp_path)
    with run.update() as m:
        m["config"]["audio"]["sfx"]["enabled"] = False
        m["config"]["audio"]["voice"]["enabled"] = False
    report = finish(run)
    assert report["checks"]["has_audio"] is False  # type: ignore[index]
    assert not media.probe(run.path(str(report["output"]))).has_audio


@pytest.mark.parametrize(("name", "res", "size"), [
    ("vfr", "720p", (1280, 720)), ("vertical", "1080p", (1080, 1920))])
def test_other_inputs(fixtures: dict[str, Path], tmp_path: Path, name: str, res: str,
                      size: tuple[int, int]) -> None:
    run = to_gate(tmp_path, fixtures[name], output__resolution=res)
    report = finish(run)
    ch = report["checks"]
    assert isinstance(ch, dict)
    assert tuple(ch["size"]) == size
    assert ch["frames"] == ch["frames_expected"]
    if name == "vertical":
        assert ch["has_audio"] is True  # a silent source still gets the new soundtrack


def test_corrupt_description_fails_loudly(gated: Run, tmp_path: Path) -> None:
    class BadVLM(type(fake_stack().vlm)):  # type: ignore[misc]
        def describe(self, *a: object, **k: object) -> object:
            return {"nonsense": True}

    run = clone(gated, tmp_path)
    for k in list(run.read()["units"]):
        if k.startswith("describe/"):
            run.invalidate(k)
    with pytest.raises(StageError, match="invalid descriptions"):
        run_pipeline(run, replace(fake_stack(), vlm=BadVLM()))
    assert run.read()["state"] == "failed"
