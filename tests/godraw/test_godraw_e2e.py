"""Real GoDraw end-to-end (TESTING.md §3 Board, "Real GoDraw").

Needs a GoDraw binary built with `-tags e2e` (C2A_GODRAW_E2E=path) and a display; in CI
the job runs pytest under `xvfb-run`. Skipped otherwise.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

import pytest

from c2a.board import tldr
from c2a.board.sync import apply_inputs, project_board, read_board, read_inputs
from c2a.core.manifest import Run
from c2a.core.pipeline import make_context
from c2a.providers.fake import fake_stack
from tests.conftest import validate_tldr
from tests.godraw.driver import Camera, GoDraw, find_solid
from tests.integration.helpers import to_gate

pytestmark = pytest.mark.godraw
EXE = os.environ.get("C2A_GODRAW_E2E")
STICKER_GREEN = (111, 200, 150)  # tldraw "green" note fill as GoDraw draws it


def wait_for(cond: object, timeout: float = 15.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():  # type: ignore[operator]
            return True
        time.sleep(0.25)
    return False


@pytest.fixture
def godraw_run(fixtures: dict[str, Path], tmp_path: Path) -> tuple[Run, GoDraw]:
    if not EXE or not Path(EXE).exists():
        pytest.skip("set C2A_GODRAW_E2E to a GoDraw binary built with -tags e2e")
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        pytest.skip("no display (run under xvfb-run)")
    run = to_gate(tmp_path / "runs", fixtures["cut"])
    sock_dir = Path(tempfile.mkdtemp(prefix="c2a-gd-"))  # Unix socket paths max ~104 bytes
    app = GoDraw(Path(EXE), run.path("board.tldr"), sock_dir / "s")
    yield run, app  # type: ignore[misc]
    log = app.close()
    (tmp_path / "godraw.log").write_text(log)


def engine_records(path: Path) -> dict[str, dict[str, object]]:
    return {r["id"]: r for r in read_board(path)[0] if tldr.owner_of(r) == "engine"}


def test_drag_sticker_in_godraw_approves(godraw_run: tuple[Run, GoDraw],
                                         tmp_path: Path) -> None:
    run, app = godraw_run
    board = run.path("board.tldr")
    assert board.name in app.title

    # 1. the board renders: find the ✓ sticker on screen → board↔screen camera
    shot = app.capture()
    shot.save(tmp_path / "1-opened.png")
    recs = {tldr.key_of(r): r for r in read_board(board)[0]}
    sticker = recs["approve-sticker"]
    cam = Camera((sticker["x"], sticker["y"]), tldr.NOTE_SIZE,
                 find_solid(shot, STICKER_GREEN))

    # 2. drag it into candidate 2:1 the way a person would
    target = recs["gate-cand-2:1"]
    tx = target["x"] + target["props"]["w"] / 2
    ty = target["y"] + target["props"]["h"] / 2
    sx, sy = cam.to_screen(sticker["x"] + 100, sticker["y"] + 100)
    before = board.stat().st_mtime_ns
    app.drag((sx, sy), cam.to_screen(tx, ty))
    app.capture().save(tmp_path / "2-dragged.png")

    # 3. GoDraw autosaves; the engine reads the approval from the saved file
    assert wait_for(lambda: board.stat().st_mtime_ns != before), "GoDraw did not save"
    validate_tldr(board)  # GoDraw's own save is still valid tldraw
    inputs = read_inputs(board)
    assert inputs.approve_in == "2:1", inputs
    assert apply_inputs(run, inputs) == ["approved candidate 2:1 on the board"]

    # 4. GoDraw kept every engine record and its metadata through its save
    saved = engine_records(board)
    ctx = make_context(run, fake_stack())
    from c2a.board.projector import project
    expected = {r["id"] for r in project(ctx).engine}
    assert expected <= saved.keys()

    # 5. the engine writes the new state; GoDraw picks it up and does not overwrite it
    project_board(ctx)
    status = recs["gate-status"]["id"]
    want = "approved: candidate 2:1 (via board)"
    assert tldr.plain_text(engine_records(board)[status]["props"]["richText"]) == want
    time.sleep(4)  # longer than GoDraw's autosave delay
    app.idle()
    after = engine_records(board)
    assert tldr.plain_text(after[status]["props"]["richText"]) == want
    app.capture().save(tmp_path / "3-engine-update.png")
    (tmp_path / "board-after.json").write_text(json.dumps(read_board(board)[0])[:2000])
