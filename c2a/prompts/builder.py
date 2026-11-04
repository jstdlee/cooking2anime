"""Deterministic prompt assembly for one clip (DESIGN_V2 §2.5, §3).

positive = style video prompt + motion sentence (from the key image's tags, or the person's
           override) + subjects + scene + emotion + camera (after the director's bias)
negative = composed style negative

Over budget, parts are dropped in this order: scene, subjects. The motion sentence,
emotion and style are never dropped; if they alone exceed the budget, the style prompt is
trimmed phrase by phrase from the end of the preset phrases; the person's custom phrases
are never dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from c2a.prompts.style import ComposedStyle, bias_camera
from c2a.prompts.tags import KeyTags
from c2a.prompts.vocab import CAMERA, emotion_phrase

TEMPLATE_VERSION = 2
WAN_TEXT_LIMIT = 512
HEADROOM = 0.15


class Tokenizer(Protocol):
    def count(self, text: str) -> int: ...


class ApproxTokenizer:
    """Conservative stand-in for the umT5 tokenizer (over-estimates on purpose)."""

    _piece = re.compile(r"\w+|[^\w\s]")

    def count(self, text: str) -> int:
        return int(len(self._piece.findall(text)) * 1.35) + 1


@dataclass(frozen=True)
class BuiltPrompt:
    positive: str
    negative: str
    camera: str
    tokens: int
    dropped: tuple[str, ...]
    template_version: int = TEMPLATE_VERSION


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().rstrip(".,; ")


def motion_sentence(t: KeyTags) -> str:
    verb = t.motion.verb.replace("_", " ")
    what = " and ".join(_clean(s) for s in t.subjects[:2])
    s = f"{verb} the {what}" if what else verb
    if t.motion.direction != "none":
        s += f", moving {t.motion.direction.replace('_', ' ')}"
    if t.motion.speed != "still":
        s += f", {t.motion.speed}"
    return s


def build_clip_prompt(tags: KeyTags, style: ComposedStyle, *,
                      motion_override: str | None = None,
                      tokenizer: Tokenizer | None = None,
                      limit: int = WAN_TEXT_LIMIT) -> BuiltPrompt:
    tok = tokenizer or ApproxTokenizer()
    budget = int(limit * (1 - HEADROOM))
    camera = bias_camera(tags.camera, style.camera_bias)
    style_phrases = [p.strip() for p in style.video_prompt.split(",") if p.strip()]
    parts: dict[str, str] = {
        "style": ", ".join(style_phrases),
        "motion": _clean(motion_override) if motion_override else motion_sentence(tags),
        "subjects": ", ".join(_clean(s) for s in tags.subjects),
        "scene": _clean(tags.scene),
        "emotion": emotion_phrase(tags.emotion.label, tags.emotion.intensity),
        "camera": CAMERA[camera],
    }
    order = ["style", "motion", "subjects", "scene", "emotion", "camera"]

    def render() -> str:
        return ". ".join(parts[k] for k in order if parts.get(k)) + "."

    dropped: list[str] = []
    for victim in ("scene", "subjects"):
        if tok.count(render()) <= budget:
            break
        if parts.get(victim):
            parts[victim] = ""
            dropped.append(victim)
    custom = {c.lower() for c in style.custom_phrases}
    while tok.count(render()) > budget:
        preset_idx = [i for i, ph in enumerate(style_phrases) if ph.lower() not in custom]
        if len(preset_idx) <= 1:
            raise ValueError("prompt cannot fit the token budget")
        style_phrases.pop(preset_idx[-1])
        parts["style"] = ", ".join(style_phrases)
        if "style_trimmed" not in dropped:
            dropped.append("style_trimmed")
    positive = render()
    return BuiltPrompt(positive, style.negative, camera, tok.count(positive), tuple(dropped))
