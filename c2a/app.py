"""One action layer for the CLI and the Studio (DESIGN_V2 §1, §6).

Every user action is a function here; the CLI and the Studio's WebSocket commands call
the same functions, so the two can never behave differently. `view()` is the run state
the Studio draws and `c2a status --json` prints.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any

from c2a import __version__
from c2a.config import Config, check_settings, deep_merge
from c2a.core.context import effective_config
from c2a.core.errors import C2AError, ConfigError, GateError, StageError
from c2a.core.manifest import Run
from c2a.core.pipeline import make_context, rerun_clip
from c2a.keys import state as kstate
from c2a.prompts.style import CLEAN_IMAGE, StyleSelection, catalog, compose
from c2a.prompts.vocab import CAMERA, COOKING_VERBS, DIRECTIONS, EMOTION, SPEEDS
from c2a.providers.fake import fake_stack
from c2a.stages import preview as pv

# ---------------------------------------------------------------- runs


def create_run(workdir: Path, source: Path, cfg: Config) -> Run:
    from c2a.providers.registry import build_stack

    if not source.exists():
        raise C2AError(f"input not found: {source}")
    build_stack(cfg)  # fail fast before creating a run folder
    return Run.create(workdir, source, cfg.model_dump(mode="json"))


def list_runs(workdir: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not workdir.exists():
        return out
    for d in sorted(workdir.iterdir(), reverse=True):
        if (d / "manifest.json").exists():
            m = json.loads((d / "manifest.json").read_text())
            out.append({"id": m["id"], "state": m["state"], "source": Path(m["source"]).name,
                        "created": m["created"]})
    return out


# ---------------------------------------------------------------- view


def _rel(run: Run, rel: str | None) -> str | None:
    return rel if rel and run.path(rel).exists() else None


def view(run: Run) -> dict[str, Any]:
    m = run.read()
    cfg = effective_config(run)
    ctx = make_context(run, stack=fake_stack())  # views never call providers
    units = m["units"]
    out: dict[str, Any] = {
        "run": run.id, "state": m["state"], "source": Path(m["source"]).name,
        "note": next((e.get("note") for e in reversed(m["events"]) if e.get("note")), None),
        "settings": {"style": cfg.style.selection.model_dump(),
                     "clips": cfg.clips.model_dump(), "audio": {"mode": cfg.audio.mode},
                     "output": {"resolution": cfg.output.resolution}},
        "catalog": catalog(),
        "vocab": {"verbs": list(COOKING_VERBS), "directions": list(DIRECTIONS),
                  "speeds": list(SPEEDS), "cameras": list(CAMERA), "emotions": list(EMOTION)},
    }
    try:
        meta = ctx.meta()
    except StageError:
        return out
    fps = ctx.src_fps
    out["meta"] = {"width": meta["src_w"], "height": meta["src_h"], "fps": float(fps),
                   "frames": meta["src_frames"], "duration": float(meta["src_frames"] / fps),
                   "shots": meta["shots"], "has_audio": meta["source_had_audio"]}
    keys = ctx.keys()
    out["keys"] = [{
        "id": k.id, "start": k.start, "image": k.image, "t": float(k.start / fps),
        "score": k.score, "reasons": list(k.reasons), "locked": k.locked,
        "user_added": k.user_added, "auto_checked": k.auto_checked, "checked": k.checked,
        "tags": k.tags.model_dump() if k.tags else None, "motion_override": k.motion_override,
        "thumb": _rel(run, f"keys/img_{k.start:06d}.png"),
    } for k in keys]
    if keys and all(k.tags for k in keys if k.checked):
        tl = ctx.timeline()
        out["plan"] = {
            "mode": cfg.clips.length_mode, "out_duration": float(tl.out_duration),
            "clips": [{"id": s.id, "key": s.key, "start": s.start,
                       "src": [float(s.start / fps), float(s.end / fps)],
                       "clip_s": float(s.clip_s), "anchored": s.anchored,
                       "out": [float(s.out_start / tl.out_fps), float(s.out_end / tl.out_fps)]}
                      for s in tl.segments]}
        clips = []
        for s in tl.segments:
            u = units.get(f"generate/{s.id}")
            clips.append({"id": s.id, "status": u["status"] if u else "pending",
                          "video": _rel(run, (u or {}).get("outputs", {}).get("video")),
                          "poster": _rel(run, (u or {}).get("outputs", {}).get("poster")),
                          "metrics": (u or {}).get("metrics", {}),
                          "attempts": (u or {}).get("attempts")})
        out["clips"] = clips
    style = compose(StyleSelection.model_validate(cfg.style.selection.model_dump()))
    st: dict[str, Any] = {"composed": {"image_prompt": f"{style.image_prompt}, {CLEAN_IMAGE}",
                                       "video_prompt": style.video_prompt,
                                       "negative": style.negative,
                                       "camera_bias": style.camera_bias, "key": style.key}}
    cand = run.path("preview", "candidates.json")
    if cand.exists():
        c = json.loads(cand.read_text())
        st["samples"] = [{"id": x["id"], "key": x["key"], "image": x["image"]}
                         for x in c["candidates"]]
        st["samples_current"] = c.get("style_key") == style.key
    st["approval"] = {**m["approval"], "current": pv.approval_is_current(run)} \
        if m.get("approval") else None
    out["style"] = st
    exp = units.get("export")
    if exp and run.path(exp["outputs"]["video"]).exists():
        rep = json.loads(run.path("out", "report.json").read_text())
        out["output"] = {"video": exp["outputs"]["video"], "report": "out/report.json",
                         "checks": rep["checks"], "flagged_clips": rep["flagged_clips"],
                         "audio": {k: v for k, v in rep["audio"].items()
                                   if k in ("mode", "cut_words", "voice_events")}}
    return out


# ---------------------------------------------------------------- actions


def toggle_key(run: Run, start: int, checked: bool) -> None:
    kstate.set_checked(run, start, checked)


def add_key(run: Run, t: float) -> int:
    ctx = make_context(run, stack=fake_stack())
    frame = max(0, min(ctx.src_frames - 1, round(t * float(ctx.src_fps))))
    kstate.add_key(run, frame)
    return frame


def edit_tags(run: Run, start: int, edits: dict[str, Any]) -> None:
    try:
        kstate.set_tags(run, start, edits)
    except ValueError as e:
        raise GateError(f"invalid tags: {e}") from e


def set_motion(run: Run, start: int, text: str | None) -> None:
    kstate.set_motion(run, start, text)


def update_settings(run: Run, changes: dict[str, Any]) -> None:
    """Validated per-run settings (style layers, clip lengths, audio mode, resolution)."""
    check_settings(changes)
    with run.update() as m:
        merged = deep_merge(m.get("settings") or {}, changes)
        try:
            cfg = Config.model_validate(deep_merge(m["config"], merged))
            compose(StyleSelection.model_validate(cfg.style.selection.model_dump()))
        except ValueError as e:
            raise ConfigError(str(e)) from e
        m["settings"] = merged


def set_style(run: Run, **selection: Any) -> None:
    update_settings(run, {"style": {"selection": selection}})


def set_clips(run: Run, **clips: Any) -> None:
    if "per_clip" in clips:
        cur = effective_config(run).clips.per_clip
        clips["per_clip"] = {**{int(k): v for k, v in cur.items()},
                             **{int(k): v for k, v in clips["per_clip"].items()}}
    update_settings(run, {"clips": clips})


def set_audio(run: Run, mode: str) -> None:
    update_settings(run, {"audio": {"mode": mode}})


def approve(run: Run, sample: str, via: str = "cli") -> dict[str, Any]:
    return pv.approve(run, sample, via=via)


def rerun(run: Run, clip_id: str) -> list[str]:
    try:
        return rerun_clip(run, clip_id)
    except KeyError:
        raise GateError(f"unknown clip {clip_id!r}") from None


# ---------------------------------------------------------------- doctor


def doctor(cfg: Config) -> list[dict[str, Any]]:
    """Checks with a fix hint each (Studio Doctor panel / `c2a doctor`)."""
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, fix: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "fix": fix})

    add("c2a", True, f"{__version__} · python {platform.python_version()} · "
        f"{platform.system()} {platform.machine()}")
    for tool in ("ffmpeg", "ffprobe"):
        p = shutil.which(tool)
        ver = ""
        if p:
            r = subprocess.run([p, "-version"], capture_output=True, text=True)
            ver = r.stdout.splitlines()[0] if r.stdout else ""
        add(tool, bool(p), ver or "not found", "install it: sudo apt install ffmpeg")
    gpu = shutil.which("nvidia-smi")
    if gpu:
        r = subprocess.run([gpu, "--query-gpu=name,memory.total", "--format=csv,noheader"],
                           capture_output=True, text=True)
        add("gpu", r.returncode == 0, r.stdout.strip() or "nvidia-smi failed")
    else:
        add("gpu", cfg.run.stack != "local", "no NVIDIA GPU found",
            "the local stack needs an NVIDIA GPU; use --stack cloud or fake")
    for env, what in ((cfg.providers.fal.api_key_env, "fal (cloud stack)"),
                      (cfg.providers.fish.api_key_env, "Fish Audio (voice sounds)"),
                      (cfg.providers.cloud_vlm.api_key_env, "cloud vision model (tagging)")):
        have = bool(os.environ.get(env))
        add(env, have or cfg.run.stack != "cloud", f"{'set' if have else 'not set'} · {what}",
            f"export {env}=... (only needed for that provider)")
    try:
        from c2a.providers.registry import build_stack

        build_stack(cfg)
        add("stack", True, cfg.run.stack)
    except C2AError as e:
        add("stack", False, str(e), "use --stack fake until the real stacks land")
    return checks
