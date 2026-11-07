"""Video generation on your own GPU with ComfyUI (e.g. a DGX): Wan 2.2 image-to-video.

The workflow is ComfyUI's native Wan 2.2 14B image-to-video graph (two experts: high noise,
then low noise), built here in API format so frames, fps, size, seed and prompts are set per
clip; with a last frame it uses WanFirstLastFrameToVideo. Model file names come from the
config (`providers.comfyui`), so other Wan 2.2 builds (fp16, fp8, GGUF loaders aside) work.
Setup on the GPU machine: scripts/comfyui/setup_wan22.sh.

API used: POST /upload/image, POST /prompt, GET /history/{id}, GET /view, and for checks
GET /system_stats and GET /models/{folder}.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from c2a.config import Config
from c2a.core.errors import ProviderError
from c2a.providers.base import ProviderInfo
from c2a.providers.cloud import Http, conform_video


def gen_size(first: Path, resolution: str) -> tuple[int, int]:
    """Generation size: the key image's aspect, short side 480/720, multiples of 16."""
    from PIL import Image

    w, h = Image.open(first).size
    short = {"480p": 480, "580p": 576, "720p": 720}.get(resolution, 480)
    s = short / min(w, h)
    return max(16, round(w * s / 16) * 16), max(16, round(h * s / 16) * 16)


def wan22_workflow(
    cfg: Config,
    first: str,
    last: str | None,
    width: int,
    height: int,
    frames: int,
    fps: int,
    prompt: str,
    negative: str,
    seed: int,
) -> dict[str, Any]:
    """ComfyUI API-format graph for Wan 2.2 14B i2v (first, or first + last frame)."""
    c = cfg.providers.comfyui
    half = max(1, c.steps // 2)
    g: dict[str, Any] = {
        "1": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": c.wan_high_noise, "weight_dtype": "default"},
        },
        "2": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": c.wan_low_noise, "weight_dtype": "default"},
        },
        "3": {
            "class_type": "CLIPLoader",
            "inputs": {"clip_name": c.text_encoder, "type": "wan", "device": "default"},
        },
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": c.vae}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["3", 0]}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["3", 0]}},
        "7": {"class_type": "LoadImage", "inputs": {"image": first}},
        "10": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1", 0], "shift": c.shift}},
        "11": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["2", 0], "shift": c.shift}},
        "12": {
            "class_type": "KSamplerAdvanced",
            "inputs": {
                "model": ["10", 0],
                "add_noise": "enable",
                "noise_seed": seed,
                "steps": c.steps,
                "cfg": c.cfg,
                "sampler_name": c.sampler,
                "scheduler": c.scheduler,
                "positive": ["9", 0],
                "negative": ["9", 1],
                "latent_image": ["9", 2],
                "start_at_step": 0,
                "end_at_step": half,
                "return_with_leftover_noise": "enable",
            },
        },
        "13": {
            "class_type": "KSamplerAdvanced",
            "inputs": {
                "model": ["11", 0],
                "add_noise": "disable",
                "noise_seed": seed,
                "steps": c.steps,
                "cfg": c.cfg,
                "sampler_name": c.sampler,
                "scheduler": c.scheduler,
                "positive": ["9", 0],
                "negative": ["9", 1],
                "latent_image": ["12", 0],
                "start_at_step": half,
                "end_at_step": 10000,
                "return_with_leftover_noise": "disable",
            },
        },
        "14": {"class_type": "VAEDecode", "inputs": {"samples": ["13", 0], "vae": ["4", 0]}},
        "15": {"class_type": "CreateVideo", "inputs": {"images": ["14", 0], "fps": fps}},
        "16": {
            "class_type": "SaveVideo",
            "inputs": {
                "video": ["15", 0],
                "filename_prefix": "c2a/clip",
                "format": "mp4",
                "codec": "h264",
            },
        },
    }
    common = {
        "positive": ["5", 0],
        "negative": ["6", 0],
        "vae": ["4", 0],
        "width": width,
        "height": height,
        "length": frames,
        "batch_size": 1,
        "start_image": ["7", 0],
    }
    if last is None:
        g["9"] = {"class_type": "WanImageToVideo", "inputs": common}
    else:
        g["8"] = {"class_type": "LoadImage", "inputs": {"image": last}}
        g["9"] = {
            "class_type": "WanFirstLastFrameToVideo",
            "inputs": {**common, "end_image": ["8", 0]},
        }
    return g


