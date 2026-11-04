import json
import os
from pathlib import Path

import pytest

from c2a.core.manifest import Run, atomic_write_json, cache_key


def test_atomic_write_keeps_old_file_on_crash(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "m.json"
    atomic_write_json(p, {"v": 1})

    def boom(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write_json(p, {"v": 2})
    assert json.loads(p.read_text()) == {"v": 1}
    assert [x.name for x in tmp_path.iterdir()] == ["m.json"]  # no temp files left


def test_cache_key_depends_on_params_and_content(tmp_path: Path) -> None:
    f = tmp_path / "a.bin"
    f.write_bytes(b"x")
    k1 = cache_key("s", 1, {"p": 1}, [f])
    assert k1 == cache_key("s", 1, {"p": 1}, [f])
    assert k1 != cache_key("s", 1, {"p": 2}, [f])
    assert k1 != cache_key("s", 2, {"p": 1}, [f])
    f.write_bytes(b"y")
    assert k1 != cache_key("s", 1, {"p": 1}, [f])


def test_units_freshness_and_exact_invalidation(tmp_path: Path) -> None:
    src = tmp_path / "in.mp4"
    src.write_bytes(b"v")
    run = Run.create(tmp_path / "runs", src, {})
    (run.root / "o.txt").write_text("o")
    for name in ("generate/1.1", "generate/1.10", "post"):
        run.record_unit(name, "k", "done", {"o": "o.txt"})
    assert run.unit_is_fresh("generate/1.1", "k")
    assert not run.unit_is_fresh("generate/1.1", "other")
    assert run.invalidate("generate/1.1") == ["generate/1.1"]
    assert run.unit("generate/1.10") is not None
    assert sorted(run.invalidate("generate/", prefix=True)) == ["generate/1.10"]
    (run.root / "o.txt").unlink()
    assert not run.unit_is_fresh("post", "k")  # outputs must exist


def test_state_transitions_are_logged(tmp_path: Path) -> None:
    src = tmp_path / "in.mp4"
    src.write_bytes(b"v")
    run = Run.create(tmp_path / "runs", src, {})
    run.set_state("analyzing")
    run.set_state("failed", "boom")
    m = run.read()
    assert m["state"] == "failed"
    assert [e["state"] for e in m["events"]] == ["analyzing", "failed"]
    with pytest.raises(ValueError):
        run.set_state("nope")  # type: ignore[arg-type]
