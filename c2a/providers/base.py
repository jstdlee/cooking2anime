"""Provider interfaces (design §4). One protocol per capability, many backends."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ProviderInfo:
    name: str
    version: str
    license: str
    commercial_ok: bool
    deterministic: bool


@dataclass(frozen=True)
class TimedCue:
    t0: float
    t1: float
    phrase: str
    cue: str


@runtime_checkable
class VLM(Protocol):
    info: ProviderInfo

    def describe(self, clip: Path, duration: float, instructions: str,
                 schema: dict[str, Any], seed: int) -> Any: ...

    def judge_motion(self, clip: Path, beats: list[str], seed: int) -> float: ...


@runtime_checkable
class ImageEdit(Protocol):
    info: ProviderInfo

    def edit(self, src: Path, refs: list[Path], prompt: str, negative: str, seed: int,
             dst: Path) -> Path: ...


@runtime_checkable
class VideoGen(Protocol):
    info: ProviderInfo

    def flf2v(self, first: Path, last: Path, frames: int, fps: int, prompt: str,
              negative: str, seed: int, dst: Path) -> Path: ...


@runtime_checkable
class Foley(Protocol):
    info: ProviderInfo

    def foley(self, video: Path, cues: list[TimedCue], duration: float, seed: int,
              dst: Path) -> Path: ...


@runtime_checkable
class VoiceFX(Protocol):
    info: ProviderInfo

    def vocal(self, direction: str, voice_ref: str | None, seed: int, dst: Path) -> Path: ...


@runtime_checkable
class ASR(Protocol):
    info: ProviderInfo

    def count_words(self, wav: Path) -> int: ...


@runtime_checkable
class OCR(Protocol):
    info: ProviderInfo

    def count_text_boxes(self, image: Path, min_conf: float) -> int: ...


@dataclass(frozen=True)
class Stack:
    vlm: VLM
    image_edit: ImageEdit
    video_gen: VideoGen
    foley: Foley
    voice: VoiceFX
    asr: ASR
    ocr: OCR

    def all(self) -> list[Any]:
        return [self.vlm, self.image_edit, self.video_gen, self.foley, self.voice, self.asr,
                self.ocr]
