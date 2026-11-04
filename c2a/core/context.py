"""Everything a stage needs, loaded once per pipeline invocation (v2)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from c2a.config import Config, deep_merge
from c2a.core.errors import StageError
from c2a.core.manifest import Run
from c2a.core.timeline import ClipSettings, Shot, Timeline, build_timeline
from c2a.keys.state import KeyState, key_states
from c2a.prompts.style import ComposedStyle, StyleSelection, compose
from c2a.providers.base import Stack


def seed_for(*parts: object) -> int:
    """Stable 31-bit seed from any parts (same inputs → same seed on every machine)."""
    h = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return int.from_bytes(h[:4], "little") & 0x7FFFFFFF


def effective_config(run: Run) -> Config:
    m = run.read()
    return Config.model_validate(deep_merge(m["config"], m.get("settings") or {}))


Progress = Callable[[str, str, float], None]


@dataclass
class Context:
    run: Run
    cfg: Config
    stack: Stack
    log: list[str] = field(default_factory=list)
    progress: Progress | None = None
    cancelled: Callable[[], bool] = lambda: False

    # ------------------------------------------------------------- ingest products
    @property
    def normalized(self) -> Path:
        return self.run.path("ingest", "normalized.mp4")

    @property
    def source(self) -> Path:
        return Path(self.run.read()["source"])

    def meta(self) -> dict[str, Any]:
        p = self.run.path("ingest", "meta.json")
        if not p.exists():
            raise StageError("ingest has not run yet")
        d: dict[str, Any] = json.loads(p.read_text())
        return d

    @property
    def src_fps(self) -> Fraction:
        return Fraction(self.meta()["src_fps"])

    @property
    def src_frames(self) -> int:
        return int(self.meta()["src_frames"])

    @property
    def shots(self) -> tuple[Shot, ...]:
        return tuple(Shot(int(i), int(a), int(b)) for i, a, b in self.meta()["shots"])

    @property
    def gen_size(self) -> tuple[int, int]:
        m = self.meta()
        return int(m["gen_w"]), int(m["gen_h"])

    @property
    def out_size(self) -> tuple[int, int]:
        from c2a.core import media

        m = self.meta()
        return media.gen_size(int(m["src_w"]), int(m["src_h"]), self.cfg.output.resolution)

    def out_fps(self) -> Fraction:
        return self.src_fps if self.cfg.output.fps == "source" else Fraction(self.cfg.output.fps)

    # ------------------------------------------------------------- keys, timeline
    def keys(self) -> list[KeyState]:
        return key_states(self.run, self.cfg.keys.auto_check_min)

    def checked_keys(self) -> list[KeyState]:
        return [k for k in self.keys() if k.checked]

    def clip_settings(self) -> ClipSettings:
        c = self.cfg.clips
        return ClipSettings(
            mode=c.length_mode, fixed_s=Fraction(c.fixed_s).limit_denominator(1000),
            min_clip_s=Fraction(c.min_clip_s).limit_denominator(1000),
            max_clip_s=Fraction(c.max_clip_s).limit_denominator(1000), anchor_end=c.anchor_end,
            per_clip={int(k): Fraction(v).limit_denominator(1000) for k, v in c.per_clip.items()})

    def timeline(self) -> Timeline:
        keys = [(k.start, k.image) for k in self.checked_keys()]
        return build_timeline(self.src_frames, self.src_fps, self.shots, keys, self.out_fps(),
                              self.clip_settings(), gen_fps=self.cfg.ingest.gen_fps)

    def key(self, start: int) -> KeyState:
        for k in self.keys():
            if k.start == start:
                return k
        raise StageError(f"no key image at frame {start}")

    # ------------------------------------------------------------- style
    def style(self) -> ComposedStyle:
        return compose(StyleSelection.model_validate(self.cfg.style.selection.model_dump()))

    def style_lock(self) -> dict[str, Any]:
        p = self.run.path("style.lock.json")
        if not p.exists():
            raise StageError("style not approved yet")
        d: dict[str, Any] = json.loads(p.read_text())
        return d

    # ------------------------------------------------------------- reporting
    def say(self, msg: str) -> None:
        self.log.append(msg)
        print(f"[c2a {self.run.id}] {msg}", flush=True)

    def tick(self, stage: str, unit: str, frac: float) -> None:
        if self.progress:
            self.progress(stage, unit, frac)
        if self.cancelled():
            raise StageError("cancelled")
