"""Closed vocabularies. The VLM must answer inside these; anything else is rejected."""

from __future__ import annotations

from functools import cache
from importlib import resources
from typing import Final

import yaml

CAMERA: Final[dict[str, str]] = {
    "static": "static camera",
    "pan_left": "camera slowly pans left",
    "pan_right": "camera slowly pans right",
    "tilt_up": "camera slowly tilts up",
    "tilt_down": "camera slowly tilts down",
    "push_in": "camera slowly pushes in",
    "pull_out": "camera slowly pulls out",
    "handheld": "subtle handheld camera movement",
}

EMOTION: Final[tuple[str, ...]] = (
    "neutral", "focused", "calm", "happy", "delighted", "surprised", "satisfied",
)

# phrase per (label, intensity 0..3); intensity 0 adds nothing
_INTENSITY: Final[tuple[str, ...]] = ("", "slightly", "clearly", "very")


def emotion_phrase(label: str, intensity: int) -> str:
    if label not in EMOTION:
        raise ValueError(f"unknown emotion {label!r}")
    if not 0 <= intensity <= 3:
        raise ValueError("intensity must be 0..3")
    if intensity == 0 or label == "neutral":
        return ""
    return f"the cook looks {_INTENSITY[intensity]} {label}"


@cache
def sound_cues() -> dict[str, str]:
    """cue id → foley prompt phrase (c2a/prompts/sound_cues.yaml)."""
    text = resources.files("c2a.prompts").joinpath("sound_cues.yaml").read_text()
    data = yaml.safe_load(text)
    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in data.items()
    ):
        raise ValueError("sound_cues.yaml must map cue id → phrase")
    return data
