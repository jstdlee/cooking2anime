"""Studio server: auth, commands, WebSocket updates, media (in-process, no sockets)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from c2a.config import load_config
from c2a.providers.fake import fake_stack
from c2a.studio.server import Studio, create_app

TOKEN = "t0ken"
H = {"x-c2a-token": TOKEN}


@pytest.fixture
def client(tmp_path: Path) -> tuple[TestClient, Studio]:
    cfg = load_config(None, {"run": {"stack": "fake"}, "ingest": {"shot_detector": "ffmpeg_scene"},
                             "style": {"preview": {"sample_keys": 2, "seeds_per_key": 2}}})
    st = Studio(cfg, tmp_path / "runs", token=TOKEN, stack=fake_stack())
    return TestClient(create_app(st)), st


def wait_view(ws: Any, pred: Any, timeout: float = 120) -> dict[str, Any]:
    end = time.time() + timeout
    while time.time() < end:
        m = ws.receive_json()
        if m["type"] == "view" and pred(m["view"]):
            v: dict[str, Any] = m["view"]
            return v
    raise AssertionError("view never matched")


def upload(c: TestClient, path: Path) -> str:
    with open(path, "rb") as f:
        r = c.post("/api/runs", headers=H, files={"video": (path.name, f, "video/mp4")})
    assert r.status_code == 200, r.text
    rid: str = r.json()["run"]
    return rid


def test_auth_is_required_everywhere(client: tuple[TestClient, Studio]) -> None:
    c, _ = client
    assert c.get("/api/runs").status_code == 401
    assert c.get("/api/runs", headers={"x-c2a-token": "nope"}).status_code == 401
    assert c.get("/api/doctor", headers=H).status_code == 200
    assert c.get("/media/x/y.mp4").status_code == 401
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect), \
            c.websocket_connect("/api/runs/x/ws?token=nope") as ws:
        ws.receive_json()
    # the page itself loads and sets the cookie when the token is right
    r = c.get(f"/?token={TOKEN}")
    assert r.status_code == 200 and "c2a_token" in r.headers.get("set-cookie", "")


def test_upload_validates_files(client: tuple[TestClient, Studio], tmp_path: Path) -> None:
    c, _ = client
    bad = tmp_path / "notes.txt"
    bad.write_text("x")
    with open(bad, "rb") as f:
        r = c.post("/api/runs", headers=H, files={"video": ("notes.txt", f, "text/plain")})
    assert r.status_code == 400


def test_full_flow_over_websocket(client: tuple[TestClient, Studio],
                                  fixtures: dict[str, Path]) -> None:
    c, _ = client
    rid = upload(c, fixtures["cut"])
    with c.websocket_connect(f"/api/runs/{rid}/ws?token={TOKEN}") as ws:
        v = wait_view(ws, lambda v: v["state"] == "awaiting_approval" and not v.get("busy"))
        n_clips = len(v["plan"]["clips"])
        extra = next(k for k in v["keys"] if not k["checked"])
        ws.send_json({"op": "toggle_key", "args": {"start": extra["start"], "checked": True},
                      "op_id": "1"})
        v = wait_view(ws, lambda v: len(v.get("plan", {}).get("clips", [])) == n_clips + 1)
        ws.send_json({"op": "set_style", "args": {"director": "ozu_inspired"}, "op_id": "2"})
        v = wait_view(ws, lambda v: v["settings"]["style"]["director"] == "ozu_inspired"
                      and v["style"].get("samples_current") and not v.get("busy"))
        ws.send_json({"op": "approve", "args": {"sample": "0:1"}, "op_id": "3"})
        v = wait_view(ws, lambda v: v["state"] == "done" and not v.get("busy"))
        ws.send_json({"op": "toggle_key", "args": {"start": 0, "checked": False}, "op_id": "4"})
        while True:
            m = ws.receive_json()
            if m["type"] == "notice":
                assert "cannot be unchecked" in m["text"]
                break
    clip = v["clips"][0]["video"]
    r = c.get(f"/media/{rid}/{clip}", headers={**H, "Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100
    assert c.get(f"/media/{rid}/{v['output']['video']}", headers=H).status_code == 200
    assert c.get(f"/media/{rid}/../../etc/passwd", headers=H).status_code == 404
    assert c.get(f"/media/{rid}/manifest.lock", headers=H).status_code == 404


def test_commands_endpoint_is_validated_and_idempotent(client: tuple[TestClient, Studio],
                                                      fixtures: dict[str, Path]) -> None:
    c, st = client
    st.autostart = False  # this test only checks command handling
    rid = upload(c, fixtures["vertical"])
    url = f"/api/runs/{rid}/commands"
    assert c.post(url, headers=H, json={"op": "explode"}).status_code == 400
    assert c.post(url, headers=H, json={"nope": 1}).status_code == 400
    assert c.post(url, headers=H, json={"op": "set_audio", "args": {"bogus": 1}}).status_code \
        == 400
    assert c.post(url, headers=H, json={"op": "set_audio", "args": {"mode": "loud"}}).status_code \
        == 409
    ok = {"op": "set_audio", "args": {"mode": "silent"}, "op_id": "same"}
    assert c.post(url, headers=H, json=ok).json() == c.post(url, headers=H, json=ok).json()
    assert c.get(f"/api/runs/{rid}", headers=H).json()["settings"]["audio"]["mode"] == "silent"
    assert c.get("/api/runs/../etc", headers=H).status_code == 404
    r = c.put(f"/api/runs/{rid}/board", headers=H, content=b"{not json")
    assert r.status_code == 400
    assert c.put(f"/api/runs/{rid}/board", headers=H, content=b"{}").status_code == 200
