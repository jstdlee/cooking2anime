"""Key image state: the stage's candidates + VLM tags + the person's choices (manifest).

manifest["keys_user"] = {
    "checked": {"<start>": bool},   # explicit check/uncheck (absent = automatic)
    "added":   [<frame>, ...],      # key images the person added
    "tags":    {"<start>": {...}},  # tag edits, validated against the tag schema
    "motion":  {"<start>": "..."},  # free motion sentence overriding the tags' sentence
}
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from c2a.core.errors import GateError
from c2a.core.manifest import Run
from c2a.prompts.tags import KeyTags, apply_edits, validate_tags


@dataclass(frozen=True)
class KeyState:
    start: int
    image: int
    score: float
    reasons: tuple[str, ...]
    locked: bool
    user_added: bool
    tags: KeyTags | None
    auto_checked: bool
    checked: bool
    motion_override: str | None

    @property
    def id(self) -> str:
        return f"k{self.start:06d}"


def _user(m: dict[str, Any]) -> dict[str, Any]:
    u: dict[str, Any] = m.setdefault("keys_user", {})
    for k, default in (("checked", {}), ("added", []), ("tags", {}), ("motion", {})):
        u.setdefault(k, default)
    return u


def candidates(run: Run) -> list[dict[str, Any]]:
    p = run.path("keys", "candidates.json")
    if not p.exists():
        return []
    d: list[dict[str, Any]] = json.loads(p.read_text())["candidates"]
    return d


def raw_tags(run: Run, start: int) -> KeyTags | None:
    p = run.path("tag", f"k{start:06d}.json")
    return validate_tags(json.loads(p.read_text())) if p.exists() else None


def key_states(run: Run, auto_check_min: float) -> list[KeyState]:
    m = run.read()
    u = _user(m)
    out: list[KeyState] = []
    for c in candidates(run):
        start = int(c["start"])
        tags = raw_tags(run, start)
        edits = u["tags"].get(str(start))
        if tags is not None and edits:
            tags = apply_edits(tags, edits)
        auto = bool(c["locked"] or c.get("user_added") or (
            tags is not None and tags.suggest and tags.importance >= auto_check_min))
        explicit = u["checked"].get(str(start))
        checked = True if c["locked"] else (auto if explicit is None else bool(explicit))
        out.append(KeyState(start, int(c["image"]), float(c["score"]), tuple(c["reasons"]),
                            bool(c["locked"]), bool(c.get("user_added")), tags, auto, checked,
                            u["motion"].get(str(start))))
    return out


def _find(run: Run, start: int) -> dict[str, Any]:
    for c in candidates(run):
        if int(c["start"]) == start:
            return c
    raise GateError(f"no key image at frame {start}")


def set_checked(run: Run, start: int, checked: bool) -> None:
    c = _find(run, start)
    if c["locked"] and not checked:
        raise GateError(f"key image {start} starts a shot and cannot be unchecked")
    with run.update() as m:
        _user(m)["checked"][str(start)] = bool(checked)


def add_key(run: Run, frame: int) -> None:
    with run.update() as m:
        added = _user(m)["added"]
        if frame not in added:
            added.append(int(frame))
            added.sort()


def set_tags(run: Run, start: int, edits: dict[str, Any]) -> KeyTags:
    _find(run, start)
    base = raw_tags(run, start)
    if base is None:
        raise GateError(f"key image {start} is not tagged yet")
    with run.update() as m:
        u = _user(m)
        merged = {**u["tags"].get(str(start), {}), **edits}
        result = apply_edits(base, merged)  # raises ValueError on bad edits
        u["tags"][str(start)] = merged
    return result


def set_motion(run: Run, start: int, text: str | None) -> None:
    _find(run, start)
    with run.update() as m:
        mo = _user(m)["motion"]
        if text and text.strip():
            mo[str(start)] = text.strip()
        else:
            mo.pop(str(start), None)
