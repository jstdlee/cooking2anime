"""Stage graph runner (v2): ordered stages, one style gate, resume via the unit cache."""

from __future__ import annotations

import contextlib
import fcntl
from collections.abc import Callable, Iterator
from typing import Literal

from c2a.core.context import Context, Progress, effective_config
from c2a.core.errors import C2AError, StageError
from c2a.core.manifest import Run, State
from c2a.providers.base import Stack
from c2a.providers.registry import build_stack
from c2a.stages.audio import audio
from c2a.stages.export import export
from c2a.stages.generate import generate
from c2a.stages.ingest import ingest
from c2a.stages.keys import keys
from c2a.stages.post import post
from c2a.stages.preview import approval_is_current, preview
from c2a.stages.restyle import restyle
from c2a.stages.tag import tag

StageFn = Callable[[Context], None]
GATE: Literal["gate"] = "gate"

PIPELINE: list[tuple[State, StageFn] | Literal["gate"]] = [
    ("ingesting", ingest),
    ("finding_keys", keys),
    ("tagging", tag),
    ("previewing", preview),
    GATE,
    ("restyling", restyle),
    ("generating", generate),
    ("post", post),
    ("audio", audio),
    ("exporting", export),
]

Outcome = Literal["awaiting_approval", "done"]


@contextlib.contextmanager
def runner_lock(run: Run) -> Iterator[None]:
    path = run.path(".runner.lock")
    with open(path, "a+") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StageError(f"another c2a process is already running {run.id}") from None
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def make_context(run: Run, stack: Stack | None = None, progress: Progress | None = None,
                 cancelled: Callable[[], bool] | None = None) -> Context:
    cfg = effective_config(run)
    ctx = Context(run=run, cfg=cfg, stack=stack or build_stack(cfg), progress=progress)
    if cancelled is not None:
        ctx.cancelled = cancelled
    return ctx


def run_pipeline(run: Run, stack: Stack | None = None, progress: Progress | None = None,
                 cancelled: Callable[[], bool] | None = None) -> Outcome:
    with runner_lock(run):
        ctx = make_context(run, stack, progress, cancelled)
        try:
            for step in PIPELINE:
                if isinstance(step, str):
                    if not approval_is_current(run):
                        run.set_state("awaiting_approval")
                        ctx.say("waiting for style approval: open the Studio, or run "
                                f"`c2a approve {run.root} --pick <sample:variation>`")
                        return "awaiting_approval"
                    continue
                state, fn = step
                run.set_state(state)
                if progress:
                    progress(state, "", 0.0)
                fn(ctx)
            run.set_state("done")
            return "done"
        except C2AError as e:
            run.set_state("failed", str(e))
            raise
        except Exception as e:
            run.set_state("failed", f"{type(e).__name__}: {e}")
            raise


def rerun_clip(run: Run, clip_id: str) -> list[str]:
    """Re-generate one clip with a new seed, and everything after it (post, audio, export)."""
    from c2a.providers.fake import fake_stack

    ctx = make_context(run, stack=fake_stack())
    ctx.timeline().segment(clip_id)  # KeyError if unknown
    with run.update() as m:
        reruns = m.setdefault("reruns", {})
        reruns[clip_id] = int(reruns.get(clip_id, 0)) + 1
    gone = run.invalidate(f"generate/{clip_id}")
    for u in ("post", "audio", "export"):
        gone += run.invalidate(u)
    return gone
