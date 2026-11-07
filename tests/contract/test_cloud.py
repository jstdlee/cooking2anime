"""Contract tests for the cloud stack: request shape, auth, retries, the fal queue, and
outputs conformed to what the pipeline expects. No network: `Http` gets a scripted sender."""

from __future__ import annotations

import base64
import json
import subprocess
import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from c2a.config import Config, load_config
from c2a.core import media
from c2a.core.errors import ProviderError
from c2a.providers.base import TimedCue
from c2a.providers.cloud import (
    Fal,
    FalASR,
    FalFoley,
    FalImageEdit,
    FalSeparator,
    FalVideo,
    FishVoice,
    Http,
    OpenAIVision,
    Response,
    cloud_stack,
    missing_keys,
    parse_json_text,
)
from c2a.providers.registry import build_stack


class Script:
    """A fake HTTP server: (method, url-substring) → responses, in order. Records requests."""

    def __init__(self, routes: list[tuple[str, str, Response]]) -> None:
        self.routes = list(routes)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None,
                 timeout: float) -> Response:
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "json": json.loads(body) if body else None})
        for i, (m, part, resp) in enumerate(self.routes):
            if m == method and part in url:
                del self.routes[i]
                return resp
        raise AssertionError(f"unexpected {method} {url}")


def ok(v: Any, status: int = 200) -> Response:
    return Response(status, json.dumps(v).encode())


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    monkeypatch.setenv("FAL_KEY", "f-key")
    monkeypatch.setenv("FISH_API_KEY", "fish-key")


def png(path: Path, w: int = 64, h: int = 36, v: int = 120) -> Path:
    media.save_png(np.full((h, w, 3), v, np.uint8), path)
    return path


