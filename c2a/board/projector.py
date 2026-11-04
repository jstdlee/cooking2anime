"""Manifest → board records (design §6). Pure function of the run folder; no I/O writes."""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from c2a.board import tldr
from c2a.board.tldr import Record
from c2a.core import media
from c2a.core.context import Context
from c2a.core.manifest import file_digest

PAGE_GATE, PAGE_SHOTS, PAGE_REPORT = "page-gate", "page-shots", "page-report"
CAND_W, GAP = 320, 40
CHUNK_W, CHUNK_H = 560, 330
NOTE_SIZE = 200

STATUS_COLOR = {"done": "green", "flagged": "red", "pending": "grey"}


@dataclass
class Projection:
    engine: list[Record] = field(default_factory=list)
    inputs: list[Record] = field(default_factory=list)  # created only if missing
    # text the engine put into each user-input note (to detect user edits later)
    input_text: dict[str, str] = field(default_factory=dict)


def _img_size(jpeg: bytes) -> tuple[int, int]:
    from PIL import Image

    with Image.open(io.BytesIO(jpeg)) as im:
        w, h = im.size
    return int(w), int(h)


class _Assets:
    def __init__(self, max_px: int, cache_dir: Path) -> None:
        self.max_px = max_px
        self.cache_dir = cache_dir
        self.records: list[Record] = []

    def from_png(self, key: str, path: Path) -> tuple[str, int, int]:
        digest = file_digest(path)
        cached = self.cache_dir / f"{digest[:24]}_{self.max_px}.jpg"
        if cached.exists():
            jpeg = cached.read_bytes()
        else:
            jpeg = media.thumbnail_jpeg(media.load_png(path), self.max_px)
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(jpeg)
        return self._add(key, jpeg, digest)

    def from_jpeg(self, key: str, path: Path) -> tuple[str, int, int]:
        return self._add(key, path.read_bytes(), file_digest(path))

    def _add(self, key: str, jpeg: bytes, digest: str) -> tuple[str, int, int]:
        w, h = _img_size(jpeg)
        rec = tldr.image_asset(key, jpeg, w, h, digest)
        self.records.append(rec)
        return str(rec["id"]), w, h


def link_url(ctx: Context, rel: str) -> str:
    return f"http://127.0.0.1:{ctx.cfg.canvas.server_port}/{rel}"


def _fit(w: int, h: int, box_w: float) -> tuple[float, float]:
    return box_w, box_w * h / w


