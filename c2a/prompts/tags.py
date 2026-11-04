"""Key image tags from the VLM (DESIGN_V2 §2.2): strict schema, closed vocabularies."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from c2a.prompts.vocab import CAMERA, COOKING_VERBS, DIRECTIONS, EMOTION, SPEEDS, sound_cues

TAG_INSTRUCTIONS = """\
You are tagging one key image from a cooking video, plus the clip that follows it
({duration:.2f} s). The image will be animated into a short clip, so describe the action
that starts at this image. Never mention on-screen text, captions, logos or watermarks.
Answer with JSON only:
- scene: short phrase (place, light)
- action: one of {verbs}
- motion: {{"verb": same list, "direction": one of {directions}, "speed": one of {speeds}}}
- subjects: 1-5 short nouns
- emotion: {{"label": one of {emotion}, "intensity": 0-3}}
- camera: one of {camera}
- person_visible: true if a face or upper body (not only hands) is visible
- sound_cues: ids from {cues}
- importance: 0-1, how clearly this image starts a new action or scene
- suggest: true if this should be a key image
- reason: one short sentence
"""


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Motion(_M):
    verb: str
    direction: str
    speed: str

    @model_validator(mode="after")
    def _vocab(self) -> Motion:
        if self.verb not in COOKING_VERBS:
            raise ValueError(f"motion.verb {self.verb!r} not in vocabulary")
        if self.direction not in DIRECTIONS:
            raise ValueError(f"motion.direction {self.direction!r} not in vocabulary")
        if self.speed not in SPEEDS:
            raise ValueError(f"motion.speed {self.speed!r} not in vocabulary")
        return self


class TagEmotion(_M):
    label: str
    intensity: int = Field(ge=0, le=3)

    @model_validator(mode="after")
    def _vocab(self) -> TagEmotion:
        if self.label not in EMOTION:
            raise ValueError(f"emotion.label {self.label!r} not in vocabulary")
        return self


class KeyTags(_M):
    scene: str = Field(max_length=160)
    action: str
    motion: Motion
    subjects: list[str] = Field(min_length=1, max_length=5)
    emotion: TagEmotion
    camera: str
    person_visible: bool
    sound_cues: list[str]
    importance: float = Field(ge=0, le=1)
    suggest: bool
    reason: str = Field(max_length=200)

    @model_validator(mode="after")
    def _vocab(self) -> KeyTags:
        if self.action not in COOKING_VERBS:
            raise ValueError(f"action {self.action!r} not in vocabulary")
        if self.camera not in CAMERA:
            raise ValueError(f"camera {self.camera!r} not in vocabulary")
        bad = [c for c in self.sound_cues if c not in sound_cues()]
        if bad:
            raise ValueError(f"sound cue {bad[0]!r} not in vocabulary")
        return self


EDITABLE = ("scene", "action", "motion", "subjects", "emotion", "camera", "person_visible",
            "sound_cues")


def validate_tags(raw: Any) -> KeyTags:
    try:
        return KeyTags.model_validate(raw)
    except ValidationError as e:
        raise ValueError(str(e)) from e


def apply_edits(tags: KeyTags, edits: dict[str, Any]) -> KeyTags:
    """Person's tag edits on top of the VLM's tags; same validation."""
    unknown = set(edits) - set(EDITABLE)
    if unknown:
        raise ValueError(f"tags not editable: {sorted(unknown)}")
    return validate_tags({**tags.model_dump(), **edits})


def tag_instructions(duration: float) -> str:
    return TAG_INSTRUCTIONS.format(
        duration=duration, verbs=", ".join(COOKING_VERBS), directions=", ".join(DIRECTIONS),
        speeds=", ".join(SPEEDS), emotion=", ".join(EMOTION), camera=", ".join(CAMERA),
        cues=", ".join(sound_cues()))
