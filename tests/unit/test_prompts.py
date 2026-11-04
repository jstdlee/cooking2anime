import copy
from typing import Any

import pytest

from c2a.prompts.builder import ApproxTokenizer, Preset, beats_sentence, build_prompt
from c2a.prompts.describe import ChunkDescription, instructions, validate_description

PRESET = Preset("t", "highly detailed, cinematic lighting", "3d render, cgi")
GOOD: dict[str, Any] = {
    "subjects": ["hands", "chef's knife", "green onion"],
    "setting": "home kitchen, warm window light from left",
    "food_state": "green onions, half sliced",
    "beats": [
        {"t0": 0.0, "t1": 2.1, "action": "right hand slices green onion in quick strokes."},
        {"t0": 2.1, "t1": 3.4, "action": "knife scrapes slices to the left of the board"},
    ],
    "gestures": ["left hand fingertips curled guiding the onion"],
    "emotion": {"label": "satisfied", "intensity": 2, "t": 3.0},
    "camera": "static",
    "person_visible": True,
    "sound_cues": [{"t0": 0.0, "t1": 2.1, "cue": "knife_chop_board"},
                   {"t0": 2.1, "t1": 3.4, "cue": "knife_scrape"}],
}


def good() -> dict[str, Any]:
    return copy.deepcopy(GOOD)


def test_valid_description_parses_and_sorts_beats() -> None:
    raw = good()
    raw["beats"].reverse()
    d = validate_description(raw, 3.4, 4)
    assert [b.t0 for b in d.beats] == [0.0, 2.1]


@pytest.mark.parametrize(("mutate", "match"), [
    (lambda r: r.update(camera="drone_shot"), "camera"),
    (lambda r: r["emotion"].update(label="angry"), "emotion"),
    (lambda r: r["sound_cues"][0].update(cue="explosion"), "sound cue"),
    (lambda r: r["beats"][1].update(t0=2.6), "gap"),
    (lambda r: r["beats"][1].update(t1=2.9), "clip is"),
    (lambda r: r["beats"][0].update(t0=0.5), "start"),
    (lambda r: r.update(extra="x"), "extra"),
    (lambda r: r["emotion"].update(intensity=5), "intensity"),
])
def test_invalid_descriptions_rejected(mutate: Any, match: str) -> None:
    raw = good()
    mutate(raw)
    with pytest.raises(ValueError, match=match):
        validate_description(raw, 3.4, 4)


def test_max_beats_enforced() -> None:
    with pytest.raises(ValueError, match="max"):
        validate_description(good(), 3.4, 1)


def test_instructions_list_vocabularies() -> None:
    text = instructions(3.4, 4)
    assert "knife_chop_board" in text and "push_in" in text and "3.40 s" in text


def desc() -> ChunkDescription:
    return validate_description(good(), 3.4, 4)


def test_prompt_snapshot() -> None:
    p = build_prompt(desc(), "Japanese anime cooking scene", "text, watermark", PRESET)
    assert p.positive == (
        "Japanese anime cooking scene. hands, chef's knife, green onion. green onions, "
        "half sliced. home kitchen, warm window light from left. first right hand slices "
        "green onion in quick strokes, then knife scrapes slices to the left of the board. "
        "left hand fingertips curled guiding the onion. the cook looks clearly satisfied. "
        "static camera. highly detailed, cinematic lighting.")
    assert p.negative == "3d render, cgi, text, watermark"
    assert p.dropped == ()


def test_prompt_is_deterministic() -> None:
    a = build_prompt(desc(), "style", "neg", PRESET)
    b = build_prompt(desc(), "style", "neg", PRESET)
    assert a == b


def test_override_replaces_beats_only() -> None:
    p = build_prompt(desc(), "STYLE", "", PRESET, beats_override="stirs the pot slowly")
    assert "stirs the pot slowly" in p.positive
    assert "slices" not in p.positive
    assert p.positive.startswith("STYLE.") and "satisfied" in p.positive


def test_drop_order_never_drops_beats_or_emotion() -> None:
    tok = ApproxTokenizer()
    full = build_prompt(desc(), "s", "", PRESET).positive
    limit = int(tok.count(full) / 0.85) - 4  # just over budget → drops gestures first
    p = build_prompt(desc(), "s", "", PRESET, limit=limit)
    assert p.dropped[0] == "gestures"
    assert "slices" in p.positive and "satisfied" in p.positive
    tiny = build_prompt(desc(), "s", "", PRESET, limit=80)
    assert tiny.dropped[:3] == ("gestures", "quality", "setting")
    assert "satisfied" in tiny.positive and tiny.tokens <= int(80 * 0.85)


def test_impossible_budget_raises() -> None:
    with pytest.raises(ValueError):
        build_prompt(desc(), "s", "", PRESET, limit=10)


def test_beats_sentence_forms() -> None:
    d = desc()
    assert beats_sentence(d).startswith("first ")
    three = good()
    three["beats"] = [{"t0": 0, "t1": 1, "action": "a b c"}, {"t0": 1, "t1": 2, "action": "d e"},
                      {"t0": 2, "t1": 3.4, "action": "f g"}]
    assert beats_sentence(validate_description(three, 3.4, 4)) == \
        "first a b c, then d e, finally f g"