def mp4(path: Path, w: int = 96, h: int = 54, frames: int = 20, fps: int = 16,
        audio: bool = False) -> Path:
    args = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
            f"testsrc2=s={w}x{h}:r={fps}:d={frames / fps}"]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=f=440:d=2", "-shortest", "-c:a", "aac"]
    subprocess.run([*args, "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def wav(path: Path, seconds: float = 0.5, sr: int = 22050, ch: int = 1) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.sin(np.arange(int(seconds * sr)) / 5) * 8000).astype("<i2").tobytes())
    return path


def wav_info(path: Path) -> tuple[int, int, float]:
    with wave.open(str(path), "rb") as w:
        return w.getframerate(), w.getnchannels(), w.getnframes() / w.getframerate()


def http(script: Script) -> Http:
    return Http(script, retries=2, backoff_s=0, sleep=lambda s: None)


def fal_ok(model: str, result: Any, download: tuple[str, bytes] | None = None
           ) -> list[tuple[str, str, Response]]:
    routes = [("POST", f"queue.fal.run/{model}", ok({"request_id": "r1",
               "status_url": f"https://queue.fal.run/{model}/requests/r1/status",
               "response_url": f"https://queue.fal.run/{model}/requests/r1"})),
              ("GET", "/requests/r1/status", ok({"status": "IN_QUEUE"})),
              ("GET", "/requests/r1/status", ok({"status": "COMPLETED"})),
              ("GET", f"{model}/requests/r1", ok(result))]
    if download:
        routes.append(("GET", download[0], Response(200, download[1])))
    return routes


# ---------------------------------------------------------------- HTTP


def test_http_retries_429_and_5xx_then_succeeds() -> None:
    s = Script([("GET", "x", Response(429, b"{}")), ("GET", "x", Response(503, b"")),
                ("GET", "x", ok({"a": 1}))])
    assert http(s).request("GET", "https://x").json() == {"a": 1}
    assert len(s.calls) == 3


def test_http_does_not_retry_client_errors_and_reports_the_detail() -> None:
    s = Script([("POST", "x", ok({"detail": "bad prompt"}, 422))])
    with pytest.raises(ProviderError, match="422: bad prompt"):
        http(s).request("POST", "https://x", body={})
    assert len(s.calls) == 1


def test_http_gives_up_after_the_retries() -> None:
    s = Script([("GET", "x", Response(500, b"boom"))] * 3)
    with pytest.raises(ProviderError, match="failed after 3 tries"):
        http(s).request("GET", "https://x")


# ---------------------------------------------------------------- vision model


def test_vlm_tag_sends_key_image_and_frames_and_parses_fenced_json(keys: None,
                                                                    tmp_path: Path) -> None:
    reply = {"choices": [{"message": {"content": "```json\n{\"action\": \"dice\"}\n```"}}]}
    s = Script([("POST", "/chat/completions", ok(reply))])
    vlm = OpenAIVision(Config(), http(s))
    out = vlm.tag(png(tmp_path / "k.png"), mp4(tmp_path / "c.mp4"), 1.2, "Tag it.",
                  {"type": "object"}, 7)
    assert out == {"action": "dice"}
    call = s.calls[0]
    assert call["url"] == ("https://generativelanguage.googleapis.com/v1beta/openai/"
                           "chat/completions")
    assert call["headers"]["Authorization"] == "Bearer g-key"
    body = call["json"]
    assert body["model"] == "gemini-2.5-flash" and body["seed"] == 7
    assert body["response_format"] == {"type": "json_object"}
    parts = body["messages"][0]["content"]
    assert parts[0]["type"] == "text" and "JSON schema" in parts[0]["text"]
    images = [p for p in parts if p["type"] == "image_url"]
    assert images[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert 1 < len(images) <= 1 + Config().providers.cloud_vlm.max_frames


def test_vlm_works_with_any_openai_compatible_server(keys: None, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config(None, {"providers": {"cloud_vlm": {
        "base_url": "http://127.0.0.1:11434/v1/", "model": "qwen2.5vl", "api_key_env": "MY_KEY"}}})
    monkeypatch.setenv("MY_KEY", "local")
    s = Script([("POST", "127.0.0.1:11434/v1/chat/completions",
                 ok({"choices": [{"message": {"content": '{"text_boxes": 2}'}}]}))])
    assert OpenAIVision(cfg, http(s)).count_text_boxes(png(tmp_path / "i.png"), 0.6) == 2
    assert s.calls[0]["json"]["model"] == "qwen2.5vl"


def test_vlm_non_json_reply_goes_back_to_the_stage_as_invalid(keys: None, tmp_path: Path) -> None:
    s = Script([("POST", "chat", ok({"choices": [{"message": {"content": "sorry, no"}}]}))])
    out = OpenAIVision(Config(), http(s)).tag(png(tmp_path / "k.png"), mp4(tmp_path / "c.mp4"),
                                              1.0, "x", {}, 0)
    assert "error" in out  # validate_tags rejects it; the tag stage retries, then fails


def test_vlm_motion_score_is_clamped(keys: None, tmp_path: Path) -> None:
    s = Script([("POST", "chat", ok({"choices": [{"message": {"content": '{"score": 1.7}'}}]}))])
    assert OpenAIVision(Config(), http(s)).judge_motion(mp4(tmp_path / "c.mp4"), ["chop"], 1) == 1.0


def test_missing_vlm_key_names_the_variable(tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="GEMINI_API_KEY"):
        OpenAIVision(Config(), http(Script([]))).chat_json("x", [], 0)


def test_parse_json_text_finds_the_object() -> None:
    assert parse_json_text('Here: {"a": [1, 2]} done') == {"a": [1, 2]}


# ---------------------------------------------------------------- fal.ai


def test_fal_queue_submit_poll_result_with_key_auth(keys: None) -> None:
    s = Script(fal_ok("fal-ai/x", {"ok": True}))
    assert Fal(Config(), http(s), sleep=lambda t: None).run("fal-ai/x", {"a": 1}) == {"ok": True}
    assert all(c["headers"]["Authorization"] == "Key f-key" for c in s.calls)
    assert s.calls[0]["json"] == {"a": 1}


def test_fal_failure_status_is_reported(keys: None) -> None:
    s = Script([("POST", "queue.fal.run/m", ok({"request_id": "r", "status_url": "https://q/s"})),
                ("GET", "q/s", ok({"status": "FAILED", "error": "NSFW input"}))])
    with pytest.raises(ProviderError, match="NSFW input"):
        Fal(Config(), http(s), sleep=lambda t: None).run("m", {})


def test_image_edit_sends_source_and_refs_and_conforms_the_size(keys: None,
                                                                 tmp_path: Path) -> None:
    out_png = png(tmp_path / "big.png", 200, 200, 30)
    model = Config().providers.fal.image_edit
    s = Script(fal_ok(model, {"images": [{"url": "https://v3.fal.media/o.png"}]},
                      ("v3.fal.media/o.png", out_png.read_bytes())))
    fal = Fal(Config(), http(s), sleep=lambda t: None)
    src, ref = png(tmp_path / "src.png", 64, 36), png(tmp_path / "ref.png", 64, 36, 200)
    dst = FalImageEdit(Config(), fal).edit(src, [ref], "anime style", "photo", 5,
                                           tmp_path / "d.png")
    body = s.calls[0]["json"]
    assert len(body["image_urls"]) == 2 and body["seed"] == 5
    assert "anime style" in body["prompt"] and "Avoid: photo" in body["prompt"]
    assert media.load_png(dst).shape == (36, 64, 3)


def test_video_conforms_frames_fps_and_size_and_passes_the_last_frame(keys: None,
                                                                       tmp_path: Path) -> None:
    model = Config().providers.fal.video
    vid = mp4(tmp_path / "gen.mp4", 160, 96, frames=12, fps=16)  # fewer frames, other size
    s = Script(fal_ok(model, {"video": {"url": "https://v3.fal.media/v.mp4"}},
                      ("v3.fal.media/v.mp4", vid.read_bytes())))
    fal = Fal(Config(), http(s), sleep=lambda t: None)
    first, last = png(tmp_path / "a.png", 64, 36), png(tmp_path / "b.png", 64, 36, 200)
    dst = FalVideo(Config(), fal).flf2v(first, last, 17, 16, "chop", "blur", 3, tmp_path / "o.mp4")
    body = s.calls[0]["json"]
    assert body["num_frames"] == 17 and body["frames_per_second"] == 16
    assert body["end_image_url"].startswith("data:image/png")
    assert body["interpolator_model"] == "none" and body["resolution"] == "480p"
    info = media.probe(dst, count_frames=True)
    assert (info.width, info.height, info.frames) == (64, 36, 17)


def test_foley_returns_48k_stereo_of_the_clip_length(keys: None, tmp_path: Path) -> None:
    model = Config().providers.fal.foley
    withsound = mp4(tmp_path / "s.mp4", frames=32, audio=True)
    s = Script(fal_ok(model, {"video": {"url": "https://v3.fal.media/s.mp4"}},
                      ("v3.fal.media/s.mp4", withsound.read_bytes())))
    fal = Fal(Config(), http(s), sleep=lambda t: None)
    cues = [TimedCue(0, 1, "knife on board", "chop")]
    dst = FalFoley(Config(), fal).foley(mp4(tmp_path / "c.mp4"), cues, 1.25, 70000,
                                        tmp_path / "f.wav")
    body = s.calls[0]["json"]
    assert "knife on board" in body["prompt"] and body["seed"] == 70000 % 65536
    sr, ch, dur = wav_info(dst)
    assert (sr, ch) == (48000, 2) and abs(dur - 1.25) < 0.01


def test_asr_counts_words_and_separator_returns_the_vocals(keys: None, tmp_path: Path) -> None:
    cfg = Config()
    voc = wav(tmp_path / "voc.wav")
    s = Script(fal_ok(cfg.providers.fal.asr, {"text": "Oh, that's good!"})
               + fal_ok(cfg.providers.fal.separator, {"vocals": {"url": "https://v3.fal.media/v.wav"}},
                        ("v3.fal.media/v.wav", voc.read_bytes())))
    fal = Fal(cfg, http(s), sleep=lambda t: None)
    assert FalASR(cfg, fal).count_words(wav(tmp_path / "w.wav")) == 3
    dst = FalSeparator(cfg, fal).voice(wav(tmp_path / "mix.wav"), tmp_path / "v.wav")
    assert wav_info(dst)[:2] == (48000, 2)


# ---------------------------------------------------------------- Fish Audio


def test_fish_voice_sends_the_direction_with_model_header(keys: None, tmp_path: Path) -> None:
    audio = wav(tmp_path / "o.wav").read_bytes()
    s = Script([("POST", "api.fish.audio/v1/tts", Response(200, audio))])
    dst = FishVoice(Config(), http(s)).vocal("[soft satisfied sigh]", "ref1", 0, tmp_path / "v.wav")
    call = s.calls[0]
    assert call["headers"]["Authorization"] == "Bearer fish-key"
    assert call["headers"]["model"] == Config().audio.voice.model
    assert call["json"]["text"] == "[soft satisfied sigh]"
    assert call["json"]["reference_id"] == "ref1"
    assert wav_info(dst)[:2] == (48000, 2)


# ---------------------------------------------------------------- the stack


def test_cloud_stack_needs_keys_and_says_which(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("GEMINI_API_KEY", "FAL_KEY", "FISH_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    cfg = Config()
    assert [e for e, _ in missing_keys(cfg)] == ["GEMINI_API_KEY", "FAL_KEY", "FISH_API_KEY"]
    with pytest.raises(ProviderError, match=r"GEMINI_API_KEY.*FAL_KEY.*FISH_API_KEY"):
        build_stack(cfg)
    quiet = load_config(None, {"audio": {"voice": {"enabled": False}}})  # no voice sounds
    assert [e for e, _ in missing_keys(quiet)] == ["GEMINI_API_KEY", "FAL_KEY"]


def test_cloud_stack_is_built_with_keys(keys: None) -> None:
    stack = build_stack(Config())
    assert stack.vlm.info.name == "vlm:gemini-2.5-flash"
    assert stack.video_gen.info.name.startswith("fal-video:")
    assert not any(p.info.deterministic for p in (stack.vlm, stack.image_edit, stack.video_gen))
    assert stack.ocr is stack.vlm  # on-screen text is checked by the same vision model
    no_ocr = cloud_stack(load_config(None, {"providers": {"cloud_vlm": {"ocr": False}}}))
    assert no_ocr.ocr.count_text_boxes(Path("unused.png"), 0.6) == 0


def test_data_uri_round_trip(tmp_path: Path) -> None:
    from c2a.providers.cloud import data_uri

    p = png(tmp_path / "a.png")
    uri = data_uri(p)
    assert base64.b64decode(uri.split(",", 1)[1]) == p.read_bytes()
