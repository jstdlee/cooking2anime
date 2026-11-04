"""Deterministic prompt assembly for one chunk (design §3.3).

positive = style + scene + beats + gestures + emotion + camera + quality suffix
negative = preset negative + style negative

Over budget, parts are dropped in this order: gestures, quality suffix, setting.
Beats and emotion are never dropped; if they alone exceed the budget, beats are shortened
word by word from the end of the longest beat.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from c2a.prompts.describe import ChunkDescription
from c2a.prompts.vocab import CAMERA, emotion_phrase

TEMPLATE_VERSION = 1
WAN_TEXT_LIMIT = 512
HEADROOM = 0.15


class Tokenizer(Protocol):
    def count(self, text: str) -> int: ...


class ApproxTokenizer:
    """Conservative stand-in for the umT5 tokenizer: ~1.35 tokens per word piece.

    Real models plug in their own tokenizer (M3); this one over-estimates on purpose.
    """

    _piece = re.compile(r"\w+|[^\w\s]")

    def count(self, text: str) -> int:
        return int(len(self._piece.findall(text)) * 1.35) + 1


@dataclass(frozen=True)
class Preset:
    name: str
    quality_suffix: str
    negative: str


@dataclass(frozen=True)
class BuiltPrompt:
    positive: str
    negative: str
    tokens: int
    dropped: tuple[str, ...]
    template_version: int = TEMPLATE_VERSION


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().rstrip(".,; ")


def beats_sentence(d: ChunkDescription) -> str:
    actions = [_clean(b.action) for b in d.beats]
    if len(actions) == 1:
        return actions[0]
    if len(actions) == 2:
        return f"first {actions[0]}, then {actions[1]}"
    middle = ", then ".join(actions[1:-1])
    return f"first {actions[0]}, then {middle}, finally {actions[-1]}"


def build_prompt(
    d: ChunkDescription,
    style_prompt: str,
    style_negative: str,
    preset: Preset,
    *,
    beats_override: str | None = None,
    tokenizer: Tokenizer | None = None,
    limit: int = WAN_TEXT_LIMIT,
) -> BuiltPrompt:
    tok = tokenizer or ApproxTokenizer()
    budget = int(limit * (1 - HEADROOM))
    beats = _clean(beats_override) if beats_override else beats_sentence(d)
    parts: dict[str, str] = {
        "style": _clean(style_prompt),
        "subjects": ", ".join(_clean(s) for s in d.subjects),
        "food_state": _clean(d.food_state),
        "setting": _clean(d.setting),
        "beats": beats,
        "gestures": "; ".join(_clean(g) for g in d.gestures),
        "emotion": emotion_phrase(d.emotion.label, d.emotion.intensity),
        "camera": CAMERA[d.camera],
        "quality": _clean(preset.quality_suffix),
    }
    order = ["style", "subjects", "food_state", "setting", "beats", "gestures", "emotion",
             "camera", "quality"]

    def render() -> str:
        return ". ".join(parts[k] for k in order if parts.get(k)) + "."

    dropped: list[str] = []
    for victim in ("gestures", "quality", "setting"):
        if tok.count(render()) <= budget:
            break
        if parts.get(victim):
            parts[victim] = ""
            dropped.append(victim)
    while tok.count(render()) > budget:
        words = parts["beats"].split()
        if len(words) <= 3:
            raise ValueError("prompt cannot fit the token budget even with minimal beats")
        parts["beats"] = " ".join(words[:-1])
        if "beats_trimmed" not in dropped:
            dropped.append("beats_trimmed")

    positive = render()
    negative = ", ".join(x for x in (_clean(preset.negative), _clean(style_negative)) if x)
    return BuiltPrompt(positive, negative, tok.count(positive), tuple(dropped))
