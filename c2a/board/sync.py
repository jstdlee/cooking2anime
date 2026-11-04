"""Board ↔ manifest sync with ownership rules and optimistic writes (design §6).

- Engine-owned records are replaced by key on every projection.
- User-input records (notes, stickers) are created when missing and otherwise only
  updated when the person has not edited them since the engine last wrote them.
- Every other record belongs to the person and is kept unchanged.
- Writes go to a temp file + rename, and only if the file did not change since it was read.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from c2a.board import tldr
from c2a.board.projector import Projection, project
from c2a.board.tldr import OWNER_ENGINE, OWNER_INPUT, Record
from c2a.core.context import Context
from c2a.core.errors import BoardError
from c2a.core.manifest import Run, atomic_write_bytes

MAX_WRITE_RETRIES = 5
NOTE_CENTER = 100.0


def _hash(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def read_board(path: Path) -> tuple[list[Record], str | None]:
    if not path.exists():
        return [], None
    raw = path.read_bytes()
    try:
        doc = json.loads(raw)
        recs = doc["records"]
        if not isinstance(recs, list):
            raise TypeError("records is not a list")
    except (ValueError, KeyError, TypeError) as e:
        raise BoardError(f"{path}: not a readable .tldr file ({e})") from e
    return recs, _hash(raw)


def merge(existing: list[Record], proj: Projection,
          last_written: dict[str, str]) -> tuple[list[Record], dict[str, str]]:
    """Return (records, input texts the engine wrote). Pure function."""
    engine_by_id = {r["id"]: r for r in proj.engine}
    inputs_by_key = {tldr.key_of(r): r for r in proj.inputs}
    out: list[Record] = []
    seen_inputs: set[str] = set()
    written = dict(last_written)
    have_doc = False
    for r in existing:
        owner = tldr.owner_of(r)
        if r.get("id") == tldr.DOCUMENT_ID:
            have_doc = True
            out.append(r)
        elif owner == OWNER_ENGINE:
            continue  # replaced below (or removed if no longer projected)
        elif owner == OWNER_INPUT:
            k = tldr.key_of(r)
            seen_inputs.add(str(k))
            want = inputs_by_key.get(k)
            if want is not None and k in proj.input_text:
                current = tldr.plain_text(r.get("props", {}).get("richText"))
                untouched = current == last_written.get(str(k), current)
                if untouched and current != proj.input_text[str(k)]:
                    r = {**r, "props": {**r["props"],
                                        "richText": tldr.rich_text(proj.input_text[str(k)])}}
                    written[str(k)] = proj.input_text[str(k)]
                elif str(k) not in last_written:
                    written[str(k)] = current
            out.append(r)
        else:
            out.append(r)
    if not have_doc:
        out.insert(0, tldr.document())
    out.extend(engine_by_id.values())
    for k, r in inputs_by_key.items():
        if str(k) not in seen_inputs:
            out.append(r)
            if str(k) in proj.input_text:
                written[str(k)] = proj.input_text[str(k)]
    return _reparent_orphans(out), written


def _abs_xy(rec: Record, by_id: dict[str, Record]) -> tuple[float, float]:
    x, y = float(rec.get("x", 0)), float(rec.get("y", 0))
    parent = by_id.get(rec.get("parentId", ""))
    while parent is not None and parent.get("typeName") == "shape":
        x += float(parent.get("x", 0))
        y += float(parent.get("y", 0))
        parent = by_id.get(parent.get("parentId", ""))
    return x, y


def _page_of(rec: Record, by_id: dict[str, Record]) -> str | None:
    cur: Record | None = rec
    while cur is not None and cur.get("typeName") == "shape":
        pid = cur.get("parentId", "")
        if str(pid).startswith("page:"):
            return str(pid)
        cur = by_id.get(pid)
    return None


def _reparent_orphans(recs: list[Record]) -> list[Record]:
    """A user's shape inside an engine frame that no longer exists moves to the page."""
    ids = {r["id"] for r in recs}
    pages = [r["id"] for r in recs if r.get("typeName") == "page"]
    out = []
    for r in recs:
        if r.get("typeName") == "shape" and r.get("parentId") not in ids:
            r = {**r, "parentId": pages[0] if pages else r.get("parentId")}
        out.append(r)
    return out


def write_board(path: Path, records: list[Record], expected_hash: str | None) -> bool:
    """Atomic write if the file still has `expected_hash`. Returns False on conflict."""
    current = _hash(path.read_bytes()) if path.exists() else None
    if current != expected_hash:
        return False
    data = (json.dumps(tldr.file_doc(records), ensure_ascii=False, indent=1) + "\n").encode()
    if current is not None and _hash(data) == current:
        return True  # nothing changed; don't touch the file (avoids app reloads)
    atomic_write_bytes(path, data)
    return True


def board_path(ctx: Context) -> Path:
    return ctx.run.path(ctx.cfg.canvas.board)


def project_board(ctx: Context) -> Path:
    path = board_path(ctx)
    budget = ctx.cfg.canvas.board_budget_mb * 1024 * 1024
    max_px = ctx.cfg.canvas.thumb_max_px
    proj = project(ctx, max_px)
    while sum(len(json.dumps(r)) for r in proj.engine) > budget and max_px > 96:
        max_px //= 2
        proj = project(ctx, max_px)
    for _ in range(MAX_WRITE_RETRIES):
        existing, h = read_board(path)
        last = ctx.run.read().get("board", {}).get("input_text", {})
        records, written = merge(existing, proj, last)
        if write_board(path, records, h):
            with ctx.run.update() as m:
                m.setdefault("board", {})["input_text"] = written
            return path
        time.sleep(0.05)
    raise BoardError(f"{path} kept changing while writing; giving up after "
                     f"{MAX_WRITE_RETRIES} tries")


