import urllib.error
import urllib.request
from pathlib import Path

import pytest

from c2a.board.server import serve


@pytest.mark.enable_socket
def test_serves_only_media_and_never_lists(tmp_path: Path) -> None:
    (tmp_path / "generate").mkdir()
    (tmp_path / "generate" / "0.0.mp4").write_bytes(b"mp4data")
    (tmp_path / "manifest.json").write_text("{}")
    (tmp_path / "secret.py").write_text("x")
    httpd = serve(tmp_path, 0)
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        with urllib.request.urlopen(f"{base}/generate/0.0.mp4") as r:
            assert r.read() == b"mp4data"
        for bad in ("/secret.py", "/generate/", "/../etc/passwd.json"):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + bad)
            assert e.value.code == 404
    finally:
        httpd.shutdown()
