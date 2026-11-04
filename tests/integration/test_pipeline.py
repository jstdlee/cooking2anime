"""The v2 pipeline end to end on fake providers (TESTING.md §3 Integration, DESIGN_V2 §8)."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from c2a import app
from c2a.core import media
from c2a.core.errors import GateError, StageError
from c2a.core.manifest import Run
from c2a.core.pipeline import run_pipeline
from c2a.providers.fake import FakeASR, FakeOCR, fake_stack, read_wav
from tests.fixtures.make_fixtures import SPEECH_WORDS
from tests.integration.helpers import clone, to_gate


@pytest.fixture(scope="module")
def gated(fixtures: dict[str, Path], tmp_path_factory: pytest.TempPathFactory) -> Run:
    return to_gate(tmp_path_factory.mktemp("runs"), fixtures["cut"],
                   ingest__shot_detector="ffmpeg_scene")


def finish(run: Run, pick: str = "0:0", stack: Any = None) -> dict[str, Any]:
    app.approve(run, pick)
    assert run_pipeline(run, stack or fake_stack()) == "done"
    r: dict[str, Any] = json.loads(run.path("out", "report.json").read_text())
    return r


def peak_freq(path: Path) -> float:
    wav = path.with_suffix(".probe.wav")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-ac", "1", "-ar", "48000",
                    str(wav)], check=True)
    data, sr = read_wav(wav)
    spec = np.abs(np.fft.rfft(data[:, 0]))
    return float(np.fft.rfftfreq(len(data), 1 / sr)[int(np.argmax(spec))])


def test_gate_has_keys_samples_and_a_plan(gated: Run) -> None:
    v = app.view(gated)
    assert v["state"] == "awaiting_approval"
    keys = v["keys"]
    assert [k["start"] for k in keys if k["locked"]] == [0, 96]  # both shot starts
    assert all(k["tags"] for k in keys)
    assert v["style"]["samples_current"] and v["style"]["samples"]
    assert len(v["plan"]["clips"]) == sum(k["checked"] for k in keys)
    with pytest.raises(GateError, match="unknown sample"):
        app.approve(gated, "9:9")


def test_full_run_output_contract(gated: Run, tmp_path: Path, fixtures: dict[str, Path]) -> None:
    run = clone(gated, tmp_path)
    r = finish(run)
    ch = r["checks"]
    tl_dur = sum(c["clip_s"] for c in r["clips"])
    assert ch["frames"] == ch["frames_expected"] == round(Fraction(tl_dur).limit_denominator(
        100000) * 24)
    assert ch["duration_error_s"] <= 1 / 24 and ch["size"] == [1280, 720]
    assert ch["has_audio"] and abs(ch["loudness_lufs"] + 14) <= 1 and ch["true_peak_dbtp"] <= -1
    out = run.path(r["output"])
    assert abs(peak_freq(fixtures["cut"]) - 440) < 5  # the source's tone …
    assert abs(peak_freq(out) - 440) > 20  # … is gone: regenerate makes a new soundtrack
    assert r["style"] == {"animation": "theatrical_anime", "director": "food_commercial",
                          "finish": "grain_35mm"}


def test_checking_a_key_adds_exactly_one_clip_and_reuses_the_rest(gated: Run,
                                                                 tmp_path: Path) -> None:
    run = clone(gated, tmp_path)
    finish(run)
    units = run.read()["units"]
    before = {k: v["t"] for k, v in units.items() if k.startswith("generate/")}
    extra = next(k for k in app.view(run)["keys"] if not k["checked"])
    app.toggle_key(run, extra["start"], True)
    assert run_pipeline(run, fake_stack()) == "done"
    after = {k: v["t"] for k, v in run.read()["units"].items() if k.startswith("generate/")}
    new = set(after) - set(before)
    assert new == {f"generate/k{extra['start']:06d}"}
    # a clip whose end anchor changed is regenerated; clips in other shots are reused
    shot1 = [k for k in before if int(k.split("k")[-1]) >= 96 and int(k.split("k")[-1]) !=
             extra["start"]]
    if extra["start"] < 96:
        assert all(after[k] == before[k] for k in shot1)


def test_unchecking_a_locked_key_is_refused(gated: Run) -> None:
    with pytest.raises(GateError, match="cannot be unchecked"):
        app.toggle_key(gated, 0, False)


def test_added_key_is_detected_tagged_and_checked(gated: Run, tmp_path: Path) -> None:
    run = clone(gated, tmp_path)
    frame = app.add_key(run, 2.0)
    assert run_pipeline(run, fake_stack()) == "awaiting_approval"
    k = next(k for k in app.view(run)["keys"] if k["start"] == frame)
    assert k["user_added"] and k["checked"] and k["tags"]


def test_tag_edits_and_motion_override_reach_the_prompt(gated: Run, tmp_path: Path) -> None:
    run = clone(gated, tmp_path)
    app.edit_tags(run, 0, {"action": "stir", "motion": {"verb": "stir", "direction": "circular",
                                                       "speed": "slow"}})
    app.set_motion(run, 96, "both hands toss rice in the wok")
    with pytest.raises(GateError, match="invalid tags"):
        app.edit_tags(run, 0, {"camera": "drone"})
    finish(run)
    prompts = [json.loads(x) for x in run.path("generate", "prompts.jsonl").read_text()
               .splitlines()]
    by_key = {p["key"]: p for p in prompts}
    assert "stir the" in by_key[0]["positive"] and "circular" in by_key[0]["positive"]
    assert "both hands toss rice in the wok" in by_key[96]["positive"]


def test_style_change_needs_a_new_approval(gated: Run, tmp_path: Path) -> None:
    run = clone(gated, tmp_path)
    finish(run)
    app.set_style(run, director="ozu_inspired", finish="black_white")
    assert run_pipeline(run, fake_stack()) == "awaiting_approval"
    v = app.view(run)
    assert v["style"]["approval"]["current"] is False and v["style"]["samples_current"]
    r = finish(run, "1:1")
    assert r["style"]["director"] == "ozu_inspired"
    prompts = [json.loads(x) for x in run.path("generate", "prompts.jsonl").read_text()
               .splitlines()]
    assert {p["camera"] for p in prompts} == {"static"}  # Ozu's camera bias
    post = run.read()["units"]["post"]["metrics"]["finish"]
    assert "hue=s=0" in post  # black & white finish applied


def test_fixed_clip_length_mode(gated: Run, tmp_path: Path) -> None:
    run = clone(gated, tmp_path)
    app.set_clips(run, length_mode="fixed", fixed_s=1.5)
    r = finish(run)
    assert {c["clip_s"] for c in r["clips"]} == {1.5}
    assert r["checks"]["duration_s"] == pytest.approx(1.5 * len(r["clips"]))


def test_per_clip_length(gated: Run, tmp_path: Path) -> None:
    run = clone(gated, tmp_path)
    app.set_clips(run, length_mode="per_clip", per_clip={96: 4.0})
    r = finish(run)
    assert next(c for c in r["clips"] if c["id"] == "k000096")["clip_s"] == 4.0


def test_settings_are_validated(gated: Run) -> None:
    from c2a.core.errors import ConfigError

    with pytest.raises(ConfigError):
        app.set_style(gated, director="nope")
    with pytest.raises(ConfigError):
        app.set_audio(gated, "loud")
    with pytest.raises(ConfigError):
        app.update_settings(gated, {"run": {"stack": "local"}})


def test_rerun_clip_only_touches_that_clip(gated: Run, tmp_path: Path) -> None:
    run = clone(gated, tmp_path)
    finish(run)
    units = run.read()["units"]
    ids = sorted(k.split("/")[1] for k in units if k.startswith("generate/"))
    t = {i: units[f"generate/{i}"]["t"] for i in ids}
    gone = app.rerun(run, ids[0])
    assert f"generate/{ids[0]}" in gone and f"generate/{ids[-1]}" not in gone
    with pytest.raises(GateError):
        app.rerun(run, "k999999")
    assert run_pipeline(run, fake_stack()) == "done"
    units = run.read()["units"]
    assert units[f"generate/{ids[-1]}"]["t"] == t[ids[-1]]
    assert units[f"generate/{ids[0]}"]["t"] > t[ids[0]]


def test_crash_mid_generate_then_resume(gated: Run, tmp_path: Path) -> None:
    run = clone(gated, tmp_path)
    app.approve(run, "0:0")
    base = fake_stack()
    calls = {"n": 0}
    real = base.video_gen.i2v

    def flaky(*a: Any, **k: Any) -> Path:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated crash")
        return real(*a, **k)

    object.__setattr__(base.video_gen, "i2v", flaky)
    with pytest.raises(RuntimeError):
        run_pipeline(run, base)
    assert run.read()["state"] == "failed"
    first = next(v["t"] for k, v in run.read()["units"].items() if k.startswith("generate/"))
    assert run_pipeline(run, fake_stack()) == "done"
    again = next(v["t"] for k, v in run.read()["units"].items() if k.startswith("generate/"))
    assert again == first


def test_qa_retry_then_flag(gated: Run, tmp_path: Path) -> None:
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

    r = finish(clone(gated, tmp_path / "a"), stack=replace(fake_stack(), ocr=OCRFailsOnce()))
    assert all(c["attempts"] == 2 and c["status"] == "done" for c in r["clips"])
    r = finish(clone(gated, tmp_path / "b"), stack=replace(fake_stack(), ocr=OCRAlways()))
    assert {c["status"] for c in r["clips"]} == {"flagged"}
    assert r["flagged_clips"] == [c["id"] for c in r["clips"]]


def test_voice_word_guard(gated: Run, tmp_path: Path) -> None:
    class Talks(FakeASR):
        def count_words(self, wav: Path) -> int:
            return 2

    run = clone(gated, tmp_path)
    for k in app.view(run)["keys"]:
        if k["checked"]:
            app.edit_tags(run, k["start"], {"person_visible": True,
                                            "emotion": {"label": "satisfied", "intensity": 3}})
    with run.update() as m:
        m["config"]["audio"]["voice"]["min_gap_s"] = 0
    r = finish(run, stack=replace(fake_stack(), asr=Talks()))
    assert r["audio"]["voice_events"] == [] and r["audio"]["voice_dropped"]


def test_silent_mode(gated: Run, tmp_path: Path) -> None:
    run = clone(gated, tmp_path)
    app.set_audio(run, "silent")
    r = finish(run)
    assert r["checks"]["has_audio"] is False
    assert not media.probe(run.path(r["output"])).has_audio


@pytest.fixture(scope="module")
def speech_gated(fixtures: dict[str, Path], tmp_path_factory: pytest.TempPathFactory) -> Run:
    return to_gate(tmp_path_factory.mktemp("speech"), fixtures["speech"],
                   ingest__shot_detector="ffmpeg_scene")


def test_keep_voice_same_length_keeps_word_times(speech_gated: Run, tmp_path: Path) -> None:
    run = clone(speech_gated, tmp_path)
    app.set_audio(run, "keep_voice")
    r = finish(run)
    assert r["checks"]["has_audio"] and r["checks"]["audio_mode"] == "keep_voice"
    phrases = r["audio"]["phrases"]
    clips = r["clips"]
    # with clip ≈ segment length, each phrase keeps its offset from its key image
    for p in phrases:
        c = clips[p["segment"]]
        seg_start = c["src"][0]
        out_clip_start = sum(x["clip_s"] for x in clips[:p["segment"]])
        r_ratio = c["clip_s"] / (c["src"][1] - c["src"][0])
        if abs(r_ratio - 1) <= 0.15 and not p["cut_words"]:
            want = out_clip_start + (p["src_start"] - seg_start) * r_ratio
            assert p["out_start"] == pytest.approx(want, abs=0.01)
    starts = sorted(p["src_start"] for p in phrases)
    assert starts[0] == pytest.approx(SPEECH_WORDS[0][0], abs=0.03)
    # the output contains the voice tone (220 Hz), not silence
    assert abs(peak_freq(run.path(r["output"])) - 220) < 10


def test_keep_voice_short_fixed_clips_cut_words(speech_gated: Run, tmp_path: Path) -> None:
    run = clone(speech_gated, tmp_path)
    app.set_audio(run, "keep_voice")
    app.set_clips(run, length_mode="fixed", fixed_s=1.0)
    r = finish(run)
    assert r["audio"]["cut_words"]  # some words could not fit 1 s clips
    for p in r["audio"]["phrases"]:
        clip_end = sum(x["clip_s"] for x in r["clips"][:p["segment"] + 1])
        assert p["out_end"] <= clip_end + 1e-3  # never crosses into the next clip


def test_keep_voice_without_source_audio_is_unavailable(fixtures: dict[str, Path],
                                                        tmp_path: Path) -> None:
    run = to_gate(tmp_path, fixtures["vertical"], ingest__shot_detector="ffmpeg_scene",
                  audio__mode="keep_voice", audio__sfx__enabled=False)
    r = finish(run)
    assert r["checks"]["has_audio"] is False  # nothing to keep, nothing generated


@pytest.mark.parametrize(("name", "res", "size"), [
    ("vfr", "720p", (1280, 720)), ("vertical", "1080p", (1080, 1920))])
def test_other_inputs(fixtures: dict[str, Path], tmp_path: Path, name: str, res: str,
                      size: tuple[int, int]) -> None:
    run = to_gate(tmp_path, fixtures[name], output__resolution=res,
                  ingest__shot_detector="ffmpeg_scene")
    r = finish(run)
    assert tuple(r["checks"]["size"]) == size
    assert r["checks"]["frames"] == r["checks"]["frames_expected"]


def test_invalid_vlm_output_fails_loudly(gated: Run, tmp_path: Path) -> None:
    class BadVLM(type(fake_stack().vlm)):  # type: ignore[misc]
        def tag(self, *a: object, **k: object) -> object:
            return {"nonsense": True}

    run = clone(gated, tmp_path)
    for k in list(run.read()["units"]):
        if k.startswith("tag/"):
            run.invalidate(k)
    with pytest.raises(StageError, match="invalid tags"):
        run_pipeline(run, replace(fake_stack(), vlm=BadVLM()))
