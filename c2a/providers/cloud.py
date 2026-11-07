"""The cloud stack: real models over HTTP (design §4).

- Vision LLM (tagging, motion judging, on-screen text): any OpenAI-compatible chat API with
  image input — Gemini (default), OpenAI, OpenRouter, Qwen/DashScope, or a local Ollama /
  vLLM server.
- fal.ai: image restyle, image-to-video (first frame, or first + last), foley, speech-to-text
  (word guard) and voice separation (keep-voice).
- Fish Audio: the non-verbal voice sounds.

Every provider returns exactly what the pipeline expects (image size, frame count and fps,
48 kHz stereo WAV), whatever the model produced. Keys are read from the environment
variables the config names; requests go through `Http`, which tests replace with recorded
responses (tests/contract).
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from c2a.config import Config
from c2a.core import media
from c2a.core.errors import ProviderError
from c2a.providers.base import ProviderInfo, Stack, TimedCue
from c2a.providers.fake import EnergyWordTimer

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except ValueError as e:
            raise ProviderError(f"not JSON ({self.status}): {self.body[:200]!r}") from e


Sender = Callable[[str, str, dict[str, str], bytes | None, float], Response]


def urllib_send(method: str, url: str, headers: dict[str, str], body: bytes | None,
                timeout: float) -> Response:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return Response(r.status, r.read())
    except urllib.error.HTTPError as e:
        return Response(e.code, e.read() or b"")


class Http:
    """JSON over HTTP with retries (429/5xx/timeouts, exponential backoff)."""

    def __init__(self, send: Sender = urllib_send, retries: int = 3, backoff_s: float = 2.0,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.send, self.retries, self.backoff_s, self.sleep = send, retries, backoff_s, sleep

    def request(self, method: str, url: str, headers: dict[str, str] | None = None,
                body: Any = None, timeout: float = 120, data: bytes | None = None,
                content_type: str | None = None) -> Response:
        """`body` is sent as JSON; `data` (with `content_type`) as raw bytes."""
        hdrs = {"User-Agent": "c2a", **(headers or {})}
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        elif data is not None and content_type:
            hdrs["Content-Type"] = content_type
        last = ""
        for attempt in range(self.retries + 1):
            try:
                r = self.send(method, url, hdrs, data, timeout)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = f"{type(e).__name__}: {e}"
            else:
                if r.status < 400:
                    return r
                if r.status not in RETRY_STATUS:
                    raise ProviderError(f"{method} {_short(url)} → {r.status}: {_detail(r)}")
                last = f"{r.status}: {_detail(r)}"
            if attempt < self.retries:
                self.sleep(self.backoff_s * 2 ** attempt)
        raise ProviderError(f"{method} {_short(url)} failed after {self.retries + 1} tries: {last}")


def _short(url: str) -> str:
    return url.split("?")[0][:120]


def _detail(r: Response) -> str:
    """The provider's error message (OpenAI, fal, Gemini — which wraps it in a list — …)."""
    try:
        v: Any = json.loads(r.body)
    except ValueError:
        return r.body[:300].decode("utf-8", "replace")
    if isinstance(v, list) and v:
        v = v[0]
    if isinstance(v, dict):
        d = v.get("detail") or v.get("error") or v.get("message") or v
        if isinstance(d, dict):
            d = d.get("message") or d
        v = d
    return str(v)[:300]


def _key(env: str, what: str) -> str:
    v = os.environ.get(env, "").strip()
    if not v:
        raise ProviderError(f"{what} needs an API key: set {env} "
                            "(c2a Review: Settings → AI providers)")
    return v


def data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def _info(name: str, model: str, license: str, commercial_ok: bool = True) -> ProviderInfo:
    return ProviderInfo(name=f"{name}:{model}", version=model, license=license,
                        commercial_ok=commercial_ok, deterministic=False)


# ---------------------------------------------------------------- conform outputs


