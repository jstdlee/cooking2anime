"""Typed configuration. `configs/default.yaml` must equal `Config()` (checked by a test)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from c2a.core.errors import ConfigError


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RunCfg(_Model):
    workdir: str = "runs"
    commercial: bool = False
    stack: Literal["local", "cloud", "fake"] = "local"


class OutputCfg(_Model):
    resolution: Literal["720p", "1080p"] = "720p"
    fps: int | Literal["source"] = 24
    codec: Literal["h264"] = "h264"
    crf: int = Field(18, ge=0, le=51)


class AnalyzeCfg(_Model):
    shot_detector: Literal["transnetv2", "ffmpeg_scene"] = "transnetv2"
    cut_threshold: float = Field(0.5, gt=0, lt=1)
    gen_fps: int = Field(16, gt=0)
    chunk_frames: list[int] = [81, 65, 49, 33, 17]


class DescribeCfg(_Model):
    vlm: Literal["qwen_vl", "cloud_vlm"] = "qwen_vl"
    sample_fps: int = Field(4, gt=0)
    max_beats: int = Field(4, ge=1, le=8)
    ignore_overlays: bool = True


class PreviewCfg(_Model):
    sample_keyframes: int = Field(4, ge=1, le=12)
    seeds_per_keyframe: int = Field(3, ge=1, le=8)


class StyleCfg(_Model):
    preset: str = "shinkai_food"
    prompt: str = (
        "Japanese anime cooking scene, cinematic Shinkai-style lighting, painterly "
        "background, highly detailed glossy food, shallow depth of field, soft film "
        "grain, professional photography composition\n"
    )
    clean_prompt: str = "clean image, no text, no logo, no watermark, no subtitles"
    negative: str = (
        "text, letters, subtitles, captions, watermark, logo, signature, UI, "
        "blurry, deformed hands, extra fingers, low quality, flicker\n"
    )
    realism: float = Field(0.45, ge=0, le=1)
    preview: PreviewCfg = PreviewCfg()


class GenerateCfg(_Model):
    model: str = "wan_flf2v_14b"
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
    lut: str | None = None
    film_grain: float = Field(0.15, ge=0, le=1)


class SfxCfg(_Model):
    enabled: bool = True
    provider: str = "hunyuan_foley"
    window_s: float = Field(8, gt=0)
    crossfade_s: float = Field(0.5, ge=0)


class VoiceCfg(_Model):
    enabled: bool = True
    provider: Literal["fish_api", "fish_local"] = "fish_api"
    model: str = "drama-3"
    reference_id: str | None = None
    min_intensity: int = Field(2, ge=0, le=3)
    min_gap_s: float = Field(4, ge=0)
    gain_db: float = -6
    word_guard: bool = True


class AudioCfg(_Model):
    sfx: SfxCfg = SfxCfg()
    voice: VoiceCfg = VoiceCfg()
    loudness_lufs: float = -14
    true_peak_dbtp: float = -1


class CanvasCfg(_Model):
    app: Literal["godraw"] = "godraw"
    godraw_commit: str = "03717388cddebd320b4d4949011340e74c9091cf"
    board: str = "board.tldr"
    write_interval_s: float = Field(2, gt=0)
    thumb_max_px: int = Field(512, ge=64)
    board_budget_mb: float = Field(25, gt=0)
    server_port: int = Field(8765, ge=1024, le=65535)


class ComfyCfg(_Model):
    url: str = "http://127.0.0.1:8188"


class FalCfg(_Model):
    api_key_env: str = "FAL_KEY"
    max_parallel: int = Field(6, ge=1)


class FishCfg(_Model):
    api_key_env: str = "FISH_API_KEY"
    base_url: str = "https://api.fish.audio"
    local_url: str = "http://127.0.0.1:8080"


class CloudVlmCfg(_Model):
    api_key_env: str = "GEMINI_API_KEY"


class ProvidersCfg(_Model):
    comfyui: ComfyCfg = ComfyCfg()
    fal: FalCfg = FalCfg()
    fish: FishCfg = FishCfg()
    cloud_vlm: CloudVlmCfg = CloudVlmCfg()


class Config(_Model):
    run: RunCfg = RunCfg()
    output: OutputCfg = OutputCfg()
    analyze: AnalyzeCfg = AnalyzeCfg()
    describe: DescribeCfg = DescribeCfg()
    style: StyleCfg = StyleCfg()
    generate: GenerateCfg = GenerateCfg()
    qa: QaCfg = QaCfg()
    post: PostCfg = PostCfg()
    audio: AudioCfg = AudioCfg()
    canvas: CanvasCfg = CanvasCfg()
    providers: ProvidersCfg = ProvidersCfg()


def load_config(path: Path | None = None, overrides: dict[str, object] | None = None) -> Config:
    data: dict[str, object] = {}
    if path is not None:
        try:
            loaded = yaml.safe_load(path.read_text()) or {}
        except (OSError, yaml.YAMLError) as e:
            raise ConfigError(f"cannot read config {path}: {e}") from e
        if not isinstance(loaded, dict):
            raise ConfigError(f"{path}: top level must be a mapping")
        data = loaded
    if overrides:
        data = _deep_merge(data, overrides)
    try:
        return Config.model_validate(data)
    except Exception as e:  # pydantic.ValidationError
        raise ConfigError(str(e)) from e


def _deep_merge(a: dict[str, object], b: dict[str, object]) -> dict[str, object]:
    out = dict(a)
    for k, v in b.items():
        cur = out.get(k)
        if isinstance(cur, dict) and isinstance(v, dict):
            out[k] = _deep_merge(cur, v)
        else:
            out[k] = v
    return out


def parse_set(items: list[str]) -> dict[str, object]:
    """`--set output.resolution=1080p` → nested dict (values parsed as YAML scalars)."""
    out: dict[str, object] = {}
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
