"""Contract tests for the ComfyUI video backend (Wan 2.2), against a scripted server."""

from __future__ import annotations

from pathlib import Path

import pytest

from c2a.config import load_config
from c2a.core import media
from c2a.core.errors import ProviderError
from c2a.providers.comfyui import ComfyVideo, gen_size, wan22_workflow
from c2a.providers.registry import build_stack
from tests.contract.test_cloud import Response, Script, http, mp4, ok, png

CFG = load_config(
    None, {"generate": {"backend": "comfyui"}, "providers": {"comfyui": {"url": "http://dgx:8188"}}}
)


def comfy_routes(video: bytes, uploads: int = 1) -> list[tuple[str, str, Response]]:
    routes = [
        ("POST", "/upload/image", ok({"name": f"c2a_{i}.png", "subfolder": "", "type": "input"}))
        for i in range(uploads)
    ]
    return [
        *routes,
        ("POST", "dgx:8188/prompt", ok({"prompt_id": "p1", "number": 1, "node_errors": {}})),
        ("GET", "/history/p1", ok({})),  # still running
        (
            "GET",
            "/history/p1",
            ok(
                {
                    "p1": {
                        "status": {"status_str": "success", "completed": True},
                        "outputs": {
                            "16": {
                                "images": [
                                    {
                                        "filename": "clip_00001.mp4",
                                        "subfolder": "c2a",
                                        "type": "output",
                                    }
                                ]
                            }
                        },
                    }
                }
            ),
        ),
        ("GET", "/view?filename=clip_00001.mp4&subfolder=c2a&type=output", Response(200, video)),
    ]


def test_first_last_frame_clip_uploads_both_and_conforms(tmp_path: Path) -> None:
    vid = mp4(tmp_path / "gen.mp4", 128, 72, frames=16, fps=16)
    s = Script(comfy_routes(vid.read_bytes(), uploads=2))
    first, last = png(tmp_path / "a.png", 64, 36), png(tmp_path / "b.png", 64, 36, 200)
    dst = ComfyVideo(CFG, http(s), sleep=lambda t: None).flf2v(
        first, last, 17, 16, "chop", "blur", 2**40 + 5, tmp_path / "o.mp4"
    )
    up = [c for c in s.calls if c["url"].endswith("/upload/image")]
    assert len(up) == 2 and up[0]["headers"]["Content-Type"].startswith("multipart/form-data")
    graph = next(c for c in s.calls if c["url"].endswith("/prompt"))["json"]["prompt"]
    node = graph["9"]
    assert node["class_type"] == "WanFirstLastFrameToVideo"
    assert node["inputs"]["length"] == 17 and node["inputs"]["end_image"] == ["8", 0]
    assert graph["12"]["inputs"]["noise_seed"] == (2**40 + 5) % 2**32
    assert graph["15"]["inputs"]["fps"] == 16
    info = media.probe(dst, count_frames=True)
    assert (info.width, info.height, info.frames) == (64, 36, 17)


def test_single_frame_clip_uses_wan_image_to_video() -> None:
    g = wan22_workflow(CFG, "a.png", None, 848, 480, 33, 16, "p", "n", 1)
    assert g["9"]["class_type"] == "WanImageToVideo" and "8" not in g
    assert g["1"]["inputs"]["unet_name"] == CFG.providers.comfyui.wan_high_noise
    assert g["12"]["inputs"]["end_at_step"] == g["13"]["inputs"]["start_at_step"] == 10


def test_generation_size_keeps_aspect_in_multiples_of_16(tmp_path: Path) -> None:
    assert gen_size(png(tmp_path / "w.png", 1280, 720), "480p") == (848, 480)
    assert gen_size(png(tmp_path / "v.png", 720, 1280), "720p") == (720, 1280)


def test_workflow_errors_are_reported(tmp_path: Path) -> None:
    s = Script(
        [
            ("POST", "/upload/image", ok({"name": "x.png"})),
            ("POST", "/prompt", ok({"prompt_id": "p1", "node_errors": {}})),
            (
                "GET",
                "/history/p1",
                ok(
                    {
                        "p1": {
                            "status": {
                                "status_str": "error",
                                "messages": [
                                    ["execution_error", {"exception_message": "CUDA out of memory"}]
                                ],
                            },
                            "outputs": {},
                        }
                    }
                ),
            ),
        ]
    )
    with pytest.raises(ProviderError, match="CUDA out of memory"):
        ComfyVideo(CFG, http(s), sleep=lambda t: None).i2v(
            png(tmp_path / "a.png"), 17, 16, "p", "n", 1, tmp_path / "o.mp4"
        )
    s = Script(
        [
            ("POST", "/upload/image", ok({"name": "x.png"})),
            ("POST", "/prompt", ok({"prompt_id": "p2", "node_errors": {"1": "model not found"}})),
        ]
    )
    with pytest.raises(ProviderError, match="model not found"):
        ComfyVideo(CFG, http(s), sleep=lambda t: None).i2v(
            png(tmp_path / "a.png"), 17, 16, "p", "n", 1, tmp_path / "o.mp4"
        )


def test_comfyui_backend_needs_no_fal_video_and_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in (("GEMINI_API_KEY", "g"), ("FAL_KEY", "f"), ("FISH_API_KEY", "x")):
        monkeypatch.setenv(k, v)
    stack = build_stack(CFG)
    assert stack.video_gen.info.name.startswith("comfyui:")
    assert stack.image_edit.info.name.startswith("fal-image:")