def project(ctx: Context, max_px: int | None = None) -> Projection:
    run = ctx.run
    m = run.read()
    p = Projection()
    assets = _Assets(max_px or ctx.cfg.canvas.thumb_max_px, run.path(".thumbs"))
    pg = tldr.safe_id("page", PAGE_GATE)
    ps = tldr.safe_id("page", PAGE_SHOTS)
    pr = tldr.safe_id("page", PAGE_REPORT)
    p.engine += [tldr.page(PAGE_GATE, "1 · Style gate", 0),
                 tldr.page(PAGE_SHOTS, "2 · Shots", 1),
                 tldr.page(PAGE_REPORT, "3 · Report", 2)]
    idx = 0

    def nxt() -> int:
        nonlocal idx
        idx += 1
        return idx

    # ---------------- page 1: style gate ----------------
    approval = m.get("approval")
    state = m.get("state")
    status = (f"approved: candidate {approval['candidate']} (via {approval['via']})"
              if approval else f"state: {state} — drag ✓ APPROVE into one candidate frame")
    p.engine.append(tldr.text("gate-title", pg, 0, -140, 1200,
                              "Style gate · cooking2anime run " + run.id, nxt(), size="l"))
    p.engine.append(tldr.text("gate-status", pg, 0, -80, 1200, status, nxt(),
                              color="green" if approval else "orange"))
    style_text = ctx.style_prompt().strip()
    p.inputs.append(tldr.note("style-prompt", pg, -520, 0, style_text, nxt(), color="yellow"))
    p.input_text["style-prompt"] = style_text
    sticker_y = 40 + tldr.NOTE_SIZE + tldr.note_grow_y(style_text)
    p.inputs.append(tldr.note("approve-sticker", pg, -520, sticker_y, "✓ APPROVE", nxt(),
                              color="green"))
    p.engine.append(tldr.text("gate-help", pg, -520, sticker_y + 220, 420,
                              "Edit the yellow note to change the style prompt (a new "
                              "preview runs). Drag the green ✓ note into a candidate to "
                              "approve.", nxt()))
    cand_file = run.path("preview", "candidates.json")
    if cand_file.exists():
        cands = json.loads(cand_file.read_text())["candidates"]
        by_sample: dict[int, list[dict[str, Any]]] = {}
        for c in cands:
            by_sample.setdefault(int(c["sample"]), []).append(c)
        gy = 0.0
        for si, srow in sorted(by_sample.items()):
            kf = int(srow[0]["kf"])
            orig = run.path("analyze", "keyframes", f"kf_{kf:06d}.png")
            aid, w, h = assets.from_png(f"orig-{si}", orig)
            iw, ih = _fit(w, h, CAND_W)
            fh = ih + 20
            p.engine.append(tldr.frame(f"gate-orig-{si}", pg, 0, gy, iw + 20, fh,
                                       f"original · keyframe {kf}", nxt(), color="grey"))
            p.engine.append(tldr.image(f"gate-orig-img-{si}", tldr.safe_id(
                "shape", f"gate-orig-{si}"), 10, 10, iw, ih, aid, nxt(), alt="original"))
            for gcol, c in enumerate(sorted(srow, key=lambda c: int(c["seed_index"])), 1):
                aid, w, h = assets.from_png(f"cand-{c['id']}", run.path(c["image"]))
                iw, ih = _fit(w, h, CAND_W)
                fkey = f"gate-cand-{c['id']}"
                chosen = bool(approval and approval["candidate"] == c["id"])
                rec = tldr.frame(fkey, pg, gcol * (CAND_W + 20 + GAP), gy, iw + 20, ih + 20,
                                 f"candidate {c['id']}" + (" · APPROVED" if chosen else ""),
                                 nxt(), color="green" if chosen else "black")
                rec["meta"]["c2a"]["candidate"] = c["id"]
                p.engine.append(rec)
                p.engine.append(tldr.image(f"gate-cand-img-{c['id']}",
                                           tldr.safe_id("shape", fkey), 10, 10, iw, ih, aid,
                                           nxt(), alt=f"candidate {c['id']}"))
            gy += fh + 60

    # ---------------- page 2: shots ----------------
    tl_file = run.path("analyze", "timeline.json")
    p.inputs.append(tldr.note("rerun-sticker", ps, -260, -260, "↻ RERUN", nxt(),
                              color="light-blue"))
    p.engine.append(tldr.text("shots-help", ps, 0, -240, 1400,
                              "Each frame is one chunk (green = passed QA, red = flagged, "
                              "grey = not generated yet). Edit a chunk's yellow note to "
                              "change its motion; drag ↻ RERUN into a chunk to re-generate "
                              "it. ▶ links open the clip (needs `c2a watch`).", nxt()))
    out_rel = (m["units"].get("export") or {}).get("outputs", {}).get("video")
    if out_rel:
        p.engine.append(tldr.geo_link("shots-output", ps, 0, -170, 320, 60,
                                      "▶ open final video", link_url(ctx, out_rel), nxt(),
                                      color="green"))
    if tl_file.exists():
        tl = ctx.timeline
        for n, c in enumerate(tl.chunks):
            col, row = n % 2, n // 2
            cx0: float = col * (CHUNK_W + NOTE_SIZE + 3 * GAP)
            cy0: float = row * (CHUNK_H + GAP)
            u = m["units"].get(f"generate/{c.id}")
            st = u["status"] if u else "pending"
            fkey = f"chunk-{c.id}"
            fid = tldr.safe_id("shape", fkey)
            rec = tldr.frame(fkey, ps, cx0, cy0, CHUNK_W, CHUNK_H,
                             f"chunk {c.id} · {st} · {float(c.t_start):.2f}–"
                             f"{float(c.t_end):.2f}s", nxt(), color=STATUS_COLOR[st])
            rec["meta"]["c2a"]["chunk"] = c.id
            p.engine.append(rec)
            for side, kf, cx in (("first", c.first_kf, 10), ("last", c.last_kf, 190)):
                rp = run.path("restyle", f"kf_{kf:06d}.png")
                if rp.exists():
                    aid, w, h = assets.from_png(f"chunk-{c.id}-{side}", rp)
                    iw, ih = _fit(w, h, 160)
                    p.engine.append(tldr.image(f"chunk-{c.id}-{side}", fid, cx, 10, iw, ih,
                                               aid, nxt(), alt=f"{side} keyframe"))
            strip = run.path("generate", f"{c.id}.strip.jpg")
            if u and strip.exists():
                aid, w, h = assets.from_jpeg(f"chunk-{c.id}-strip", strip)
                iw, ih = _fit(w, h, 540)
                p.engine.append(tldr.image(f"chunk-{c.id}-strip", fid, 10, 120, iw,
                                           min(ih, 120), aid, nxt(), alt="frame strip"))
                p.engine.append(tldr.geo_link(f"chunk-{c.id}-open", fid, 370, 10, 180, 60,
                                              "▶ open clip",
                                              link_url(ctx, u["outputs"]["video"]), nxt()))
            metrics = (u or {}).get("metrics", {})
            mtext = " · ".join(f"{k} {v}" for k, v in metrics.items()
                               if k in ("boundary_ssim_first", "boundary_ssim_last",
                                        "motion_match", "flicker_proxy"))
            if u:
                mtext += f" · attempts {u.get('attempts')}"
            p.engine.append(tldr.text(f"chunk-{c.id}-metrics", fid, 10, 250, 540,
                                      mtext or "not generated yet", nxt()))
            desc = run.path("describe", f"{c.id}.json")
            if desc.exists():
                from c2a.prompts.builder import beats_sentence
                from c2a.prompts.describe import ChunkDescription
                d = ChunkDescription.model_validate(json.loads(desc.read_text()))
                override = m["overrides"].get("chunks", {}).get(c.id)
                body = override or beats_sentence(d)
                key = f"prompt-{c.id}"
                p.inputs.append(tldr.note(key, ps, cx0 + CHUNK_W + GAP, cy0, body, nxt(),
                                          color="yellow", chunk=c.id))
                p.input_text[key] = body

    # ---------------- page 3: report ----------------
    report = run.path("out", "report.json")
    lines = [f"run {run.id}", f"state: {state}"]
    if report.exists():
        r = json.loads(report.read_text())
        ch = r["checks"]
        lines += [f"output: {r['output']}",
                  f"frames: {ch['frames']} (expected {ch['frames_expected']})",
                  f"size: {ch['size'][0]}x{ch['size'][1]} · audio: {ch['has_audio']}",
                  f"loudness: {ch.get('loudness_lufs', '–')} LUFS · "
                  f"true peak {ch.get('true_peak_dbtp', '–')} dBTP",
                  f"flagged chunks: {', '.join(r['flagged_chunks']) or 'none'}",
                  f"flagged keyframes: {', '.join(r['flagged_keyframes']) or 'none'}"]
    last = (m.get("events") or [{}])[-1]
    if last.get("note"):
        lines.append(f"last note: {last['note']}")
    p.engine.append(tldr.text("report-body", pr, 0, 0, 900, "\n".join(lines), nxt(),
                              size="m"))

    p.engine = assets.records + p.engine
    return p
