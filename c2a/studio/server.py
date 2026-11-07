"""c2a Studio server (DESIGN_V2 §1, §6): engine + API + the bundled canvas app.

- HTTP:  GET  /api/doctor · GET/POST /api/runs · GET /api/runs/{id}
         POST /api/runs/{id}/commands · PUT /api/runs/{id}/board
         GET  /media/{id}/{path}   (allow-listed files, HTTP Range for video scrubbing)
- WS:    /api/runs/{id}/ws  →  {"type": "view" | "progress" | "notice", ...}
         (the client may also send commands on it)
- Auth:  a random token printed at start; sent as ?token=…, header X-C2A-Token or cookie.

Each run has a worker thread. Any command that changes the run "kicks" it: the pipeline
runs (from cache) until it is done or waits at the style approval; a kick during a run
re-runs once it finishes. Views are pushed to every open tab after each change.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import threading
import time
import webbrowser
from collections import OrderedDict
from collections.abc import Callable
from html import escape as html_escape
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.websockets import WebSocketDisconnect

from c2a import __version__, app
from c2a.config import Config
from c2a.core.errors import C2AError
from c2a.core.manifest import Run, atomic_write_bytes
from c2a.prompts.style import catalog
from c2a.providers.base import Stack

MEDIA_TYPES = {".mp4": "video/mp4", ".png": "image/png", ".jpg": "image/jpeg",
               ".json": "application/json", ".wav": "audio/wav", ".tldr": "application/json"}
MAX_UPLOAD = 4 * 1024**3
COMMANDS: dict[str, Callable[..., Any]] = {}


def command(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        COMMANDS[name] = fn
        return fn
    return deco


@command("resume")
def _resume(run: Run) -> None:
    """Continue the pipeline (e.g. after a failure, once keys or settings are fixed)."""


@command("toggle_key")
def _toggle(run: Run, start: int, checked: bool) -> None:
    app.toggle_key(run, int(start), bool(checked))


@command("add_key")
def _add(run: Run, t: float) -> dict[str, int]:
    return {"frame": app.add_key(run, float(t))}


@command("edit_tags")
def _tags(run: Run, start: int, tags: dict[str, Any]) -> None:
    app.edit_tags(run, int(start), tags)


@command("set_motion")
def _motion(run: Run, start: int, text: str | None) -> None:
    app.set_motion(run, int(start), text)


@command("set_style")
def _style(run: Run, **sel: Any) -> None:
    app.set_style(run, **sel)


@command("set_clips")
def _clips(run: Run, **clips: Any) -> None:
    app.set_clips(run, **clips)


@command("set_audio")
def _audio(run: Run, mode: str) -> None:
    app.set_audio(run, mode)


@command("approve")
def _approve(run: Run, sample: str) -> None:
    app.approve(run, sample, via="studio")


@command("rerun")
def _rerun(run: Run, clip: str) -> None:
    app.rerun(run, clip)


@command("generate")
def _generate(run: Run) -> None:
    return None  # the kick after every command does the work


class Hub:
    """WebSocket subscribers per run; thread-safe publish."""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.subs: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self.lock = threading.Lock()

    def subscribe(self, run_id: str) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        with self.lock:
            self.subs.setdefault(run_id, set()).add(q)
        return q

    def unsubscribe(self, run_id: str, q: asyncio.Queue[dict[str, Any]]) -> None:
        with self.lock:
            self.subs.get(run_id, set()).discard(q)

    def publish(self, run_id: str, msg: dict[str, Any]) -> None:
        if self.loop is None:
            return
        with self.lock:
            targets = list(self.subs.get(run_id, ()))
        for q in targets:
            def put(q: asyncio.Queue[dict[str, Any]] = q) -> None:
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(msg)
            self.loop.call_soon_threadsafe(put)


class Worker:
    """Runs the pipeline for one run in a background thread, on demand."""

    def __init__(self, run: Run, hub: Hub, stack: Stack | None) -> None:
        self.run, self.hub, self.stack = run, hub, stack
        self.kicked = threading.Event()
        self.cancel = threading.Event()
        self.busy = False
        self.last_progress = 0.0
        self.progress: dict[str, Any] | None = None  # the latest {stage, unit, pct}
        self.thread = threading.Thread(target=self._loop, daemon=True, name=f"c2a-{run.id}")
        self.thread.start()

    def kick(self) -> None:
        self.cancel.clear()
        self.kicked.set()

    def _progress(self, stage: str, unit: str, frac: float) -> None:
        self.progress = {"stage": stage, "unit": unit, "pct": round(frac * 100, 1)}
        now = time.monotonic()
        if frac in (0.0, 1.0) or now - self.last_progress > 0.25:
            self.last_progress = now
            self.hub.publish(self.run.id, {"type": "progress", "stage": stage, "unit": unit,
                                           "pct": round(frac * 100, 1)})

    def _loop(self) -> None:
        from c2a.core.pipeline import run_pipeline

        while True:
            self.kicked.wait()
            self.kicked.clear()
            self.busy = True
            self.hub.publish(self.run.id, {"type": "view", "view": self.view()})
            try:
                run_pipeline(self.run, self.stack, progress=self._progress,
                             cancelled=self.cancel.is_set)
            except C2AError as e:
                self.hub.publish(self.run.id, {"type": "notice", "level": "error",
                                               "text": str(e)})
            except Exception as e:  # surface, keep the worker alive
                self.hub.publish(self.run.id, {"type": "notice", "level": "error",
                                               "text": f"{type(e).__name__}: {e}"})
            finally:
                self.busy = False
                self.progress = None
                self.hub.publish(self.run.id, {"type": "view", "view": self.view()})

    def view(self) -> dict[str, Any]:
        v = app.view(self.run)
        v["busy"] = self.busy
        v["progress"] = self.progress
        return v


class Studio:
    def __init__(self, cfg: Config, workdir: Path, token: str | None = None,
                 stack: Stack | None = None, autostart: bool = True) -> None:
        self.cfg, self.workdir = cfg, workdir
        self.token = token or secrets.token_urlsafe(18)
        self.stack, self.autostart = stack, autostart
        self.hub = Hub()
        self.workers: dict[str, Worker] = {}
        self.ops: dict[str, OrderedDict[str, Any]] = {}
        self.lock = threading.Lock()
        workdir.mkdir(parents=True, exist_ok=True)

    def run(self, run_id: str) -> Run:
        if "/" in run_id or ".." in run_id:
            raise HTTPException(404, "no such run")
        try:
            return Run.open(self.workdir / run_id)
        except C2AError:
            raise HTTPException(404, "no such run") from None

    def worker(self, run: Run) -> Worker:
        with self.lock:
            if run.id not in self.workers:
                self.workers[run.id] = Worker(run, self.hub, self.stack)
            return self.workers[run.id]

    def kick(self, run: Run) -> None:
        if self.autostart:
            self.worker(run).kick()
        else:
            self.hub.publish(run.id, {"type": "view", "view": app.view(run)})

    def execute(self, run: Run, op: str, args: dict[str, Any], op_id: str | None) -> Any:
        seen = self.ops.setdefault(run.id, OrderedDict())
        if op_id and op_id in seen:
            return seen[op_id]  # idempotent retry
        if op == "cancel":
            if run.id in self.workers:
                self.workers[run.id].cancel.set()
            return {"ok": True}
        fn = COMMANDS.get(op)
        if fn is None:
            raise HTTPException(400, f"unknown command {op!r}")
        try:
            result = fn(run, **args)
        except TypeError as e:
            raise HTTPException(400, f"bad arguments for {op}: {e}") from None
        except C2AError as e:
            raise HTTPException(409, str(e)) from None
        out = {"ok": True, "result": result}
        if op_id:
            seen[op_id] = out
            while len(seen) > 200:
                seen.popitem(last=False)
        self.kick(run)
        return out


def _static_dir() -> Path | None:
    p = Path(str(resources.files("c2a.studio").joinpath("static")))
    return p if (p / "index.html").exists() else None


FALLBACK = """<!doctype html><meta charset=utf-8><title>c2a Studio</title>
<body style="font-family:sans-serif;padding:2em"><h1>c2a Studio</h1>
<p>The Studio frontend is not built. Run <code>npm ci && npm run build</code> in
<code>studio/</code>, or use a release build. The API is running.</p></body>"""


def create_app(studio: Studio) -> FastAPI:
    api = FastAPI(title="c2a Studio", version=__version__, docs_url=None, redoc_url=None)
    static = _static_dir()

    def authed(request: Request) -> None:
        if studio.hub.loop is None:
            studio.hub.loop = asyncio.get_running_loop()
        tok = (request.headers.get("x-c2a-token") or request.query_params.get("token")
               or request.cookies.get("c2a_token"))
        if not tok or not secrets.compare_digest(tok, studio.token):
            raise HTTPException(401, "missing or wrong Studio token")

    @api.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Response:
        html = (static / "index.html").read_text() if static else FALLBACK
        lic = os.environ.get("C2A_TLDRAW_LICENSE")
        if lic:
            html = html.replace("<head>", f'<head><meta name="c2a-tldraw-license" '
                                f'content="{html_escape(lic)}">', 1)
        resp = HTMLResponse(html)
        tok = request.query_params.get("token")
        if tok and secrets.compare_digest(tok, studio.token):
            resp.set_cookie("c2a_token", tok, httponly=True, samesite="strict")
        return resp

    @api.get("/assets/{name:path}")
    async def assets(name: str) -> Response:
        if static is None or ".." in name:
            raise HTTPException(404)
        f = static / "assets" / name
        if not f.is_file():
            raise HTTPException(404)
        return FileResponse(f)

    @api.get("/api/doctor")
    async def doctor(request: Request) -> JSONResponse:
        authed(request)
        live = request.query_params.get("live") in ("1", "true")
        return JSONResponse({"checks": await asyncio.to_thread(app.doctor, studio.cfg, live)})

    @api.get("/api/runs")
    async def runs(request: Request) -> JSONResponse:
        authed(request)
        return JSONResponse({"runs": app.list_runs(studio.workdir), "version": __version__,
                             "stack": studio.cfg.run.stack, "catalog": catalog()})

    @api.post("/api/runs")
    async def new_run(request: Request, video: UploadFile = File(...),
                      settings: str = Form("{}")) -> JSONResponse:
        authed(request)
        name = Path(video.filename or "upload.mp4").name
        if not name.lower().endswith((".mp4", ".mov", ".mkv", ".webm", ".m4v")):
            raise HTTPException(400, "upload a video file (.mp4, .mov, .mkv, .webm, .m4v)")
        up = studio.workdir / "uploads" / f"{int(time.time())}-{secrets.token_hex(3)}-{name}"
        up.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        with open(up, "wb") as f:
            while chunk := await video.read(1 << 20):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    up.unlink(missing_ok=True)
                    raise HTTPException(413, "video too large")
                f.write(chunk)
        try:
            run = app.create_run(studio.workdir, up, studio.cfg)
            extra = json.loads(settings or "{}")
            if extra:
                app.update_settings(run, extra)
        except C2AError as e:
            raise HTTPException(400, str(e)) from None
        studio.kick(run)
        return JSONResponse({"run": run.id})

    @api.get("/api/runs/{run_id}")
    async def get_run(request: Request, run_id: str) -> JSONResponse:
        authed(request)
        run = studio.run(run_id)
        v = app.view(run)
        w = studio.workers.get(run.id)
        v["busy"] = w.busy if w else False
        v["progress"] = w.progress if w else None
        return JSONResponse(v)

    @api.post("/api/runs/{run_id}/commands")
    async def commands(request: Request, run_id: str) -> JSONResponse:
        authed(request)
        run = studio.run(run_id)
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("op"), str):
            raise HTTPException(400, "expected {op, args?, op_id?}")
        out = await asyncio.to_thread(studio.execute, run, body["op"], body.get("args") or {},
                                      body.get("op_id"))
        return JSONResponse(out)

    @api.put("/api/runs/{run_id}/board")
    async def save_board(request: Request, run_id: str) -> JSONResponse:
        authed(request)
        run = studio.run(run_id)
        data = await request.body()
        if len(data) > 50 * 1024**2:
            raise HTTPException(413, "board too large")
        try:
            json.loads(data)
        except ValueError:
            raise HTTPException(400, "board must be JSON") from None
        atomic_write_bytes(run.path("board.tldr"), data)
        return JSONResponse({"ok": True})

    @api.get("/media/{run_id}/{path:path}")
    async def media(request: Request, run_id: str, path: str) -> Response:
        authed(request)
        run = studio.run(run_id)
        target = (run.root / path).resolve()
        if run.root.resolve() not in target.parents or target.suffix not in MEDIA_TYPES \
                or not target.is_file():
            raise HTTPException(404)
        return FileResponse(target, media_type=MEDIA_TYPES[target.suffix])

    @api.websocket("/api/runs/{run_id}/ws")
    async def ws(sock: WebSocket, run_id: str) -> None:
        studio.hub.loop = asyncio.get_running_loop()
        tok = sock.query_params.get("token") or sock.cookies.get("c2a_token")
        if not tok or not secrets.compare_digest(tok, studio.token):
            await sock.close(code=4401)
            return
        try:
            run = studio.run(run_id)
        except HTTPException:
            await sock.close(code=4404)
            return
        await sock.accept()
        q = studio.hub.subscribe(run.id)
        try:
            v = app.view(run)
            v["busy"] = studio.workers[run.id].busy if run.id in studio.workers else False
            await sock.send_json({"type": "view", "view": v})

            async def pump() -> None:
                while True:
                    await sock.send_json(await q.get())

            async def listen() -> None:
                while True:
                    msg = await sock.receive_json()
                    try:
                        out = await asyncio.to_thread(studio.execute, run, msg.get("op", ""),
                                                      msg.get("args") or {}, msg.get("op_id"))
                        await sock.send_json({"type": "ack", "op_id": msg.get("op_id"),
                                              "result": out})
                    except HTTPException as e:
                        await sock.send_json({"type": "notice", "level": "error",
                                              "op_id": msg.get("op_id"), "text": e.detail})
                    if msg.get("op") != "cancel":
                        await sock.send_json({"type": "view", "view": app.view(run)})

            done, pending = await asyncio.wait(
                [asyncio.create_task(pump()), asyncio.create_task(listen())],
                return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                exc = t.exception()
                if exc and not isinstance(exc, WebSocketDisconnect):
                    raise exc
        except WebSocketDisconnect:
            pass
        finally:
            studio.hub.unsubscribe(run.id, q)

    return api


def serve(cfg: Config, workdir: Path, host: str = "127.0.0.1", port: int = 8765,
          open_browser: bool = True) -> int:
    import uvicorn

    studio = Studio(cfg, workdir, token=os.environ.get("C2A_STUDIO_TOKEN") or None)
    url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}/" \
          f"?token={studio.token}"
    print(f"c2a Studio {__version__}\n  open: {url}\n  runs: {workdir.resolve()}")
    if host == "0.0.0.0":
        print("  listening on all interfaces: open it from another machine with this "
              "machine's address and the same token")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(studio), host=host, port=port, log_level="warning")
    return 0
