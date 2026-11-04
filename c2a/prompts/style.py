"""Layered style presets and their deterministic composition (DESIGN_V2 §3).

Layers, in order: animation → director → finish → custom. Each preset contributes prompt
fragments for the image restyle and the video model, a negative prompt, and (director)
a camera bias or (finish) post-processing parameters. Composition de-duplicates phrases,
keeps layer order, and never drops custom text.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from c2a.prompts.vocab import CAMERA

Layer = Literal["animation", "director", "finish"]
LAYERS: tuple[Layer, ...] = ("animation", "director", "finish")
CLEAN_IMAGE = "clean image, no text, no logo, no watermark, no subtitles"
BASE_NEGATIVE = ("text, letters, subtitles, captions, watermark, logo, signature, UI, blurry, "
                 "deformed hands, extra fingers, low quality, flicker")


class PostParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    grain: float = Field(0.0, ge=0, le=1)
    warmth: float = Field(0.0, ge=-1, le=1)
    saturation: float = Field(1.0, ge=0, le=2)
    contrast: float = Field(1.0, ge=0.5, le=2)
    teal_orange: float = Field(0.0, ge=0, le=1)
    halation: float = Field(0.0, ge=0, le=1)
    bw: bool = False


class Preset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    layer: Layer
    title: str
    description: str
    image_prompt: str
    video_prompt: str = ""
    negative: str = ""
    realism: float | None = Field(None, ge=0, le=1)
    camera_bias: list[str] | None = None
    post: PostParams | None = None


@cache
def presets() -> dict[Layer, dict[str, Preset]]:
    root = resources.files("c2a").joinpath("styles")
    out: dict[Layer, dict[str, Preset]] = {}
    for layer in LAYERS:
        d: dict[str, Preset] = {}
        for f in sorted(root.joinpath(layer).iterdir(), key=lambda p: p.name):
            if not f.name.endswith(".yaml"):
                continue
            p = Preset.model_validate(yaml.safe_load(f.read_text()))
            if p.layer != layer or p.name != f.name.removesuffix(".yaml"):
                raise ValueError(f"preset {f.name}: name/layer do not match its file")
            for c in p.camera_bias or []:
                if c not in CAMERA:
                    raise ValueError(f"preset {p.name}: unknown camera {c!r}")
            d[p.name] = p
        out[layer] = d
    return out


def catalog() -> dict[str, list[dict[str, Any]]]:
    """For the Studio's preset pickers."""
    return {layer: [{"name": p.name, "title": p.title, "description": p.description}
                    for p in ps.values()] for layer, ps in presets().items()}


class StyleSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    animation: str | None = "theatrical_anime"
    director: str | None = "food_commercial"
    finish: str | None = "grain_35mm"
    custom_prompt: str = ""
    custom_negative: str = ""


@dataclass(frozen=True)
class ComposedStyle:
    image_prompt: str
    video_prompt: str
    negative: str
    camera_bias: tuple[str, ...] | None
    post: PostParams
    realism: float
    layers: dict[str, str | None] = field(default_factory=dict)
    custom_phrases: tuple[str, ...] = ()  # the person's own phrases (never dropped)

    @property
    def key(self) -> str:
        blob = json.dumps({"i": self.image_prompt, "v": self.video_prompt, "n": self.negative,
                           "c": self.camera_bias, "p": self.post.model_dump(),
                           "r": self.realism}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _phrases(text: str) -> list[str]:
    return [p.strip() for p in text.replace("\n", " ").split(",") if p.strip()]


def _join(parts: list[str]) -> str:
    seen: set[str] = set()
    out: list[str] = []
    for part in parts:
        for ph in _phrases(part):
            k = ph.lower()
            if k not in seen:
                seen.add(k)
                out.append(ph)
    return ", ".join(out)


def compose(sel: StyleSelection) -> ComposedStyle:
    ps = presets()
    chosen: list[Preset] = []
    for layer in LAYERS:
        name = getattr(sel, layer)
        if name is None:
            continue
        if name not in ps[layer]:
            raise ValueError(f"unknown {layer} preset {name!r}")
        chosen.append(ps[layer][name])
    image = _join([p.image_prompt for p in chosen] + [sel.custom_prompt])
    video = _join([p.video_prompt for p in chosen] + [sel.custom_prompt])
    negative = _join([BASE_NEGATIVE] + [p.negative for p in chosen] + [sel.custom_negative])
    cams = next((tuple(p.camera_bias) for p in chosen if p.camera_bias), None)
    post = next((p.post for p in reversed(chosen) if p.post), PostParams())
    realism = next((p.realism for p in chosen if p.realism is not None), 0.45)
    preset_keys = {ph.lower() for p in chosen for ph in _phrases(p.video_prompt)}
    custom = tuple(ph for ph in _phrases(_join([sel.custom_prompt]))
                   if ph.lower() not in preset_keys)
    return ComposedStyle(image, video, negative, cams, post, realism,
                         {layer: getattr(sel, layer) for layer in LAYERS}, custom)


def bias_camera(camera: str, bias: tuple[str, ...] | None) -> str:
    """Director presets restrict camera language: keep it if allowed, else the first one."""
    if not bias or camera in bias:
        return camera
    return bias[0]


def post_filter(p: PostParams) -> str | None:
    """ffmpeg filter chain (ffmpeg ≥ 4.4) for a finish's post parameters."""
    f: list[str] = []
    if p.contrast != 1.0 or p.saturation != 1.0:
        f.append(f"eq=contrast={p.contrast:.3f}:saturation={p.saturation:.3f}")
    if p.warmth:
        w = p.warmth * 0.25
        f.append(f"colorbalance=rm={w:.3f}:gm={w / 3:.3f}:bm={-w:.3f}")
    if p.teal_orange:
        t = p.teal_orange * 0.3
        f.append(f"colorbalance=rs={-t:.3f}:bs={t:.3f}:rh={t:.3f}:bh={-t:.3f}")
    if p.bw:
        f.append("hue=s=0")
    if p.grain:
        f.append(f"noise=alls={round(p.grain * 20)}:allf=t")
    chain = ",".join(f)
    if p.halation:
        glow = f"split[c2a_a][c2a_b];[c2a_b]gblur=sigma=12[c2a_g];[c2a_a][c2a_g]blend=" \
               f"all_mode=screen:all_opacity={p.halation:.3f}"
        chain = f"{chain},{glow}" if chain else glow
    return chain or None
