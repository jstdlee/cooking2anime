"""Minimal, typed builders for tldraw `.tldr` records (pinned @tldraw/tlschema 5.6.0).

Only the record types the engine writes are modelled. Every produced board is validated
against the real schema in CI by tools/tldr-validate.
"""

from __future__ import annotations

import base64
import json
import re
from functools import cache
from importlib import resources
from typing import Any

Record = dict[str, Any]

DOCUMENT_ID = "document:document"
OWNER_ENGINE = "engine"
OWNER_INPUT = "user-input"

COLORS = ("black", "grey", "light-violet", "violet", "blue", "light-blue", "yellow",
          "orange", "green", "light-green", "light-red", "red", "white")

_DIGITS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


@cache
def schema() -> dict[str, Any]:
    d: dict[str, Any] = json.loads(
        resources.files("c2a.board").joinpath("tlschema-5.6.0.json").read_text())
    return d


def index_key(n: int) -> str:
    """n-th fractional index key in ascending order: a1..az, b10..bzz, c100.. (n ≥ 0).

    Keys never end in '0' in the fractional sense because they are pure integer keys.
    """
    if n < 0:
        raise ValueError(n)
    n += 1  # skip a0 so that a user can always insert before our first shape
    if n < 62:
        return "a" + _DIGITS[n]
    n -= 62
    for head, width in (("b", 2), ("c", 3), ("d", 4)):
        if n < 62**width:
            digits = ""
            for _ in range(width):
                n, r = divmod(n, 62)
                digits = _DIGITS[r] + digits
            return head + digits
        n -= 62**width
    raise ValueError("index too large")


def safe_id(kind: str, key: str) -> str:
    return f"{kind}:c2a-" + re.sub(r"[^A-Za-z0-9_-]", "_", key)


def rich_text(text: str) -> dict[str, Any]:
    paras = []
    for line in text.split("\n"):
        paras.append({"type": "paragraph", "content": [{"type": "text", "text": line}]}
                     if line else {"type": "paragraph"})
    return {"type": "doc", "content": paras}


def plain_text(rt: Any) -> str:
    """Text of a TipTap doc; paragraphs joined with newlines."""
    if not isinstance(rt, dict):
        return ""
    out: list[str] = []

    def walk(node: Any) -> str:
        if not isinstance(node, dict):
            return ""
        if node.get("type") == "text":
            return str(node.get("text", ""))
        return "".join(walk(c) for c in node.get("content", []) or [])

    for p in rt.get("content", []) or []:
        out.append(walk(p))
    return "\n".join(out)


def meta(owner: str, key: str, **extra: Any) -> dict[str, Any]:
    return {"c2a": {"owner": owner, "key": key, **extra}}


def document() -> Record:
    return {"id": DOCUMENT_ID, "typeName": "document", "gridSize": 10, "name": "",
            "meta": {}}


def page(key: str, name: str, idx: int) -> Record:
    return {"id": safe_id("page", key), "typeName": "page", "name": name,
            "index": index_key(idx), "meta": meta(OWNER_ENGINE, key)}


def _shape(type_: str, key: str, parent: str, x: float, y: float, idx: int,
           props: dict[str, Any], owner: str = OWNER_ENGINE, locked: bool = False,
           **meta_extra: Any) -> Record:
    return {"id": safe_id("shape", key), "typeName": "shape", "type": type_,
            "x": float(x), "y": float(y), "rotation": 0, "index": index_key(idx),
            "parentId": parent, "isLocked": locked, "opacity": 1, "props": props,
            "meta": meta(owner, key, **meta_extra)}


def frame(key: str, parent: str, x: float, y: float, w: float, h: float, name: str,
          idx: int, color: str = "black") -> Record:
    return _shape("frame", key, parent, x, y, idx,
                  {"w": float(w), "h": float(h), "name": name, "color": color})


def text(key: str, parent: str, x: float, y: float, w: float, body: str, idx: int,
         size: str = "s", color: str = "black") -> Record:
    return _shape("text", key, parent, x, y, idx, {
        "color": color, "size": size, "font": "sans", "textAlign": "start", "w": float(w),
        "richText": rich_text(body), "scale": 1, "autoSize": False})


def geo_link(key: str, parent: str, x: float, y: float, w: float, h: float, label: str,
             url: str, idx: int, color: str = "blue") -> Record:
    return _shape("geo", key, parent, x, y, idx, {
        "geo": "rectangle", "dash": "solid", "url": url, "w": float(w), "h": float(h),
        "growY": 0, "scale": 1, "flipX": False, "flipY": False, "labelColor": color,
        "color": color, "fill": "semi", "size": "s", "font": "sans", "align": "middle",
        "verticalAlign": "middle", "richText": rich_text(label)})


NOTE_SIZE = 200.0
_NOTE_CHARS_PER_LINE = 17  # size "s" sans in a 200px note (measured in GoDraw)
_NOTE_LINE_PX = 25.0
_NOTE_PAD_PX = 32.0


def note_grow_y(body: str) -> float:
    """Extra height a note needs so its text does not overflow (apps recompute it)."""
    lines = sum(max(1, -(-len(p) // _NOTE_CHARS_PER_LINE)) for p in body.split("\n"))
    return max(0.0, lines * _NOTE_LINE_PX + _NOTE_PAD_PX - NOTE_SIZE)


def note(key: str, parent: str, x: float, y: float, body: str, idx: int,
         color: str = "yellow", owner: str = OWNER_INPUT, **meta_extra: Any) -> Record:
    return _shape("note", key, parent, x, y, idx, {
        "color": color, "labelColor": "black", "size": "s", "font": "sans",
        "fontSizeAdjustment": None, "align": "start", "verticalAlign": "start",
        "growY": note_grow_y(body),
        "url": "", "richText": rich_text(body), "scale": 1, "textLastEditedBy": None},
        owner=owner, **meta_extra)


def image_asset(key: str, jpeg: bytes, w: int, h: int, digest: str) -> Record:
    return {"id": safe_id("asset", f"{key}-{digest[:10]}"), "typeName": "asset",
            "type": "image", "props": {
                "w": w, "h": h, "name": f"{key}.jpg", "isAnimated": False,
                "mimeType": "image/jpeg",
                "src": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode(),
                "fileSize": len(jpeg)},
            "meta": meta(OWNER_ENGINE, key)}


def image(key: str, parent: str, x: float, y: float, w: float, h: float, asset_id: str,
          idx: int, alt: str = "", url: str = "") -> Record:
    return _shape("image", key, parent, x, y, idx, {
        "w": float(w), "h": float(h), "playing": True, "url": url, "assetId": asset_id,
        "crop": None, "flipX": False, "flipY": False, "altText": alt})


def owner_of(rec: Record) -> str | None:
    m = rec.get("meta") or {}
    c = m.get("c2a") if isinstance(m, dict) else None
    return c.get("owner") if isinstance(c, dict) else None


def key_of(rec: Record) -> str | None:
    m = rec.get("meta") or {}
    c = m.get("c2a") if isinstance(m, dict) else None
    return c.get("key") if isinstance(c, dict) else None


def file_doc(records: list[Record]) -> dict[str, Any]:
    return {"tldrawFileFormatVersion": 1, "schema": schema(), "records": records}
