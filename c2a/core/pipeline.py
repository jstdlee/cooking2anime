"""Stage graph runner: ordered stages, one human gate, resume via the unit cache."""

from __future__ import annotations

import contextlib
import fcntl
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Literal

from c2a.config import Config
from c2a.core.context import Context, load_preset
from c2a.core.errors import C2AError, StageError
from c2a.core.manifest import Run, State
from c2a.providers.base import Stack
from c2a.providers.registry import build_stack
from c2a.stages.analyze import analyze
from c2a.stages.audio import audio
from c2a.stages.describe import describe
from c2a.stages.export import export
from c2a.stages.generate import generate
from c2a.stages.post import post
from c2a.stages.preview import preview
from c2a.stages.restyle import restyle

StageFn = Callable[[Context], None]
GATE: Literal["gate"] = "gate"

PIPELINE: list[tuple[State, StageFn] | Literal["gate"]] = [
    ("analyzing", analyze),
    ("describing", describe),
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


def make_context(run: Run, stack: Stack | None = None) -> Context:
    cfg = Config.model_validate(run.read()["config"])
    return Context(run=run, cfg=cfg, stack=stack or build_stack(cfg),
                   preset=load_preset(cfg.style.preset))


def _project(ctx: Context) -> None:
    from c2a.board.sync import project_board

    try:
        project_board(ctx)
    except Exception as e:  # the board is a view; it must never break a run
        ctx.say(f"board: projection skipped ({e})")


def run_pipeline(run: Run, stack: Stack | None = None,
                 project: bool = True) -> Outcome:
    with runner_lock(run):
        ctx = make_context(run, stack)
        try:
            for step in PIPELINE:
                if isinstance(step, str):
                    if run.read().get("approval") is None:
                        run.set_state("awaiting_approval")
                        if project:
                            _project(ctx)
                        ctx.say("waiting for style approval: drag ✓ onto a candidate in "
                                f"{run.path(ctx.cfg.canvas.board)} or run "
                                f"`c2a approve {run.root} --pick <sample:seed>`")
                        return "awaiting_approval"
                    continue
                state, fn = step
                run.set_state(state)
                fn(ctx)
                if project:
                    _project(ctx)
            run.set_state("done")
            if project:
                _project(ctx)
            return "done"
        except C2AError as e:
            run.set_state("failed", str(e))
            if project:
                _project(ctx)
            raise
        except Exception as e:
            run.set_state("failed", f"{type(e).__name__}: {e}")
            raise


def rerun_chunk(run: Run, chunk_id: str) -> list[str]:
    """Invalidate one chunk and everything downstream of it (post, audio, export)."""
    ctx = make_context(run, stack=_null_stack())
    ctx.timeline.chunk(chunk_id)  # KeyError if unknown
    with run.update() as m:
        reruns = m.setdefault("reruns", {})
        reruns[chunk_id] = int(reruns.get(chunk_id, 0)) + 1
    gone = run.invalidate(f"generate/{chunk_id}")
    for u in ("post", "audio", "export"):
        gone += run.invalidate(u)
    return gone


def _null_stack() -> Stack:
    from c2a.providers.fake import fake_stack

    return fake_stack()


def find_run(path: Path) -> Run:
    return Run.open(path)
