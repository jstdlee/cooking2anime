"""Typed chunk description + strict validation of VLM output."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from c2a.prompts.vocab import CAMERA, EMOTION, sound_cues

# beats must cover [0, duration] with at most this gap/overlap (seconds)
COVER_TOLERANCE_S = 0.15

VLM_INSTRUCTIONS = """\
You are describing one short clip from a cooking video so it can be re-animated.
Describe only what physically happens. Never mention on-screen text, captions, logos,
watermarks, UI or subtitles.
Answer with JSON only, matching the schema. Rules:
- times are seconds from the clip start; the clip lasts {duration:.2f} s
- 1 to {max_beats} beats; together they cover the whole clip without gaps
- each beat action: one concrete sentence about hands, tools and food
- camera: one of {camera}
- emotion.label: one of {emotion}; intensity 0-3; t = when it is clearest
- sound_cues[].cue: only ids from this list: {cues}
"""


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Beat(_M):
    t0: float = Field(ge=0)
    t1: float = Field(gt=0)
    action: str = Field(min_length=3, max_length=240)

    @model_validator(mode="after")
    def _order(self) -> Beat:
        if self.t1 <= self.t0:
            raise ValueError("beat t1 must be > t0")
        return self


class Emotion(_M):
    label: str
    intensity: int = Field(ge=0, le=3)
    t: float = Field(ge=0)

    @model_validator(mode="after")
    def _vocab(self) -> Emotion:
        if self.label not in EMOTION:
            raise ValueError(f"emotion.label {self.label!r} not in vocabulary")
        return self


class SoundCue(_M):
    t0: float = Field(ge=0)
    t1: float = Field(gt=0)
    cue: str

    @model_validator(mode="after")
    def _vocab(self) -> SoundCue:
        if self.cue not in sound_cues():
            raise ValueError(f"sound cue {self.cue!r} not in vocabulary")
        if self.t1 <= self.t0:
            raise ValueError("cue t1 must be > t0")
        return self


class ChunkDescription(_M):
    subjects: list[str] = Field(min_length=1)
    setting: str
    food_state: str
    beats: list[Beat] = Field(min_length=1, max_length=8)
    gestures: list[str]
    emotion: Emotion
    camera: str
    person_visible: bool
    sound_cues: list[SoundCue]

    @model_validator(mode="after")
    def _camera(self) -> ChunkDescription:
        if self.camera not in CAMERA:
            raise ValueError(f"camera {self.camera!r} not in vocabulary")
        return self


def validate_description(raw: Any, duration: float, max_beats: int) -> ChunkDescription:
    """Parse + check coverage. Raises ValueError with a readable reason."""
    try:
        d = ChunkDescription.model_validate(raw)
    except ValidationError as e:
        raise ValueError(str(e)) from e
    if len(d.beats) > max_beats:
        raise ValueError(f"{len(d.beats)} beats > max {max_beats}")
    beats = sorted(d.beats, key=lambda b: b.t0)
    if beats[0].t0 > COVER_TOLERANCE_S:
        raise ValueError("beats do not start at the clip start")
    if abs(beats[-1].t1 - duration) > COVER_TOLERANCE_S:
        raise ValueError(f"beats end at {beats[-1].t1:.2f}s, clip is {duration:.2f}s")
    for a, b in zip(beats, beats[1:], strict=False):
        if abs(b.t0 - a.t1) > COVER_TOLERANCE_S:
            raise ValueError(f"gap/overlap between beats at {a.t1:.2f}s")
    for c in d.sound_cues:
        if c.t1 > duration + COVER_TOLERANCE_S:
            raise ValueError("sound cue beyond clip end")
    if d.emotion.t > duration + COVER_TOLERANCE_S:
        raise ValueError("emotion time beyond clip end")
    return d.model_copy(update={"beats": beats})


def instructions(duration: float, max_beats: int) -> str:
    return VLM_INSTRUCTIONS.format(
        duration=duration, max_beats=max_beats, camera=", ".join(CAMERA),
        emotion=", ".join(EMOTION), cues=", ".join(sound_cues()))
