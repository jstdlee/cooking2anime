"""Run folder + manifest: the only source of truth for a run.

Every write is atomic (temp file in the same folder + os.replace) and serialized with an
advisory file lock, so a crash leaves either the old or the new manifest, never a mix.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import secrets
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from c2a.core.errors import StageError

State = Literal[
    "created", "ingesting", "finding_keys", "tagging", "previewing", "awaiting_approval",
    "restyling", "generating", "post", "audio", "exporting", "done", "failed",
]
STATES: tuple[State, ...] = (
    "created", "ingesting", "finding_keys", "tagging", "previewing", "awaiting_approval",
    "restyling", "generating", "post", "audio", "exporting", "done", "failed",
)

MANIFEST = "manifest.json"


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_bytes(path, (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode())


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


_digest_cache: dict[tuple[str, int, int], str] = {}


def file_digest(path: Path) -> str:
    st = path.stat()
    k = (str(path.resolve()), st.st_size, st.st_mtime_ns)
    if k not in _digest_cache:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        _digest_cache[k] = h.hexdigest()
    return _digest_cache[k]


def cache_key(stage: str, version: int, params: Any, inputs: list[Path]) -> str:
    payload = {
        "stage": stage,
        "version": version,
        "params": params,
        "inputs": [file_digest(p) for p in inputs],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


@dataclass
class Run:
    """A run folder. All manifest access goes through `read()` / `update()`."""

    root: Path

    @property
    def id(self) -> str:
        return self.root.name

    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST

    def path(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    @classmethod
    def create(cls, workdir: Path, source: Path, config: dict[str, Any]) -> Run:
        run = cls(workdir / new_run_id())
        run.root.mkdir(parents=True, exist_ok=False)
        atomic_write_json(run.manifest_path, {
            "id": run.id,
            "version": 1,
            "created": time.time(),
            "source": str(source.resolve()),
            "state": "created",
            "config": config,
            "units": {},
            "approval": None,
            "settings": {},
            "keys_user": {"checked": {}, "added": [], "tags": {}, "motion": {}},
            "reruns": {},
            "events": [],
        })
        return run

    @classmethod
    def open(cls, path: Path) -> Run:
        run = cls(path)
        if not run.manifest_path.exists():
            raise StageError(f"not a run folder (no manifest): {path}")
        return run

    def read(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.manifest_path.read_text())
        return data

    @contextlib.contextmanager
    def update(self) -> Iterator[dict[str, Any]]:
        with file_lock(self.root / ".manifest.lock"):
            data = self.read()
            yield data
            atomic_write_json(self.manifest_path, data)

    def set_state(self, state: State, note: str | None = None) -> None:
        if state not in STATES:
            raise ValueError(state)
        with self.update() as m:
            m["state"] = state
            m["events"].append({"t": time.time(), "state": state, "note": note})

    @property
    def state(self) -> State:
        s: State = self.read()["state"]
        return s

    # --- units of work -------------------------------------------------------------
    def unit(self, name: str) -> dict[str, Any] | None:
        u: dict[str, Any] | None = self.read()["units"].get(name)
        return u

    def unit_is_fresh(self, name: str, key: str) -> bool:
        u = self.unit(name)
        if not u or u.get("key") != key or u.get("status") not in ("done", "flagged"):
            return False
        return all(self.path(p).exists() for p in u.get("outputs", {}).values())

    def record_unit(self, name: str, key: str, status: str, outputs: dict[str, str],
                    metrics: dict[str, Any] | None = None, attempts: int = 1) -> None:
        with self.update() as m:
            m["units"][name] = {
                "key": key, "status": status, "outputs": outputs,
                "metrics": metrics or {}, "attempts": attempts, "t": time.time(),
            }

    def invalidate(self, name: str, *, prefix: bool = False) -> list[str]:
        with self.update() as m:
            gone = [k for k in m["units"]
                    if (k.startswith(name) if prefix else k == name)]
            for k in gone:
                del m["units"][k]
        return gone
