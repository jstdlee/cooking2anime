"""Everything a stage needs, loaded once per pipeline invocation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from fractions import Fraction
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from c2a.config import Config
from c2a.core.errors import StageError
from c2a.core.manifest import Run
from c2a.core.timeline import Timeline, timeline_from_json
from c2a.prompts.builder import Preset
from c2a.providers.base import Stack


def load_preset(name: str) -> Preset:
    try:
        text = resources.files("c2a").joinpath(f"styles/{name}.yaml").read_text()
    except FileNotFoundError as e:
        raise StageError(f"unknown style preset {name!r}") from e
    d = yaml.safe_load(text)
    return Preset(name=d["name"], quality_suffix=d["quality_suffix"], negative=d["negative"])


def seed_for(*parts: object) -> int:
    """Stable 31-bit seed from any parts (same inputs → same seed on every machine)."""
    h = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return int.from_bytes(h[:4], "little") & 0x7FFFFFFF


@dataclass
class Context:
    run: Run
    cfg: Config
    stack: Stack
    preset: Preset
    log: list[str] = field(default_factory=list)

    _timeline: Timeline | None = None

    @property
    def timeline(self) -> Timeline:
        if self._timeline is None:
            p = self.run.path("analyze", "timeline.json")
            if not p.exists():
                raise StageError("timeline not available yet (analyze has not run)")
            self._timeline = timeline_from_json(json.loads(p.read_text()))
        return self._timeline

    def reset_timeline(self) -> None:
        self._timeline = None

    @property
    def normalized(self) -> Path:
        return self.run.path("analyze", "normalized.mp4")

    @property
    def source(self) -> Path:
        return Path(self.run.read()["source"])

    def meta(self) -> dict[str, Any]:
        p = self.run.path("analyze", "meta.json")
        d: dict[str, Any] = json.loads(p.read_text())
        return d

    @property
    def gen_size(self) -> tuple[int, int]:
        m = self.meta()
        return int(m["gen_w"]), int(m["gen_h"])

    @property
    def out_size(self) -> tuple[int, int]:
        m = self.meta()
        return int(m["out_w"]), int(m["out_h"])

    def style_lock(self) -> dict[str, Any]:
        p = self.run.path("style.lock.json")
        if not p.exists():
            raise StageError("style not approved yet")
        d: dict[str, Any] = json.loads(p.read_text())
        return d

    def style_prompt(self) -> str:
        override = self.run.read()["overrides"].get("style_prompt")
        return str(override) if override else self.cfg.style.prompt

    def out_fps(self, src_fps: Fraction) -> Fraction:
        return src_fps if self.cfg.output.fps == "source" else Fraction(self.cfg.output.fps)

    def say(self, msg: str) -> None:
        self.log.append(msg)
        print(f"[c2a {self.run.id}] {msg}", flush=True)
