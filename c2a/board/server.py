"""Read-only local HTTP server for the board's ▶ open links (tldraw links must be http)."""

from __future__ import annotations

import functools
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ALLOWED = {".mp4", ".png", ".jpg", ".json", ".wav", ".html"}


class _Handler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if ".." in path or Path(path).suffix.lower() not in ALLOWED:
            self.send_error(404)
            return
        super().do_GET()

    def do_HEAD(self) -> None:
        self.do_GET()

    def list_directory(self, path: Any) -> None:
        self.send_error(404)
        return None

    def log_message(self, format: str, *args: Any) -> None:
        return


def serve(root: Path, port: int) -> ThreadingHTTPServer:
    """Serve `root` on 127.0.0.1:`port` (0 = any free port; see httpd.server_port)."""
    handler = functools.partial(_Handler, directory=str(root))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd
