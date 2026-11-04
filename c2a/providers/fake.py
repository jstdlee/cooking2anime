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
from c2a.prompts.vocab import CAMERA, COOKING_VERBS, DIRECTIONS, EMOTION, SPEEDS, sound_cues
from c2a.providers.base import ProviderInfo, Stack, TimedCue, Word

SR = 48_000


def _info(name: str) -> ProviderInfo:
    return ProviderInfo(name=name, version="1", license="MIT", commercial_ok=True,
                        deterministic=True)


def _rng(*parts: object) -> np.random.Generator:
    h = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return np.random.default_rng(int.from_bytes(h[:8], "little"))


class FakeVLM:
    """Deterministic tags from the image content; importance from visible detail."""

    info = _info("fake-vlm")

    def tag(self, image: Path, clip: Path, duration: float, instructions: str,
            schema: dict[str, Any], seed: int) -> Any:
        img = media.load_png(image)
        r = _rng(file_digest(image), seed)
        cues = sorted(sound_cues())
        detail = float(np.abs(np.diff(img.astype(np.float32), axis=1)).mean())
        importance = round(min(1.0, 0.35 + detail / 40), 3)
        verb = COOKING_VERBS[int(r.integers(len(COOKING_VERBS) - 1))]
        return {
            "scene": "home kitchen with warm window light",
            "action": verb,
            "motion": {"verb": verb, "direction": DIRECTIONS[int(r.integers(len(DIRECTIONS)))],
                       "speed": SPEEDS[1 + int(r.integers(len(SPEEDS) - 1))]},
            "subjects": ["hands", "vegetables"],
            "emotion": {"label": EMOTION[int(r.integers(len(EMOTION)))],
                        "intensity": int(r.integers(4))},
            "camera": list(CAMERA)[int(r.integers(len(CAMERA)))],
            "person_visible": bool(r.integers(2)),
            "sound_cues": [cues[int(r.integers(len(cues)))]],
            "importance": importance,
            "suggest": importance >= 0.5,
            "reason": "deterministic fake tag",
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
    """i2v: slow 6% push-in on the image; flf2v: cross-fade first → last. Exact frame counts."""

    info = _info("fake-video-gen")

    def i2v(self, first: Path, frames: int, fps: int, prompt: str, negative: str, seed: int,
            dst: Path) -> Path:
        from PIL import Image

        a = media.load_png(first)
        h, w = a.shape[:2]
        im = Image.fromarray(a)

        def gen() -> Any:
            for i in range(frames):
                z = 1 + 0.06 * (i / max(1, frames - 1))
                cw, ch = w / z, h / z
                box = ((w - cw) / 2, (h - ch) / 2, (w + cw) / 2, (h + ch) / 2)
                yield np.asarray(im.resize((w, h), Image.Resampling.BILINEAR, box=box))

        media.write_video(gen(), dst, Fraction(fps), crf=8, preset="veryfast")
        return dst

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


class FakeSeparator:
    """Passes the mix through as the 'voice' stem (real separation: BS-RoFormer/Demucs)."""

    info = _info("fake-separator")

    def voice(self, mix: Path, dst: Path) -> Path:
        data, sr = read_wav(mix)
        write_wav(dst, data, sr)
        return dst


class EnergyWordTimer:
    """Energy-based voice activity + dip splitting: real behaviour for clean voice stems,
    standing in for WhisperX word timestamps (which also give the words' text)."""

    info = _info("energy-vad")
    frame_s = 0.02

    def words(self, wav: Path) -> list[Word]:
        data, sr = read_wav(wav)
        mono = data.mean(axis=1) if data.ndim == 2 else data
        hop = max(1, round(self.frame_s * sr))
        n = len(mono) // hop
        if n == 0:
            return []
        rms = np.sqrt((mono[: n * hop].reshape(n, hop) ** 2).mean(axis=1))
        peak = float(rms.max())
        if peak < 1e-4:
            return []
        thr = max(peak * 0.1, 1e-4)
        active = rms > thr
        out: list[Word] = []
        i = 0
        while i < n:
            if not active[i]:
                i += 1
                continue
            j = i
            while j < n and active[j]:
                j += 1
            if j - i >= 3:  # ≥ 60 ms
                out.append(Word(round(i * self.frame_s, 3), round(j * self.frame_s, 3),
                                f"w{len(out) + 1}"))
            i = j
        return out


def fake_stack() -> Stack:
    return Stack(vlm=FakeVLM(), image_edit=FakeImageEdit(), video_gen=FakeVideoGen(),
                 foley=FakeFoley(), voice=FakeVoiceFX(), asr=FakeASR(), ocr=FakeOCR(),
                 separator=FakeSeparator(), word_timer=EnergyWordTimer())
