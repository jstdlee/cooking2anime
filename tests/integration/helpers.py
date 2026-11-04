from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from c2a.config import load_config
from c2a.core.manifest import Run
from c2a.core.pipeline import run_pipeline
from c2a.providers.base import Stack
from c2a.providers.fake import fake_stack


def new_run(workdir: Path, src: Path, **overrides: Any) -> Run:
    # a 2×2 sample grid keeps tests fast; the default (3×3) is covered by the e2e test
    ov: dict[str, Any] = {"run": {"stack": "fake"},
                          "style": {"preview": {"sample_keys": 2, "seeds_per_key": 2}}}
    for k, v in overrides.items():
        node = ov
        parts = k.split("__")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = v
    cfg = load_config(None, ov)
    return Run.create(workdir, src, cfg.model_dump(mode="json"))


def to_gate(workdir: Path, src: Path, stack: Stack | None = None, **overrides: Any) -> Run:
    run = new_run(workdir, src, **overrides)
    assert run_pipeline(run, stack or fake_stack()) == "awaiting_approval"
    return run


def clone(run: Run, dst_root: Path) -> Run:
    dst = dst_root / run.id
    shutil.copytree(run.root, dst, ignore=shutil.ignore_patterns(".*.lock"))
    return Run.open(dst)
