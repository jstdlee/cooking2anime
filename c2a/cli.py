"""c2a command line (v2). Every Studio action has a command here (same functions: c2a.app)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from c2a import __version__, app
from c2a.config import Config, load_config, parse_set
from c2a.core.errors import C2AError
from c2a.core.manifest import Run


def _cfg(a: argparse.Namespace) -> Config:
    overrides = parse_set(getattr(a, "set", None) or [])
    if getattr(a, "stack", None):
        overrides.setdefault("run", {})["stack"] = a.stack
    return load_config(Path(a.config) if getattr(a, "config", None) else None, overrides)


def _go(run: Run) -> int:
    from c2a.core.pipeline import run_pipeline

    outcome = run_pipeline(run)
    v = app.view(run)
    if outcome == "awaiting_approval":
        keys = v.get("keys", [])
        print(f"\nrun:     {run.root}\nkeys:    {sum(k['checked'] for k in keys)} of "
              f"{len(keys)} key images checked   (c2a keys {run.root})\n"
              f"samples: {', '.join(s['id'] for s in v.get('style', {}).get('samples', []))}\n"
              f"next:    c2a approve {run.root} --pick <sample>    or open it in c2a studio")
    else:
        o = v["output"]
        print(f"\ndone:    {run.path(o['video'])}\nreport:  {run.path(o['report'])}")
        if o["flagged_clips"]:
            print(f"flagged clips: {', '.join(o['flagged_clips'])}")
    return 0


def cmd_run(a: argparse.Namespace) -> int:
    cfg = _cfg(a)
    run = app.create_run(Path(a.workdir or cfg.run.workdir), Path(a.input), cfg)
    print(f"created run {run.id}")
    return _go(run)


def cmd_resume(a: argparse.Namespace) -> int:
    return _go(Run.open(Path(a.run)))


def cmd_keys(a: argparse.Namespace) -> int:
    run = Run.open(Path(a.run))
    changed = False
    for f in a.check or []:
        app.toggle_key(run, f, True)
        changed = True
    for f in a.uncheck or []:
        app.toggle_key(run, f, False)
        changed = True
    for t in a.add or []:
        print(f"added key image at frame {app.add_key(run, t)}")
        changed = True
    if a.motion:
        frame, text = a.motion
        app.set_motion(run, int(frame), text)
        changed = True
    if a.tags:
        frame, raw = a.tags
        app.edit_tags(run, int(frame), json.loads(raw))
        changed = True
    if changed and not a.no_resume:
        return _go(run)
    v = app.view(run)
    if a.json:
        print(json.dumps(v.get("keys", []), indent=2))
        return 0
    for k in v.get("keys", []):
        tags = k["tags"] or {}
        mot = tags.get("motion", {})
        flag = "☑" if k["checked"] else "☐"
        lock = " (shot start)" if k["locked"] else ""
        print(f"{flag} frame {k['start']:>6}  {k['t']:7.2f}s  {tags.get('action', '?'):<10} "
              f"{mot.get('direction', '')} {mot.get('speed', '')}  "
              f"importance {tags.get('importance', 0):.2f}{lock}")
    if "plan" in v:
        p = v["plan"]
        print(f"→ {len(p['clips'])} clips, {p['out_duration']:.2f} s ({p['mode']})")
    return 0


def cmd_style(a: argparse.Namespace) -> int:
    run = Run.open(Path(a.run)) if a.run else None
    if a.list or run is None:
        from c2a.prompts.style import catalog

        for layer, items in catalog().items():
            print(f"{layer}:")
            for it in items:
                print(f"  {it['name']:<24} {it['title']} — {it['description']}")
        return 0
    sel: dict[str, Any] = {}
    for layer in ("animation", "director", "finish"):
        v = getattr(a, layer)
        if v is not None:
            sel[layer] = None if v == "none" else v
    if a.prompt is not None:
        sel["custom_prompt"] = a.prompt
    if a.negative is not None:
        sel["custom_negative"] = a.negative
    if sel:
        app.set_style(run, **sel)
        if not a.no_resume:
            return _go(run)
    print(json.dumps(app.view(run)["style"]["composed"], indent=2))
    return 0


def cmd_clips(a: argparse.Namespace) -> int:
    run = Run.open(Path(a.run))
    ch: dict[str, Any] = {}
    if a.mode:
        ch["length_mode"] = a.mode
    if a.fixed is not None:
        ch["fixed_s"] = a.fixed
    if a.length:
        frame, secs = a.length
        ch["per_clip"] = {int(frame): float(secs)}
        ch.setdefault("length_mode", "per_clip")
    if a.anchor is not None:
        ch["anchor_end"] = a.anchor == "on"
    if ch:
        app.set_clips(run, **ch)
        if not a.no_resume:
            return _go(run)
    v = app.view(run)
    for c in v.get("plan", {}).get("clips", []):
        print(f"{c['id']}  src {c['src'][0]:6.2f}–{c['src'][1]:6.2f}s → {c['clip_s']:.2f}s"
              f"{'  ⇥ anchored' if c['anchored'] else ''}")
    return 0


def cmd_audio(a: argparse.Namespace) -> int:
    run = Run.open(Path(a.run))
    app.set_audio(run, a.mode)
    return 0 if a.no_resume else _go(run)


def cmd_approve(a: argparse.Namespace) -> int:
    run = Run.open(Path(a.run))
    lock = app.approve(run, a.pick, via="cli")
    print(f"approved sample {lock['candidate']}")
    return 0 if a.no_resume else _go(run)


def cmd_rerun(a: argparse.Namespace) -> int:
    run = Run.open(Path(a.run))
    gone = app.rerun(run, a.clip)
    print(f"invalidated: {', '.join(gone) or '(nothing cached)'}")
    return 0 if a.no_resume else _go(run)


def cmd_status(a: argparse.Namespace) -> int:
    run = Run.open(Path(a.run))
    v = app.view(run)
    if a.json:
        print(json.dumps(v, indent=2, default=str))
        return 0
    print(f"run {v['run']}: {v['state']}")
    if v.get("note"):
        print(f"note: {v['note']}")
    for c in v.get("clips", []):
        print(f"  clip {c['id']}: {c['status']}")
    if "output" in v:
        print(f"output: {run.path(v['output']['video'])}")
    return 0


def _opener() -> list[str]:
    if sys.platform == "darwin":
        return ["open"]
    if os.name == "nt":
        return ["cmd", "/c", "start", ""]
    return ["xdg-open"]


def cmd_open(a: argparse.Namespace) -> int:
    run = Run.open(Path(a.run))
    v = app.view(run)
    if a.clip:
        c = next((c for c in v.get("clips", []) if c["id"] == a.clip), None)
        if not c or not c["video"]:
            raise C2AError(f"clip {a.clip} has not been generated yet")
        target = run.path(c["video"])
    else:
        if "output" not in v:
            raise C2AError("no output yet")
        target = run.path(v["output"]["video"])
    print(target)
    if not a.print_only and shutil.which(_opener()[0]):
        subprocess.Popen([*_opener(), str(target)])
    return 0


def cmd_studio(a: argparse.Namespace) -> int:
    from c2a.studio.server import serve

    cfg = _cfg(a)
    return serve(cfg, Path(a.workdir or cfg.run.workdir), host=a.host or cfg.studio.host,
                 port=a.port or cfg.studio.port, open_browser=not a.no_browser)


def cmd_doctor(a: argparse.Namespace) -> int:
    ok = True
    for c in app.doctor(_cfg(a)):
        ok &= c["ok"]
        print(f"{'ok ' if c['ok'] else 'BAD'} {c['name']:<15} {c['detail']}"
              + (f"\n    fix: {c['fix']}" if not c["ok"] and c["fix"] else ""))
    return 0 if ok or not a.strict else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="c2a", description=__doc__)
    p.add_argument("--version", action="version", version=f"c2a {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def cfg_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("-c", "--config", help="YAML config (defaults: configs/default.yaml)")
        sp.add_argument("--set", action="append", metavar="KEY=VALUE",
                        help="override a config value, e.g. output.resolution=1080p")
        sp.add_argument("--stack", choices=["local", "cloud", "fake"])

    def resume_flag(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--no-resume", action="store_true", help="change it, don't run")

    sp = sub.add_parser("studio", help="the Studio app: canvas + engine in your browser")
    sp.add_argument("--host")
    sp.add_argument("--port", type=int)
    sp.add_argument("--workdir")
    sp.add_argument("--no-browser", action="store_true")
    cfg_args(sp)
    sp.set_defaults(fn=cmd_studio)

    sp = sub.add_parser("run", help="start a run; stops at the style approval")
    sp.add_argument("input")
    sp.add_argument("--workdir")
    cfg_args(sp)
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("resume", help="continue a run")
    sp.add_argument("run")
    sp.set_defaults(fn=cmd_resume)

    sp = sub.add_parser("keys", help="list / check / uncheck / add key images, edit tags")
    sp.add_argument("run")
    sp.add_argument("--check", type=int, action="append", metavar="FRAME")
    sp.add_argument("--uncheck", type=int, action="append", metavar="FRAME")
    sp.add_argument("--add", type=float, action="append", metavar="SECONDS")
    sp.add_argument("--motion", nargs=2, metavar=("FRAME", "TEXT"))
    sp.add_argument("--tags", nargs=2, metavar=("FRAME", "JSON"))
    sp.add_argument("--json", action="store_true")
    resume_flag(sp)
    sp.set_defaults(fn=cmd_keys)

    sp = sub.add_parser("style", help="list presets, or set this run's style layers")
    sp.add_argument("run", nargs="?")
    sp.add_argument("--list", action="store_true")
    sp.add_argument("--animation")
    sp.add_argument("--director")
    sp.add_argument("--finish")
    sp.add_argument("--prompt", help="your own prompt text (never dropped)")
    sp.add_argument("--negative")
    resume_flag(sp)
    sp.set_defaults(fn=cmd_style)

    sp = sub.add_parser("clips", help="clip lengths: match | fixed | per_clip")
    sp.add_argument("run")
    sp.add_argument("--mode", choices=["match", "fixed", "per_clip"])
    sp.add_argument("--fixed", type=float, metavar="SECONDS")
    sp.add_argument("--length", nargs=2, metavar=("FRAME", "SECONDS"))
    sp.add_argument("--anchor", choices=["on", "off"])
    resume_flag(sp)
    sp.set_defaults(fn=cmd_clips)

    sp = sub.add_parser("audio", help="regenerate | keep_voice | silent")
    sp.add_argument("run")
    sp.add_argument("mode", choices=["regenerate", "keep_voice", "silent"])
    resume_flag(sp)
    sp.set_defaults(fn=cmd_audio)

    sp = sub.add_parser("approve", help="approve a style sample (then generate)")
    sp.add_argument("run")
    sp.add_argument("--pick", required=True, metavar="SAMPLE:VARIATION")
    resume_flag(sp)
    sp.set_defaults(fn=cmd_approve)

    sp = sub.add_parser("rerun", help="re-generate one clip with a new seed")
    sp.add_argument("run")
    sp.add_argument("--clip", required=True, metavar="kFRAME")
    resume_flag(sp)
    sp.set_defaults(fn=cmd_rerun)

    sp = sub.add_parser("status", help="show run state")
    sp.add_argument("run")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("open", help="open the output (or a clip) in the system player")
    sp.add_argument("run")
    sp.add_argument("--clip")
    sp.add_argument("--print-only", action="store_true")
    sp.set_defaults(fn=cmd_open)

    sp = sub.add_parser("doctor", help="check tools, GPU, keys and the provider stack")
    sp.add_argument("--strict", action="store_true")
    cfg_args(sp)
    sp.set_defaults(fn=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except C2AError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