# ------------------------------------------------------------------ reading inputs
@dataclass
class BoardInputs:
    approve_in: str | None = None  # candidate id the ✓ sticker is in
    rerun_in: str | None = None  # chunk id the ↻ sticker is in
    texts: dict[str, str] = field(default_factory=dict)  # input key → current text
    notices: list[str] = field(default_factory=list)


def _container(sticker: Record, by_id: dict[str, Record], attr: str) -> list[str]:
    """Values of meta.c2a[attr] for engine frames that hold the sticker.

    A frame holds it if it is an ancestor (the app re-parented it on drop) or if the
    sticker's centre lies inside the frame on the same page (apps that don't re-parent).
    """
    found: list[str] = []
    cur = by_id.get(sticker.get("parentId", ""))
    while cur is not None and cur.get("typeName") == "shape":
        v = (cur.get("meta", {}).get("c2a") or {}).get(attr)
        if v is not None:
            return [str(v)]
        cur = by_id.get(cur.get("parentId", ""))
    sx, sy = _abs_xy(sticker, by_id)
    scale = float(sticker.get("props", {}).get("scale", 1) or 1)
    cx, cy = sx + NOTE_CENTER * scale, sy + NOTE_CENTER * scale
    page = _page_of(sticker, by_id)
    for r in by_id.values():
        v = (r.get("meta", {}).get("c2a") or {}).get(attr) if r.get("type") == "frame" else None
        if v is None or _page_of(r, by_id) != page:
            continue
        fx, fy = _abs_xy(r, by_id)
        w, h = float(r["props"]["w"]), float(r["props"]["h"])
        if fx <= cx <= fx + w and fy <= cy <= fy + h:
            found.append(str(v))
    return found


def read_inputs(path: Path) -> BoardInputs:
    recs, _ = read_board(path)
    by_id = {r["id"]: r for r in recs if "id" in r}
    out = BoardInputs()
    for r in recs:
        if tldr.owner_of(r) != OWNER_INPUT:
            continue
        k = str(tldr.key_of(r))
        if k == "approve-sticker":
            hits = _container(r, by_id, "candidate")
            if len(hits) > 1:
                out.notices.append("✓ sticker overlaps several candidates; ignored")
            elif hits:
                out.approve_in = hits[0]
        elif k == "rerun-sticker":
            hits = _container(r, by_id, "chunk")
            if len(hits) > 1:
                out.notices.append("↻ sticker overlaps several chunks; ignored")
            elif hits:
                out.rerun_in = hits[0]
        elif r.get("type") == "note":
            out.texts[k] = tldr.plain_text(r.get("props", {}).get("richText")).strip()
    return out


def apply_inputs(run: Run, inputs: BoardInputs) -> list[str]:
    """Copy validated board inputs into the manifest. Returns human-readable changes.

    Stickers act on *transitions*: dropping ✓/↻ into a frame triggers once; moving it to
    another frame triggers again. Text inputs act when they differ from what the engine
    last wrote.
    """
    from c2a.core.pipeline import rerun_chunk
    from c2a.stages.preview import approve, clear_approval

    changes: list[str] = list(inputs.notices)
    m = run.read()
    bstate: dict[str, Any] = m.get("board", {})
    written: dict[str, str] = bstate.get("input_text", {})

    # style prompt edit → new preview, approval cleared
    sp = inputs.texts.get("style-prompt")
    if sp is not None and sp != written.get("style-prompt", "").strip():
        with run.update() as mm:
            mm["overrides"]["style_prompt"] = sp or None
            mm.setdefault("board", {}).setdefault("input_text", {})["style-prompt"] = sp
        if run.read().get("approval"):
            clear_approval(run)
        changes.append("style prompt edited on the board → new preview, approval cleared")

    # chunk prompt edits → per-chunk overrides (generate cache keys change by themselves)
    for k, text in inputs.texts.items():
        if not k.startswith("prompt-"):
            continue
        cid = k.removeprefix("prompt-")
        if text != written.get(k, "").strip():
            with run.update() as mm:
                if text:
                    mm["overrides"].setdefault("chunks", {})[cid] = text
                else:
                    mm["overrides"].get("chunks", {}).pop(cid, None)
                mm.setdefault("board", {}).setdefault("input_text", {})[k] = text
            changes.append(f"chunk {cid}: motion prompt edited on the board")

    # ✓ sticker transition → approval
    prev = bstate.get("approve_in")
    if inputs.approve_in != prev:
        with run.update() as mm:
            mm.setdefault("board", {})["approve_in"] = inputs.approve_in
        cur = (run.read().get("approval") or {}).get("candidate")
        if inputs.approve_in and inputs.approve_in != cur:
            approve(run, inputs.approve_in, via="board")
            changes.append(f"approved candidate {inputs.approve_in} on the board")

    # ↻ sticker transition → rerun
    prev_r = bstate.get("rerun_in")
    if inputs.rerun_in != prev_r:
        with run.update() as mm:
            mm.setdefault("board", {})["rerun_in"] = inputs.rerun_in
        if inputs.rerun_in:
            rerun_chunk(run, inputs.rerun_in)
            changes.append(f"chunk {inputs.rerun_in}: re-run requested on the board")
    return changes