class ComfyVideo:
    """VideoGen on a ComfyUI server."""

    def __init__(
        self, cfg: Config, http: Http | None = None, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        self.cfg = cfg
        c = cfg.providers.comfyui
        self.url, self.timeout = c.url.rstrip("/"), c.timeout_s
        self.http, self.sleep = http or Http(), sleep
        self.client_id = uuid.uuid4().hex
        self.info = ProviderInfo(
            name=f"comfyui:{c.wan_high_noise}",
            version="wan2.2",
            license="Apache-2.0 (Wan 2.2)",
            commercial_ok=True,
            deterministic=False,
        )

    def _upload(self, image: Path) -> str:
        boundary = f"c2a{uuid.uuid4().hex}"
        name = f"c2a_{uuid.uuid4().hex[:12]}{image.suffix or '.png'}"
        body = (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="image"; '
                f'filename="{name}"\r\n'
                "Content-Type: image/png\r\n\r\n"
            ).encode()
            + image.read_bytes()
            + f"\r\n--{boundary}--\r\n".encode()
        )
        r = self.http.request(
            "POST",
            f"{self.url}/upload/image",
            data=body,
            content_type=f"multipart/form-data; boundary={boundary}",
            timeout=120,
        ).json()
        sub = r.get("subfolder") or ""
        return f"{sub}/{r['name']}" if sub else str(r["name"])

    def _run(self, graph: dict[str, Any]) -> dict[str, Any]:
        r = self.http.request(
            "POST",
            f"{self.url}/prompt",
            body={"prompt": graph, "client_id": self.client_id},
            timeout=60,
        ).json()
        if r.get("node_errors"):
            raise ProviderError(f"ComfyUI rejected the workflow: {str(r['node_errors'])[:400]}")
        pid = r.get("prompt_id")
        if not pid:
            raise ProviderError(f"ComfyUI returned no prompt id: {str(r)[:200]}")
        deadline = time.monotonic() + self.timeout
        delay = 1.0
        while True:
            h = self.http.request("GET", f"{self.url}/history/{pid}", timeout=60).json()
            item = h.get(pid)
            if item:
                st = item.get("status") or {}
                if st.get("status_str") == "error":
                    msgs = [m for m in st.get("messages", []) if m and m[0] == "execution_error"]
                    detail = msgs[-1][1].get("exception_message", "") if msgs else st
                    raise ProviderError(f"ComfyUI failed: {str(detail)[:400]}")
                if st.get("completed", True) and item.get("outputs"):
                    return dict(item["outputs"])
            if time.monotonic() > deadline:
                raise ProviderError(f"ComfyUI: no result after {self.timeout} s (prompt {pid})")
            self.sleep(delay)
            delay = min(delay * 1.5, 5.0)

    def _video_file(self, outputs: dict[str, Any]) -> dict[str, Any]:
        for out in outputs.values():
            for key in ("videos", "gifs", "images"):
                for f in out.get(key, []) or []:
                    if (
                        str(f.get("filename", ""))
                        .lower()
                        .endswith((".mp4", ".webm", ".mov", ".webp"))
                    ):
                        return dict(f)
        raise ProviderError(f"ComfyUI produced no video: {str(outputs)[:300]}")

    def _gen(
        self,
        first: Path,
        last: Path | None,
        frames: int,
        fps: int,
        prompt: str,
        negative: str,
        seed: int,
        dst: Path,
    ) -> Path:
        w, h = gen_size(first, self.cfg.providers.comfyui.resolution)
        graph = wan22_workflow(
            self.cfg,
            self._upload(first),
            self._upload(last) if last else None,
            w,
            h,
            frames,
            fps,
            prompt,
            negative,
            seed % 2**32,
        )
        f = self._video_file(self._run(graph))
        q = urlencode(
            {
                "filename": f["filename"],
                "subfolder": f.get("subfolder", ""),
                "type": f.get("type", "output"),
            }
        )
        tmp = dst.with_suffix(".download.mp4")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(self.http.request("GET", f"{self.url}/view?{q}", timeout=300).body)
        try:
            return conform_video(tmp, first, frames, fps, dst)
        finally:
            tmp.unlink(missing_ok=True)

    def i2v(
        self, first: Path, frames: int, fps: int, prompt: str, negative: str, seed: int, dst: Path
    ) -> Path:
        return self._gen(first, None, frames, fps, prompt, negative, seed, dst)

    def flf2v(
        self,
        first: Path,
        last: Path,
        frames: int,
        fps: int,
        prompt: str,
        negative: str,
        seed: int,
        dst: Path,
    ) -> Path:
        return self._gen(first, last, frames, fps, prompt, negative, seed, dst)

    def check(self) -> list[tuple[str, bool, str]]:
        """(name, ok, detail) for the doctor: server reachable, Wan nodes and model files."""
        c = self.cfg.providers.comfyui
        out: list[tuple[str, bool, str]] = []
        try:
            st = Http(retries=0).request("GET", f"{self.url}/system_stats", timeout=10).json()
            dev = (st.get("devices") or [{}])[0]
            out.append(
                (
                    "ComfyUI server",
                    True,
                    f"{self.url} · {dev.get('name', 'device')} · "
                    f"{round((dev.get('vram_total') or 0) / 2**30)} GB",
                )
            )
        except ProviderError as e:
            return [("ComfyUI server", False, f"{self.url} not reachable: {e}")]
        for folder, names in (
            ("diffusion_models", [c.wan_high_noise, c.wan_low_noise]),
            ("text_encoders", [c.text_encoder]),
            ("vae", [c.vae]),
        ):
            try:
                have = set(
                    Http(retries=0).request("GET", f"{self.url}/models/{folder}", timeout=10).json()
                )
            except ProviderError:
                continue  # older ComfyUI without /models: the run reports missing files
            miss = [n for n in names if n not in have]
            out.append(
                (
                    f"ComfyUI {folder}",
                    not miss,
                    "missing " + ", ".join(miss) if miss else ", ".join(names),
                )
            )
        return out
