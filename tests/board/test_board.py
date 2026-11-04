"""Board contract (TESTING.md §3 Board): validity, ownership, stickers, notes, conflicts."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from c2a.board import tldr
from c2a.board.sync import (
    apply_inputs,
    board_path,
    project_board,
    read_board,
    read_inputs,
    write_board,
)
from c2a.core.manifest import Run
from c2a.core.pipeline import make_context, run_pipeline
from c2a.providers.fake import fake_stack
from tests.conftest import validate_tldr
from tests.integration.helpers import clone, to_gate


@pytest.fixture(scope="module")
def gated(fixtures: dict[str, Path], tmp_path_factory: pytest.TempPathFactory) -> Run:
    return to_gate(tmp_path_factory.mktemp("runs"), fixtures["cut"])


def board(run: Run) -> Path:
    return run.path("board.tldr")


def records(run: Run) -> list[dict[str, Any]]:
    return read_board(board(run))[0]


def by_key(run: Run, key: str) -> dict[str, Any]:
    return next(r for r in records(run) if tldr.key_of(r) == key)


def edit(run: Run, fn: Any) -> None:
    """Simulate the person editing the board in GoDraw (in-place JSON edit + save)."""
    doc = json.loads(board(run).read_text())
    fn(doc["records"])
    board(run).write_text(json.dumps(doc))


def move_into(records_: list[dict[str, Any]], key: str, frame_key: str,
              reparent: bool) -> None:
    sticker = next(r for r in records_ if tldr.key_of(r) == key)
    frame = next(r for r in records_ if tldr.key_of(r) == frame_key)
    if reparent:  # what tldraw does on drop
        sticker["parentId"], sticker["x"], sticker["y"] = frame["id"], 5.0, 5.0
    else:  # an app that keeps page coordinates
        sticker["x"], sticker["y"] = frame["x"] + 10, frame["y"] - 60


def test_board_is_valid_tldraw(gated: Run) -> None:
    validate_tldr(board(gated))
    pages = [r["name"] for r in records(gated) if r["typeName"] == "page"]
    assert pages == ["1 · Style gate", "2 · Shots", "3 · Report"]


@pytest.mark.parametrize("reparent", [True, False])
def test_approve_by_dragging_sticker(gated: Run, tmp_path: Path, reparent: bool) -> None:
    run = clone(gated, tmp_path)
    edit(run, lambda rs: move_into(rs, "approve-sticker", "gate-cand-1:2", reparent))
    changes = apply_inputs(run, read_inputs(board(run)))
    assert changes == ["approved candidate 1:2 on the board"]
    assert run.read()["approval"]["via"] == "board"
    # same position again → no new transition
    assert apply_inputs(run, read_inputs(board(run))) == []


def test_sticker_outside_or_ambiguous_does_nothing(gated: Run, tmp_path: Path) -> None:
    run = clone(gated, tmp_path)
    assert apply_inputs(run, read_inputs(board(run))) == []

    def overlap(rs: list[dict[str, Any]]) -> None:
        a = next(r for r in rs if tldr.key_of(r) == "gate-cand-0:0")
        b = next(r for r in rs if tldr.key_of(r) == "gate-cand-0:1")
        b["x"] = a["x"]  # two candidate frames on top of each other
        s = next(r for r in rs if tldr.key_of(r) == "approve-sticker")
        s["x"], s["y"] = a["x"] + 20, a["y"] + 20

    edit(run, overlap)
    changes = apply_inputs(run, read_inputs(board(run)))
    assert changes == ["✓ sticker overlaps several candidates; ignored"]
    assert run.read()["approval"] is None


def test_style_prompt_edit_clears_approval_and_reruns_preview(gated: Run,
                                                              tmp_path: Path) -> None:
    run = clone(gated, tmp_path)
    from c2a.stages.preview import approve
    approve(run, "0:0")

    def change(rs: list[dict[str, Any]]) -> None:
        n = next(r for r in rs if tldr.key_of(r) == "style-prompt")
        n["props"]["richText"] = tldr.rich_text("watercolor ghibli kitchen")

    edit(run, change)
    changes = apply_inputs(run, read_inputs(board(run)))
    assert any("style prompt edited" in c for c in changes)
    assert run.read()["approval"] is None
    assert run_pipeline(run, fake_stack()) == "awaiting_approval"
    cands = json.loads(run.path("preview", "candidates.json").read_text())
    assert cands["prompt"].startswith("watercolor ghibli kitchen")
    # the engine keeps the person's text in the note
    assert tldr.plain_text(by_key(run, "style-prompt")["props"]["richText"]) == \
        "watercolor ghibli kitchen"


def test_chunk_note_edit_becomes_override_and_survives_projection(gated: Run,
                                                                  tmp_path: Path) -> None:
    run = clone(gated, tmp_path)
    from c2a.stages.preview import approve
    approve(run, "0:0")
    assert run_pipeline(run, fake_stack()) == "done"
    old_t = run.read()["units"]["generate/0.0"]["t"]

    def change(rs: list[dict[str, Any]]) -> None:
        n = next(r for r in rs if tldr.key_of(r) == "prompt-0.0")
        n["props"]["richText"] = tldr.rich_text("both hands toss rice in the wok")

    edit(run, change)
    assert apply_inputs(run, read_inputs(board(run))) == \
        ["chunk 0.0: motion prompt edited on the board"]
    assert run.read()["overrides"]["chunks"]["0.0"] == "both hands toss rice in the wok"
    assert run_pipeline(run, fake_stack()) == "done"
    assert run.read()["units"]["generate/0.0"]["t"] > old_t  # regenerated
    line = next(json.loads(x) for x in run.path("generate", "prompts.jsonl").read_text()
                .splitlines() if json.loads(x)["chunk"] == "0.0")
    assert "both hands toss rice in the wok" in line["positive"]
    assert tldr.plain_text(by_key(run, "prompt-0.0")["props"]["richText"]) == \
        "both hands toss rice in the wok"
    validate_tldr(board(run))


def test_rerun_sticker(gated: Run, tmp_path: Path) -> None:
    run = clone(gated, tmp_path)
    from c2a.stages.preview import approve
    approve(run, "0:0")
    assert run_pipeline(run, fake_stack()) == "done"
    edit(run, lambda rs: move_into(rs, "rerun-sticker", "chunk-1.0", True))
    assert apply_inputs(run, read_inputs(board(run))) == \
        ["chunk 1.0: re-run requested on the board"]
    assert run.read()["units"].get("generate/1.0") is None
    assert run.read()["units"].get("generate/0.0") is not None


def test_optimistic_write_refuses_stale_hash(gated: Run, tmp_path: Path) -> None:
    run = clone(gated, tmp_path)
    recs, h = read_board(board(run))
    board(run).write_text(board(run).read_text() + " ")  # someone else saved
    assert write_board(board(run), recs, h) is False
    assert write_board(board(run), recs, read_board(board(run))[1]) is True


def test_unchanged_projection_does_not_touch_file(gated: Run, tmp_path: Path) -> None:
    run = clone(gated, tmp_path)
    ctx = make_context(run, fake_stack())
    project_board(ctx)
    m1 = board(run).stat().st_mtime_ns
    project_board(ctx)
    assert board(run).stat().st_mtime_ns == m1


def test_orphaned_user_shape_moves_to_page(gated: Run, tmp_path: Path) -> None:
    run = clone(gated, tmp_path)

    def add(rs: list[dict[str, Any]]) -> None:
        rs.append({**tldr.note("mine", "shape:c2a-gone-frame", 0, 0, "hi", 0, owner="me"),
                   "meta": {}})

    edit(run, add)
    project_board(make_context(run, fake_stack()))
    mine = next(r for r in records(run) if r["id"] == "shape:c2a-mine")
    assert mine["parentId"].startswith("page:")
    validate_tldr(board(run))


def test_rebuild_reproduces_engine_records(gated: Run, tmp_path: Path) -> None:
    run = clone(gated, tmp_path)
    ctx = make_context(run, fake_stack())
    before = {r["id"]: r for r in records(run) if tldr.owner_of(r) == "engine"}
    board_path(ctx).unlink()
    project_board(ctx)
    after = {r["id"]: r for r in records(run) if tldr.owner_of(r) == "engine"}
    assert before == after


# ---- property: the person's own records survive any number of engine writes ----
_shape_kinds = st.sampled_from(["geo", "note", "text"])


@st.composite
def user_records(draw: st.DrawFn) -> list[dict[str, Any]]:
    n = draw(st.integers(1, 12))
    out = []
    for i in range(n):
        kind = draw(_shape_kinds)
        x, y = draw(st.floats(-5000, 5000)), draw(st.floats(-5000, 5000))
        label = draw(st.text(max_size=40))
        page = draw(st.sampled_from(["page:c2a-page-gate", "page:c2a-page-shots"]))
        if kind == "note":
            r = tldr.note(f"u{i}", page, x, y, label, i, owner="me")
        elif kind == "text":
            r = tldr.text(f"u{i}", page, x, y, 200, label, i)
        else:
            r = tldr.geo_link(f"u{i}", page, x, y, 100, 50, label, "", i)
        r["id"] = f"shape:user{i}"
        r["meta"] = {"mine": i}
        out.append(r)
    return out


@settings(max_examples=25, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(user_records())
def test_user_records_survive_engine_writes(gated: Run, tmp_path_factory: Any,
                                            users: list[dict[str, Any]]) -> None:
    run = clone(gated, tmp_path_factory.mktemp("prop"))
    edit(run, lambda rs: rs.extend(json.loads(json.dumps(users))))
    ctx = make_context(run, fake_stack())
    rng = random.Random(0)
    for _ in range(4):
        with run.update() as m:  # vary engine state between writes
            m["state"] = rng.choice(["awaiting_approval", "generating", "done"])
        project_board(ctx)
    kept = {r["id"]: r for r in records(run)}
    for u in json.loads(json.dumps(users)):
        assert kept[u["id"]] == u
