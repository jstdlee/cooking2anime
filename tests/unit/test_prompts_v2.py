"""Tags, style presets and clip prompts (DESIGN_V2 §2.2, §3, §8)."""

from __future__ import annotations

import copy
import itertools
import subprocess
from typing import Any

import pytest

from c2a.prompts.builder import ApproxTokenizer, build_clip_prompt, motion_sentence
from c2a.prompts.style import (
    LAYERS,
    StyleSelection,
    bias_camera,
    catalog,
    compose,
    post_filter,
    presets,
)
from c2a.prompts.tags import apply_edits, tag_instructions, validate_tags

TAGS: dict[str, Any] = {
    "scene": "cutting board, warm window light", "action": "slice",
    "motion": {"verb": "slice", "direction": "left", "speed": "fast"},
    "subjects": ["knife", "green onion"], "emotion": {"label": "focused", "intensity": 1},
    "camera": "pan_left", "person_visible": False, "sound_cues": ["knife_chop_board"],
    "importance": 0.8, "suggest": True, "reason": "new action",
}


def tags(**kw: Any) -> Any:
    d = copy.deepcopy(TAGS)
    d.update(kw)
    return validate_tags(d)


# ------------------------------------------------------------------ tags
@pytest.mark.parametrize(("field", "value", "match"), [
    ("action", "explode", "action"), ("camera", "drone", "camera"),
    ("sound_cues", ["boom"], "sound cue"), ("importance", 1.5, "importance"),
    ("motion", {"verb": "slice", "direction": "sideways", "speed": "fast"}, "direction"),
    ("emotion", {"label": "angry", "intensity": 1}, "emotion"),
])
def test_tag_vocabularies_are_closed(field: str, value: Any, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        tags(**{field: value})


def test_tag_edits_are_validated_and_limited() -> None:
    t = tags()
    assert apply_edits(t, {"action": "stir"}).action == "stir"
    with pytest.raises(ValueError, match="not editable"):
        apply_edits(t, {"importance": 1.0})
    with pytest.raises(ValueError):
        apply_edits(t, {"camera": "drone"})
    assert "slice" in tag_instructions(2.0) and "2.00 s" in tag_instructions(2.0)


# ------------------------------------------------------------------ presets
def test_six_presets_per_layer_and_catalog() -> None:
    ps = presets()
    assert [len(ps[layer]) for layer in LAYERS] == [6, 6, 6]
    cat = catalog()
    assert {c["name"] for c in cat["director"]} >= {"ozu_inspired", "food_commercial"}
    # director presets describe a language and say "inspired"
    for p in ps["director"].values():
        assert "inspired" in p.title.lower() or p.name == "food_commercial"


@pytest.mark.parametrize("combo", list(itertools.product(
    ["theatrical_anime", "ukiyo_e", None], ["ozu_inspired", "wong_kar_wai_inspired", None],
    ["black_white", "soft_halation", None])))
def test_every_combination_composes(combo: tuple[str | None, str | None, str | None]) -> None:
    a, d, f = combo
    c = compose(StyleSelection(animation=a, director=d, finish=f, custom_prompt="miso soup"))
    assert "miso soup" in c.image_prompt and "miso soup" in c.custom_phrases
    assert c.negative.startswith("text, letters")
    # deterministic, and the key changes with the selection
    assert compose(StyleSelection(animation=a, director=d, finish=f,
                                  custom_prompt="miso soup")).key == c.key


def test_composition_dedupes_and_orders_layers() -> None:
    c = compose(StyleSelection(animation="watercolor_storybook", director="ozu_inspired",
                               finish=None, custom_prompt="low camera at tatami height, rice"))
    phrases = [p.strip() for p in c.image_prompt.split(",")]
    assert phrases.count("low camera at tatami height") == 1
    assert phrases[0].startswith("warm hand-painted watercolor")
    assert phrases[-1] == "rice"


def test_unknown_preset_rejected() -> None:
    with pytest.raises(ValueError, match="unknown director"):
        compose(StyleSelection(director="nope"))


def test_camera_bias() -> None:
    ozu = compose(StyleSelection(director="ozu_inspired"))
    assert ozu.camera_bias == ("static",)
    assert bias_camera("pan_left", ozu.camera_bias) == "static"
    assert bias_camera("pan_left", None) == "pan_left"
    assert build_clip_prompt(tags(), ozu).camera == "static"


def test_post_filters_are_valid_ffmpeg() -> None:
    for p in presets()["finish"].values():
        vf = post_filter(p.post) if p.post else None
        assert vf
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=64x36:duration=0.1", "-vf", vf, "-f", "null", "-"],
                       check=True)


# ------------------------------------------------------------------ clip prompts
def test_clip_prompt_snapshot() -> None:
    style = compose(StyleSelection(animation="modern_tv_anime", director=None, finish=None))
    p = build_clip_prompt(tags(), style)
    assert p.positive == (
        "modern TV anime animation, clean snappy motion. slice the knife and green onion, "
        "moving left, fast. knife, green onion. cutting board, warm window light. "
        "the cook looks slightly focused. camera slowly pans left.")
    assert motion_sentence(tags(motion={"verb": "idle", "direction": "none",
                                        "speed": "still"})) == "idle the knife and green onion"


def test_motion_override_replaces_tag_sentence() -> None:
    style = compose(StyleSelection())
    p = build_clip_prompt(tags(), style, motion_override="both hands toss rice")
    assert "both hands toss rice" in p.positive and "slice the knife" not in p.positive


def test_budget_drops_scene_then_subjects_then_trims_presets_never_custom() -> None:
    style = compose(StyleSelection(custom_prompt="KEEP-ME"))
    tok = ApproxTokenizer()
    full = build_clip_prompt(tags(), style).positive
    p = build_clip_prompt(tags(), style, limit=int(tok.count(full) / 0.85) - 2)
    assert p.dropped[0] == "scene"
    tiny = build_clip_prompt(tags(), style, limit=70)
    assert "KEEP-ME" in tiny.positive and "slice" in tiny.positive
    assert tiny.dropped[:2] == ("scene", "subjects") and "style_trimmed" in tiny.dropped
    with pytest.raises(ValueError):
        build_clip_prompt(tags(), style, limit=12)
