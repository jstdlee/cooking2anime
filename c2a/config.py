"""Typed configuration (v2). `configs/default.yaml` must equal `Config()` (checked by a test).

Per-run choices made in the Studio or CLI (style layers, clip lengths, audio mode) are
stored in the run manifest under "settings" and deep-merged over this config.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from c2a.core.errors import ConfigError


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RunCfg(_Model):
    workdir: str = "runs"
    commercial: bool = False
    stack: Literal["local", "cloud", "fake"] = "cloud"


class OutputCfg(_Model):
    resolution: Literal["720p", "1080p"] = "720p"
    fps: int | Literal["source"] = 24
    codec: Literal["h264"] = "h264"
    crf: int = Field(18, ge=0, le=51)


class IngestCfg(_Model):
    shot_detector: Literal["transnetv2", "ffmpeg_scene"] = "ffmpeg_scene"
    gen_fps: int = Field(16, gt=0)


class KeysCfg(_Model):
    sensitivity: float = Field(0.7, gt=0, le=2)
    min_gap_s: float = Field(0.75, gt=0)
    max_gap_s: float = Field(5.0, gt=0)
    refine_s: float = Field(0.25, ge=0)
    auto_check_min: float = Field(0.6, ge=0, le=1)
    analysis_width: int = Field(128, ge=32, le=512)


class TagCfg(_Model):
    vlm: Literal["qwen_vl", "cloud_vlm"] = "cloud_vlm"
    sample_fps: int = Field(4, gt=0)


class StyleSelectionCfg(_Model):
    animation: str | None = "theatrical_anime"
    director: str | None = "food_commercial"
    finish: str | None = "grain_35mm"
    custom_prompt: str = ""
    custom_negative: str = ""


class PreviewCfg(_Model):
    sample_keys: int = Field(3, ge=1, le=8)
    seeds_per_key: int = Field(3, ge=1, le=6)  # the chosen combination + variations


class StyleCfg(_Model):
    selection: StyleSelectionCfg = StyleSelectionCfg()
    preview: PreviewCfg = PreviewCfg()


class ClipsCfg(_Model):
    length_mode: Literal["match", "fixed", "per_clip"] = "match"
    fixed_s: float = Field(3.0, gt=0)
    min_clip_s: float = Field(1.0, gt=0)
    max_clip_s: float = Field(5.0, gt=0)
    anchor_end: bool = True
    per_clip: dict[int, float] = {}  # key start frame → seconds (length_mode per_clip)


class GenerateCfg(_Model):
    model: str = "wan_i2v_14b"
    max_retries: int = Field(2, ge=0, le=5)


class QaCfg(_Model):
    min_style_sim: float = 0.80
    min_structure_ssim: float = 0.35
    min_boundary_ssim: float = 0.90
    min_motion_match: float = 0.70
    max_flicker: float = 0.05
    max_text_boxes: int = 0


class PostCfg(_Model):
    interpolate: Literal["rife", "nearest"] = "rife"
    upscaler: Literal["seedvr2", "lanczos"] = "seedvr2"


class SfxCfg(_Model):
    enabled: bool = True
    provider: str = "hunyuan_foley"


class VoiceCfg(_Model):
    enabled: bool = True
    provider: Literal["fish_api", "fish_local"] = "fish_api"
    model: str = "drama-3"
    reference_id: str | None = None
    min_intensity: int = Field(2, ge=0, le=3)
    min_gap_s: float = Field(4, ge=0)
    gain_db: float = -6
    word_guard: bool = True


class KeepVoiceCfg(_Model):
    stretch_tolerance: float = Field(0.15, ge=0, le=0.5)
    fade_ms: int = Field(80, ge=0, le=500)
    crossfade_ms: int = Field(40, ge=0, le=500)
    phrase_gap_s: float = Field(0.3, gt=0)
    duck_db: float = Field(8, ge=0, le=30)
    foley_under: bool = True


class AudioCfg(_Model):
    mode: Literal["regenerate", "keep_voice", "silent"] = "regenerate"
    sfx: SfxCfg = SfxCfg()
    voice: VoiceCfg = VoiceCfg()
    keep_voice: KeepVoiceCfg = KeepVoiceCfg()
    loudness_lufs: float = -14
    true_peak_dbtp: float = -1


class StudioCfg(_Model):
    host: str = "127.0.0.1"
    port: int = Field(8765, ge=1024, le=65535)
    open_browser: bool = True


class ComfyCfg(_Model):
    url: str = "http://127.0.0.1:8188"


class FalCfg(_Model):
    api_key_env: str = "FAL_KEY"
    max_parallel: int = Field(6, ge=1)
    timeout_s: int = Field(900, ge=30)
    # fal.ai model ids (https://fal.ai/models); any model with the same inputs works
    image_edit: str = "fal-ai/nano-banana/edit"
    video: str = "fal-ai/wan/v2.2-a14b/image-to-video"
    video_resolution: Literal["480p", "580p", "720p"] = "480p"
    foley: str = "fal-ai/mmaudio-v2"
    asr: str = "fal-ai/wizper"
    separator: str = "fal-ai/demucs"


class FishCfg(_Model):
    api_key_env: str = "FISH_API_KEY"
    base_url: str = "https://api.fish.audio"
    local_url: str = "http://127.0.0.1:8080"


class CloudVlmCfg(_Model):
    """Any OpenAI-compatible chat API with image input (Gemini, OpenAI, OpenRouter, Qwen,
    a local Ollama or vLLM server...)."""

    api_key_env: str = "GEMINI_API_KEY"
    base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    model: str = "gemini-2.5-flash"
    timeout_s: int = Field(120, ge=10)
    max_frames: int = Field(6, ge=1, le=16)
    ocr: bool = True  # also check images/clips for on-screen text with this model


class ProvidersCfg(_Model):
    comfyui: ComfyCfg = ComfyCfg()
    fal: FalCfg = FalCfg()
    fish: FishCfg = FishCfg()
    cloud_vlm: CloudVlmCfg = CloudVlmCfg()


class Config(_Model):
    run: RunCfg = RunCfg()
    output: OutputCfg = OutputCfg()
    ingest: IngestCfg = IngestCfg()
    keys: KeysCfg = KeysCfg()
    tag: TagCfg = TagCfg()
    style: StyleCfg = StyleCfg()
    clips: ClipsCfg = ClipsCfg()
    generate: GenerateCfg = GenerateCfg()
    qa: QaCfg = QaCfg()
    post: PostCfg = PostCfg()
    audio: AudioCfg = AudioCfg()
    studio: StudioCfg = StudioCfg()
    providers: ProvidersCfg = ProvidersCfg()


# Settings a person may change per run (Studio / CLI). Anything else is config-only.
SETTABLE: dict[str, tuple[str, ...]] = {
    "style": ("selection",),
    "clips": ("length_mode", "fixed_s", "anchor_end", "per_clip"),
    "audio": ("mode",),
    "output": ("resolution",),
}


def check_settings(settings: dict[str, Any]) -> None:
    for section, value in settings.items():
        allowed = SETTABLE.get(section)
        if allowed is None or not isinstance(value, dict):
            raise ConfigError(f"setting {section!r} cannot be changed per run")
        bad = set(value) - set(allowed)
        if bad:
            raise ConfigError(f"settings not changeable per run: {section}.{sorted(bad)[0]}")


def load_config(path: Path | None = None, overrides: dict[str, Any] | None = None) -> Config:
    data: dict[str, Any] = {}
    if path is not None:
        try:
            loaded = yaml.safe_load(path.read_text()) or {}
        except (OSError, yaml.YAMLError) as e:
            raise ConfigError(f"cannot read config {path}: {e}") from e
        if not isinstance(loaded, dict):
            raise ConfigError(f"{path}: top level must be a mapping")
        data = loaded
    if overrides:
        data = deep_merge(data, overrides)
    try:
        return Config.model_validate(data)
    except Exception as e:  # pydantic.ValidationError
        raise ConfigError(str(e)) from e


def deep_merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        cur = out.get(k)
        if isinstance(cur, dict) and isinstance(v, dict) and k != "per_clip":
            out[k] = deep_merge(cur, v)
        else:
            out[k] = v
    return out


def parse_set(items: list[str]) -> dict[str, Any]:
    """`--set output.resolution=1080p` → nested dict (values parsed as YAML scalars)."""
    out: dict[str, Any] = {}
    for item in items:
        if "=" not in item:
            raise ConfigError(f"--set expects key=value, got {item!r}")
        key, raw = item.split("=", 1)
        node = out
        parts = key.split(".")
        for p in parts[:-1]:
            nxt = node.setdefault(p, {})
            if not isinstance(nxt, dict):
                raise ConfigError(f"conflicting --set for {key}")
            node = nxt
        node[parts[-1]] = yaml.safe_load(raw)
    return out
