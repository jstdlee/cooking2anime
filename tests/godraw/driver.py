"""Drive a real GoDraw (built with `-tags e2e`) over MyGo's automation socket.

Protocol: newline-delimited JSON over a Unix socket named by MYGO_AUTOMATION
(github.com/egoist/mygo/plugins/automation).
"""

from __future__ import annotations

import base64
import io
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image


class GoDraw:
    def __init__(self, exe: Path, board: Path, sock: Path) -> None:
        self.sock = sock
        sock.unlink(missing_ok=True)
        env = {**os.environ, "MYGO_AUTOMATION": str(sock)}
        self.proc = subprocess.Popen([str(exe), str(board)], env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self._id = 0
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                wins = self.call("windows")["windows"]
                if wins:
                    self.window = int(wins[0]["id"])
                    self.title = str(wins[0]["title"])
                    break
            except OSError:
                pass
            time.sleep(0.2)
        else:
            log = self.close()
            raise RuntimeError(f"GoDraw did not open a window; its output:\n{log[-2000:]}")
        self.idle()

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self._id += 1
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(30)
            s.connect(str(self.sock))
            s.sendall((json.dumps({"id": self._id, "method": method,
                                   "params": params or {}}) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(1 << 20)
                if not chunk:
                    break
                buf += chunk
        msg = json.loads(buf)
        if "error" in msg:
            raise RuntimeError(f"{method}: {msg['error']}")
        return msg["result"]

    def idle(self, timeout_ms: int = 10_000) -> None:
        self.call("idle", {"window": self.window, "timeout": timeout_ms})

    def capture(self) -> Image.Image:
        r = self.call("capture", {"window": self.window})
        return Image.open(io.BytesIO(base64.b64decode(r["png"]))).convert("RGB")

    def drag(self, a: tuple[float, float], b: tuple[float, float], steps: int = 12) -> None:
        events: list[dict[str, Any]] = [{"type": "pointerMove", "x": a[0], "y": a[1]},
                                        {"type": "pointerDown", "x": a[0], "y": a[1]}]
        for i in range(1, steps + 1):
            t = i / steps
            events.append({"type": "pointerMove", "x": a[0] + (b[0] - a[0]) * t,
                           "y": a[1] + (b[1] - a[1]) * t})
        events.append({"type": "pointerUp", "x": b[0], "y": b[1]})
        self.call("input", {"window": self.window, "events": events})
        self.idle()

    def key(self, key: str) -> None:
        self.call("input", {"window": self.window, "events": [{"type": "key", "key": key}]})
        self.idle()

    def close(self) -> str:
        self.proc.terminate()
        try:
            out, _ = self.proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            out, _ = self.proc.communicate()
        return out.decode(errors="replace") if out else ""


def find_solid(img: Image.Image, rgb: tuple[int, int, int], tol: int = 12,
               min_run: int = 30) -> tuple[int, int, int, int]:
    """Bounding box (x0, y0, x1, y1) of the solid region of one colour.

    Only rows/columns with at least `min_run` matching pixels count, which skips thin
    strokes and text of the same colour.
    """
    a = np.asarray(img, dtype=np.int16)
    mask = np.all(np.abs(a - np.array(rgb)) <= tol, axis=-1)
    rows = np.where(mask.sum(axis=1) >= min_run)[0]
    cols = np.where(mask.sum(axis=0) >= min_run)[0]
    if not len(rows) or not len(cols):
        raise LookupError(f"no solid region of colour {rgb}")
    return int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1


class Camera:
    """Board → window coordinates, derived from a shape of known board size/position."""

    def __init__(self, board_xy: tuple[float, float], board_w: float,
                 screen_box: tuple[int, int, int, int]) -> None:
        x0, y0, x1, _ = screen_box
        self.zoom = (x1 - x0) / board_w
        self.ox = x0 - board_xy[0] * self.zoom
        self.oy = y0 - board_xy[1] * self.zoom

    def to_screen(self, x: float, y: float) -> tuple[float, float]:
        return self.ox + x * self.zoom, self.oy + y * self.zoom