def conform_image(src: Path, like: Path, dst: Path) -> Path:
    """Resize/crop `src` to the size of `like` (cover), as PNG."""
    from PIL import Image

    w, h = Image.open(like).size
    im = Image.open(src).convert("RGB")
    s = max(w / im.width, h / im.height)
    im = im.resize((max(w, round(im.width * s)), max(h, round(im.height * s))),
                   Image.Resampling.LANCZOS)
    x, y = (im.width - w) // 2, (im.height - h) // 2
    dst.parent.mkdir(parents=True, exist_ok=True)
    im.crop((x, y, x + w, y + h)).save(dst)
    return dst


def conform_video(src: Path, like: Path, frames: int, fps: int, dst: Path) -> Path:
    """Exactly `frames` frames at `fps`, at the size of the image `like` (cover + crop;
    the last frame is held if the model returned fewer)."""
    from PIL import Image

    w, h = Image.open(like).size
    dst.parent.mkdir(parents=True, exist_ok=True)
    media.run([media._bin("ffmpeg"), "-y", "-v", "error", "-i", str(src), "-an", "-vf",
               f"fps={fps},scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,"
               f"crop={w}:{h},tpad=stop_mode=clone:stop={frames},setsar=1",
               "-frames:v", str(frames), "-c:v", "libx264", "-preset", "veryfast", "-crf", "10",
               "-pix_fmt", "yuv420p", str(dst)])
    return dst


