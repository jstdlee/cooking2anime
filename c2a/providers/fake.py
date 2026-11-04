"""Deterministic, CPU-only providers. Pure functions of their inputs (+ seed).

They exist so that the whole pipeline (timeline, cache, QA, board, mux) runs in CI.
They make no claim about visual or audio quality.
"""

from __future__ import annotations

import hashlib
import wave
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np

from c2a.core import media
from c2a.core.manifest import file_digest
from c2a.prompts.vocab import CAMERA, EMOTION, sound_cues
from c2a.providers.base import ProviderInfo, Stack, TimedCue

SR = 48_000


def _info(name: str) -> ProviderInfo:
    return ProviderInfo(name=name, version="1", license="MIT", commercial_ok=True,
                        deterministic=True)


def _rng(*parts: object) -> np.random.Generator:
    h = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return np.random.default_rng(int.from_bytes(h[:8], "little"))


class FakeVLM:
    info = _info("fake-vlm")

    def describe(self, clip: Path, duration: float, instructions: str,
                 schema: dict[str, Any], seed: int) -> Any:
        r = _rng(file_digest(clip), seed)
        cues = sorted(sound_cues())
        mid = round(duration / 2, 3)
        label = EMOTION[int(r.integers(len(EMOTION)))]
        return {
            "subjects": ["hands", "chef's knife", "vegetables", "wooden board"],
            "setting": "home kitchen with warm window light",
            "food_state": "vegetables being prepared",
            "beats": [
                {"t0": 0.0, "t1": mid, "action": "right hand slices vegetables in even strokes"},
                {"t0": mid, "t1": round(duration, 3),
                 "action": "knife gathers the slices to one side of the board"},
            ],
            "gestures": ["left hand fingertips curled to guide the food"],
            "emotion": {"label": label, "intensity": int(r.integers(4)),
                        "t": round(duration * 0.5, 3)},
            "camera": list(CAMERA)[int(r.integers(len(CAMERA)))],
            "person_visible": bool(r.integers(2)),
            "sound_cues": [
                {"t0": 0.0, "t1": mid, "cue": cues[int(r.integers(len(cues)))]},
                {"t0": mid, "t1": round(duration, 3), "cue": cues[int(r.integers(len(cues)))]},
            ],
        }

    def judge_motion(self, clip: Path, beats: list[str], seed: int) -> float:
        return 0.9


class FakeImageEdit:
    """Posterize + palette shift: a cheap, deterministic stand-in for an anime restyle."""

    info = _info("fake-image-edit")

    def edit(self, src: Path, refs: list[Path], prompt: str, negative: str, seed: int,
             dst: Path) -> Path:
        x = media.load_png(src).astype(np.float64)
        levels = 6
        x = np.round(x / 255 * (levels - 1)) / (levels - 1) * 255
        r = _rng(prompt, seed)
        tint = np.array([1.0 + r.uniform(0, 0.06), 1.0, 1.0 - r.uniform(0, 0.06)])
        media.save_png(np.clip(x * tint, 0, 255).astype(np.uint8), dst)
        return dst


class FakeVideoGen:
    """Linear cross-fade first → last with exactly `frames` frames."""

    info = _info("fake-video-gen")

    def flf2v(self, first: Path, last: Path, frames: int, fps: int, prompt: str,
              negative: str, seed: int, dst: Path) -> Path:
        a = media.load_png(first).astype(np.float32)
        b = media.load_png(last).astype(np.float32)
        if a.shape != b.shape:
            raise ValueError("first/last keyframes differ in size")

        def gen() -> Any:
            for i in range(frames):
                w = i / (frames - 1) if frames > 1 else 0.0
                yield np.clip(a * (1 - w) + b * w, 0, 255).astype(np.uint8)

        media.write_video(gen(), dst, Fraction(fps), crf=8, preset="veryfast")
        return dst


def write_wav(path: Path, data: np.ndarray, sr: int = SR) -> None:
    """float32 [-1, 1], shape (n, 2) → 16-bit PCM WAV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(data, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        raw = w.readframes(w.getnframes())
    data = np.frombuffer(raw, "<i2").astype(np.float32) / 32767
    data = data.reshape(-1, ch)
    if ch == 1:
        data = np.repeat(data, 2, axis=1)
    return data, sr


class FakeFoley:
    """Filtered noise bursts per cue, deterministic per cue id."""

    info = _info("fake-foley")

    def foley(self, video: Path, cues: list[TimedCue], duration: float, seed: int,
              dst: Path) -> Path:
        n = round(duration * SR)
        out = np.zeros((n, 2), np.float32)
        for c in cues:
            a, b = round(c.t0 * SR), min(n, round(c.t1 * SR))
            if b <= a:
                continue
            r = _rng(c.cue, seed)
            k = int(r.integers(2, 40))
            noise = np.convolve(r.standard_normal(b - a), np.ones(k) / k, mode="same")
            rate = r.uniform(2, 9)
            t = np.arange(b - a) / SR
            env = 0.5 + 0.5 * np.sin(2 * np.pi * rate * t)
            out[a:b] += (0.2 * noise * env)[:, None]
        write_wav(dst, out)
        return dst


class FakeVoiceFX:
    info = _info("fake-voice-fx")

    def vocal(self, direction: str, voice_ref: str | None, seed: int, dst: Path) -> Path:
        r = _rng(direction, voice_ref, seed)
        dur = 0.7
        t = np.arange(round(dur * SR)) / SR
        f0 = r.uniform(160, 240)
        env = np.sin(np.pi * t / dur) ** 2
        tone = 0.3 * env * np.sin(2 * np.pi * f0 * t * (1 - 0.1 * t))
        write_wav(dst, np.repeat(tone[:, None].astype(np.float32), 2, axis=1))
        return dst


class FakeASR:
    info = _info("fake-asr")

    def count_words(self, wav: Path) -> int:
        return 0


class FakeOCR:
    info = _info("fake-ocr")

    def count_text_boxes(self, image: Path, min_conf: float) -> int:
        return 0


def fake_stack() -> Stack:
    return Stack(vlm=FakeVLM(), image_edit=FakeImageEdit(), video_gen=FakeVideoGen(),
                 foley=FakeFoley(), voice=FakeVoiceFX(), asr=FakeASR(), ocr=FakeOCR())