def conform_audio(src: Path, duration: float | None, dst: Path) -> Path:
    """48 kHz stereo 16-bit WAV, padded/trimmed to `duration` when given."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    af = ["-af", f"apad=whole_dur={duration:.6f}", "-t", f"{duration:.6f}"] if duration else []
    media.run([media._bin("ffmpeg"), "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "2",
               "-ar", "48000", *af, "-c:a", "pcm_s16le", str(dst)])
    return dst


def sample_frames(clip: Path, n: int, width: int = 512) -> list[Path]:
    """Up to `n` evenly spaced JPEG frames of a clip (for the vision model)."""
    tmp = Path(tempfile.mkdtemp(prefix="c2a-frames-"))
    info = media.probe(clip)
    dur = max(float(info.duration), 0.1)
    out: list[Path] = []
    for i in range(n):
        t = dur * (i + 0.5) / n
        p = tmp / f"f{i}.jpg"
        media.run([media._bin("ffmpeg"), "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(clip),
                   "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", str(p)])
        if p.exists():
            out.append(p)
    return out


# ---------------------------------------------------------------- vision LLM


class OpenAIVision:
    """Tags, motion judging and text detection with an OpenAI-compatible vision model."""

    def __init__(self, cfg: Config, http: Http | None = None) -> None:
        c = cfg.providers.cloud_vlm
        self.base = c.base_url.rstrip("/")
        self.model, self.key_env, self.timeout = c.model, c.api_key_env, c.timeout_s
        self.frames = c.max_frames
        self.http = http or Http()
        self.info = _info("vlm", self.model, "API terms of the provider")

    def chat_json(self, prompt: str, images: list[Path], seed: int) -> Any:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        content += [{"type": "image_url", "image_url": {"url": data_uri(p)}} for p in images]
        body = {"model": self.model, "messages": [{"role": "user", "content": content}],
                "response_format": {"type": "json_object"}, "temperature": 0.2, "seed": seed}
        r = self.http.request("POST", f"{self.base}/chat/completions",
                              {"Authorization": f"Bearer {_key(self.key_env, 'the vision model')}"},
                              body, timeout=self.timeout).json()
        try:
            text = r["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise ProviderError(f"vision model reply has no message: {str(r)[:200]}") from e
        return parse_json_text(text)

    def tag(self, image: Path, clip: Path, duration: float, instructions: str,
            schema: dict[str, Any], seed: int) -> Any:
        frames = sample_frames(clip, self.frames)
        try:
            prompt = (f"{instructions}\n\nThe first image is the key image; the next "
                      f"{len(frames)} are frames from the {duration:.1f} s that follow it, "
                      "in order. Reply with one JSON object only, matching this JSON schema:\n"
                      f"{json.dumps(schema)}")
            try:
                return self.chat_json(prompt, [image, *frames], seed)
            except ValueError as e:  # not JSON: the stage retries and reports it
                return {"error": str(e)}
        finally:
            _cleanup(frames)

    def judge_motion(self, clip: Path, beats: list[str], seed: int) -> float:
        frames = sample_frames(clip, self.frames)
        try:
            prompt = ("These frames come from a short generated video, in order. The intended "
                      f"motion is: {' '.join(beats)}\nHow well does the video show that motion "
                      "(0 = not at all, 1 = exactly)? Reply as JSON: {\"score\": number}")
            try:
                v = self.chat_json(prompt, frames, seed)
                return float(max(0.0, min(1.0, float(v.get("score", 0)))))
            except (ValueError, TypeError, AttributeError):
                return 0.0
        finally:
            _cleanup(frames)

    def count_text_boxes(self, image: Path, min_conf: float) -> int:
        prompt = ("Count the separate pieces of written text, logos, watermarks, captions or "
                  "subtitles visible in this image (0 if none). Ignore text that is only "
                  "texture with no readable letters. Reply as JSON: {\"text_boxes\": integer}")
        try:
            v = self.chat_json(prompt, [image], 0)
            return max(0, int(v.get("text_boxes", 0)))
        except (ValueError, TypeError, AttributeError):
            return 0


class NoOCR:
    info = ProviderInfo("ocr-off", "1", "n/a", True, True)

    def count_text_boxes(self, image: Path, min_conf: float) -> int:
        return 0


def parse_json_text(text: str) -> Any:
    """JSON from a model reply (tolerates ```json fences and text around the object)."""
    s = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if m:
        s = m.group(1).strip()
    try:
        return json.loads(s)
    except ValueError:
        a, b = s.find("{"), s.rfind("}")
        if a >= 0 and b > a:
            return json.loads(s[a:b + 1])
        raise


def _cleanup(paths: list[Path]) -> None:
    if paths:
        shutil.rmtree(paths[0].parent, ignore_errors=True)


# ---------------------------------------------------------------- fal.ai


class Fal:
    """fal.ai queue API: submit, poll, fetch the result; files go in as data URIs."""

    QUEUE = "https://queue.fal.run"

    def __init__(self, cfg: Config, http: Http | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.key_env = cfg.providers.fal.api_key_env
        self.timeout = cfg.providers.fal.timeout_s
        self.http, self.sleep = http or Http(), sleep

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Key {_key(self.key_env, 'fal.ai')}"}

    def run(self, model: str, payload: dict[str, Any]) -> Any:
        sub = self.http.request("POST", f"{self.QUEUE}/{model}", self._auth(), payload,
                                timeout=300).json()
        rid = sub.get("request_id")
        if not rid:
            raise ProviderError(f"fal {model}: no request id in {str(sub)[:200]}")
        status_url = sub.get("status_url") or f"{self.QUEUE}/{model}/requests/{rid}/status"
        result_url = sub.get("response_url") or f"{self.QUEUE}/{model}/requests/{rid}"
        deadline = time.monotonic() + self.timeout
        delay = 1.0
        while True:
            st = self.http.request("GET", status_url, self._auth(), timeout=60).json()
            state = st.get("status")
            if state == "COMPLETED":
                break
            if state not in ("IN_QUEUE", "IN_PROGRESS"):
                raise ProviderError(f"fal {model}: {st.get('error') or state or st}")
            if time.monotonic() > deadline:
                raise ProviderError(f"fal {model}: no result after {self.timeout} s")
            self.sleep(delay)
            delay = min(delay * 1.5, 5.0)
        return self.http.request("GET", result_url, self._auth(), timeout=120).json()

    def download(self, url: str, dst: Path) -> Path:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if url.startswith("data:"):
            dst.write_bytes(base64.b64decode(url.split(",", 1)[1]))
            return dst
        r = self.http.request("GET", url, timeout=300)
        dst.write_bytes(r.body)
        return dst


def _file_url(v: Any, what: str) -> str:
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict) and v.get("url"):
        return str(v["url"])
    raise ProviderError(f"fal returned no {what}: {str(v)[:200]}")


class FalImageEdit:
    def __init__(self, cfg: Config, fal: Fal) -> None:
        self.model, self.fal = cfg.providers.fal.image_edit, fal
        self.info = _info("fal-image", self.model, "fal.ai API terms")

    def edit(self, src: Path, refs: list[Path], prompt: str, negative: str, seed: int,
             dst: Path) -> Path:
        text = prompt
        if refs:
            text += (" Match the drawing style, palette and line work of the other image(s); "
                     "keep the first image's composition, objects and positions.")
        if negative:
            text += f" Avoid: {negative}."
        payload: dict[str, Any] = {"prompt": text, "seed": seed % 2**31, "num_images": 1,
                                   "output_format": "png"}
        uris = [data_uri(p) for p in [src, *refs]]
        if "kontext" in self.model and "multi" not in self.model:
            payload["image_url"] = uris[0]
        else:
            payload["image_urls"] = uris
        out = self.fal.run(self.model, payload)
        tmp = dst.with_suffix(".download")
        self.fal.download(_file_url(out.get("images") or out.get("image"), "image"), tmp)
        try:
            return conform_image(tmp, src, dst)
        finally:
            tmp.unlink(missing_ok=True)


class FalVideo:
    """Image-to-video; with a last frame when the model takes one (Wan 2.2 does)."""

    def __init__(self, cfg: Config, fal: Fal) -> None:
        f = cfg.providers.fal
        self.model, self.resolution, self.fal = f.video, f.video_resolution, fal
        self.info = _info("fal-video", self.model, "fal.ai API terms")

    def _gen(self, first: Path, last: Path | None, frames: int, fps: int, prompt: str,
             negative: str, seed: int, dst: Path) -> Path:
        payload: dict[str, Any] = {
            "image_url": data_uri(first), "prompt": prompt, "negative_prompt": negative,
            "num_frames": max(17, frames), "frames_per_second": fps, "seed": seed % 2**31,
            "resolution": self.resolution, "aspect_ratio": "auto",
            "interpolator_model": "none", "num_interpolated_frames": 0,
            "adjust_fps_for_interpolation": False, "enable_prompt_expansion": False}
        if last is not None:
            payload["end_image_url"] = data_uri(last)
        out = self.fal.run(self.model, payload)
        tmp = dst.with_suffix(".download.mp4")
        self.fal.download(_file_url(out.get("video"), "video"), tmp)
        try:
            return conform_video(tmp, first, frames, fps, dst)
        finally:
            tmp.unlink(missing_ok=True)

    def i2v(self, first: Path, frames: int, fps: int, prompt: str, negative: str, seed: int,
            dst: Path) -> Path:
        return self._gen(first, None, frames, fps, prompt, negative, seed, dst)

    def flf2v(self, first: Path, last: Path, frames: int, fps: int, prompt: str,
              negative: str, seed: int, dst: Path) -> Path:
        return self._gen(first, last, frames, fps, prompt, negative, seed, dst)


class FalFoley:
    """Video-to-audio (MMAudio): sounds that follow what happens on screen."""

    def __init__(self, cfg: Config, fal: Fal) -> None:
        self.model, self.fal = cfg.providers.fal.foley, fal
        self.info = _info("fal-foley", self.model, "fal.ai API terms")

    def foley(self, video: Path, cues: list[TimedCue], duration: float, seed: int,
              dst: Path) -> Path:
        sounds = ", ".join(dict.fromkeys(c.phrase for c in cues)) or "kitchen ambience"
        payload = {"video_url": data_uri(video), "duration": max(1.0, min(30.0, duration)),
                   "prompt": f"close-up cooking sounds: {sounds}; crisp, natural, no music",
                   "negative_prompt": "music, speech, talking, singing, voice",
                   "seed": seed % 65536}
        out = self.fal.run(self.model, payload)
        tmp = dst.with_suffix(".download.mp4")
        self.fal.download(_file_url(out.get("video") or out.get("audio"), "audio"), tmp)
        try:
            return conform_audio(tmp, duration, dst)
        finally:
            tmp.unlink(missing_ok=True)


class FalASR:
    def __init__(self, cfg: Config, fal: Fal) -> None:
        self.model, self.fal = cfg.providers.fal.asr, fal
        self.info = _info("fal-asr", self.model, "fal.ai API terms")

    def count_words(self, wav: Path) -> int:
        out = self.fal.run(self.model, {"audio_url": data_uri(wav), "task": "transcribe",
                                        "language": None})
        return len(re.findall(r"[^\W_]{2,}", str(out.get("text", "")), re.UNICODE))


class FalSeparator:
    def __init__(self, cfg: Config, fal: Fal) -> None:
        self.model, self.fal = cfg.providers.fal.separator, fal
        self.info = _info("fal-separator", self.model, "fal.ai API terms")

    def voice(self, mix: Path, dst: Path) -> Path:
        out = self.fal.run(self.model, {"audio_url": data_uri(mix), "stems": ["vocals"],
                                        "output_format": "wav"})
        tmp = dst.with_suffix(".download.wav")
        self.fal.download(_file_url(out.get("vocals"), "vocals stem"), tmp)
        try:
            return conform_audio(tmp, None, dst)
        finally:
            tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------- Fish Audio


class FishVoice:
    """Non-verbal voice sounds (sighs, "mm", breaths) with Fish Audio TTS."""

    def __init__(self, cfg: Config, http: Http | None = None) -> None:
        f = cfg.providers.fish
        self.base, self.key_env = f.base_url.rstrip("/"), f.api_key_env
        self.model = cfg.audio.voice.model
        self.http = http or Http()
        self.info = _info("fish", self.model, "Fish Audio API terms")

    def vocal(self, direction: str, voice_ref: str | None, seed: int, dst: Path) -> Path:
        body: dict[str, Any] = {"text": direction, "format": "wav", "normalize": False,
                                "latency": "normal"}
        if voice_ref:
            body["reference_id"] = voice_ref
        r = self.http.request("POST", f"{self.base}/v1/tts",
                              {"Authorization": f"Bearer {_key(self.key_env, 'Fish Audio')}",
                               "model": self.model}, body, timeout=120)
        tmp = dst.with_suffix(".download.wav")
        tmp.write_bytes(r.body)
        try:
            return conform_audio(tmp, None, dst)
        finally:
            tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------- the stack


def cloud_stack(cfg: Config, http: Http | None = None) -> Stack:
    http = http or Http()
    fal = Fal(cfg, http)
    vlm = OpenAIVision(cfg, http)
    video: Any = FalVideo(cfg, fal)
    if cfg.generate.backend == "comfyui":
        from c2a.providers.comfyui import ComfyVideo

        video = ComfyVideo(cfg, http)
    return Stack(vlm=vlm, image_edit=FalImageEdit(cfg, fal), video_gen=video,
                 foley=FalFoley(cfg, fal), voice=FishVoice(cfg, http), asr=FalASR(cfg, fal),
                 ocr=vlm if cfg.providers.cloud_vlm.ocr else NoOCR(),
                 separator=FalSeparator(cfg, fal), word_timer=EnergyWordTimer())


def missing_keys(cfg: Config) -> list[tuple[str, str]]:
    """(env var, what for) for every key the cloud stack needs and does not have."""
    need = [(cfg.providers.cloud_vlm.api_key_env, "vision model (tagging, checks)"),
            (cfg.providers.fal.api_key_env, "fal.ai (restyle, foley"
             + (")" if cfg.generate.backend == "comfyui" else ", video)"))]
    if cfg.audio.voice.enabled and cfg.audio.mode == "regenerate":
        need.append((cfg.providers.fish.api_key_env, "Fish Audio (voice sounds)"))
    return [(e, w) for e, w in need if not os.environ.get(e, "").strip()]
