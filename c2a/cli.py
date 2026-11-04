"""c2a command line. Everything the GoDraw board can do, the CLI can do too."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

from c2a import __version__
from c2a.config import Config, load_config, parse_set
from c2a.core.errors import C2AError
from c2a.core.manifest import Run


def _cfg_from_args(a: argparse.Namespace) -> Config:
    overrides = parse_set(a.set or [])
    if a.stack:
        overrides.setdefault("run", {})
        assert isinstance(overrides["run"], dict)
        overrides["run"]["stack"] = a.stack
    return load_config(Path(a.config) if a.config else None, overrides)


def _run_and_report(run: Run) -> int:
    from c2a.core.pipeline import run_pipeline

    outcome = run_pipeline(run)
    if outcome == "awaiting_approval":
        print(f"\nrun:   {run.root}\nboard: {run.path('board.tldr')}\n"
              f"next:  c2a board open {run.root}   (drag ✓ APPROVE into a candidate)\n"
              f"   or: c2a approve {run.root} --pick <sample:seed>")
    else:
        report = json.loads(run.path("out", "report.json").read_text())
        print(f"\ndone:  {run.path(report['output'])}\nreport: {run.path('out', 'report.json')}")
        if report["flagged_chunks"]:
            print(f"flagged chunks: {', '.join(report['flagged_chunks'])}")
    return 0


def cmd_run(a: argparse.Namespace) -> int:
    from c2a.providers.registry import build_stack

    cfg = _cfg_from_args(a)
    src = Path(a.input)
    if not src.exists():
        raise C2AError(f"input not found: {src}")
    build_stack(cfg)  # fail fast before creating a run folder
    run = Run.create(Path(a.workdir or cfg.run.workdir), src, cfg.model_dump(mode="json"))
    print(f"created run {run.id}")
    return _run_and_report(run)


def cmd_resume(a: argparse.Namespace) -> int:
    return _run_and_report(Run.open(Path(a.run)))


def cmd_approve(a: argparse.Namespace) -> int:
    from c2a.stages.preview import approve

    run = Run.open(Path(a.run))
    lock = approve(run, a.pick, via="cli")
    print(f"approved candidate {lock['candidate']} (seed {lock['seed']})")
    return 0 if a.no_resume else _run_and_report(run)


def cmd_rerun(a: argparse.Namespace) -> int:
    from c2a.core.pipeline import rerun_chunk

    run = Run.open(Path(a.run))
    try:
        gone = rerun_chunk(run, a.chunk)
    except KeyError:
        raise C2AError(f"unknown chunk {a.chunk!r}") from None
    print(f"invalidated: {', '.join(gone) or '(nothing cached)'}")
    return 0 if a.no_resume else _run_and_report(run)


def cmd_status(a: argparse.Namespace) -> int:
    run = Run.open(Path(a.run))
    m = run.read()
    units = m["units"]
    summary = {
        "run": run.id,
        "state": m["state"],
        "approval": m.get("approval"),
        "chunks": {k.split("/", 1)[1]: v["status"] for k, v in units.items()
                   if k.startswith("generate/")},
        "last_event": (m.get("events") or [None])[-1],
    }
    if a.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"run {run.id}: {m['state']}")
        if m.get("approval"):
            print(f"approved: {m['approval']['candidate']} via {m['approval']['via']}")
        for cid, st in summary["chunks"].items():
            print(f"  chunk {cid}: {st}")
        ev = summary["last_event"]
        if ev and ev.get("note"):
            print(f"note: {ev['note']}")
    return 0


def _opener() -> list[str]:
    if sys.platform == "darwin":
        return ["open"]
    if os.name == "nt":
        return ["cmd", "/c", "start", ""]
    return ["xdg-open"]


def cmd_open(a: argparse.Namespace) -> int:
    run = Run.open(Path(a.run))
    units = run.read()["units"]
    if a.chunk:
        u = units.get(f"generate/{a.chunk}")
        if not u:
            raise C2AError(f"chunk {a.chunk} has not been generated yet")
        target = run.path(u["outputs"]["video"])
    else:
        u = units.get("export")
        if not u:
            raise C2AError("no output yet")
        target = run.path(u["outputs"]["video"])
    print(target)
    if not a.print_only and shutil.which(_opener()[0]):
        subprocess.Popen([*_opener(), str(target)])
    return 0


def _godraw_bin() -> str | None:
    return os.environ.get("C2A_GODRAW") or shutil.which("godraw")


def cmd_board(a: argparse.Namespace) -> int:
    from c2a.board.sync import project_board
    from c2a.core.pipeline import make_context

    run = Run.open(Path(a.run))
    ctx = make_context(run)
    if a.action == "rebuild":
        ctx.run.path(ctx.cfg.canvas.board).unlink(missing_ok=True)
        with run.update() as m:
            m.setdefault("board", {})["input_text"] = {}
        print(project_board(ctx))
        return 0
    path = project_board(ctx)
    exe = _godraw_bin()
    if not exe:
        print(f"{path}\n(GoDraw not found: set C2A_GODRAW or put `godraw` on PATH)")
        return 0
    subprocess.Popen([exe, str(path)])
    print(f"opened {path} in {exe}")
    return 0


def cmd_watch(a: argparse.Namespace) -> int:
    from c2a.board.server import serve
    from c2a.board.sync import apply_inputs, project_board, read_inputs
    from c2a.core.pipeline import make_context, run_pipeline

    run = Run.open(Path(a.run))
    ctx = make_context(run)
    httpd = None if a.once else serve(run.root, ctx.cfg.canvas.server_port)
    path = project_board(ctx)
    print(f"watching {path} (Ctrl-C to stop)" if not a.once else f"checking {path}")
    last_mtime = -1.0
    try:
        while True:
            mtime = path.stat().st_mtime if path.exists() else 0.0
            changes: list[str] = []
            if mtime != last_mtime:
                changes = apply_inputs(run, read_inputs(path))
                for c in changes:
                    print(f"board: {c}")
            state = run.read()["state"]
            if changes or state not in ("awaiting_approval", "done", "failed"):
                try:
                    run_pipeline(run)
                except C2AError as e:
                    print(f"error: {e}", file=sys.stderr)
            last_mtime = path.stat().st_mtime if path.exists() else 0.0
            if a.once:
                return 0
            time.sleep(a.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        if httpd:
            httpd.shutdown()


def cmd_doctor(a: argparse.Namespace) -> int:
    ok = True

    def line(name: str, good: bool, detail: str) -> None:
        nonlocal ok
        ok &= good
        print(f"{'ok ' if good else 'BAD'} {name:<10} {detail}")

    print(f"c2a {__version__} · python {platform.python_version()} · "
          f"{platform.system()} {platform.machine()}")
    for tool in ("ffmpeg", "ffprobe"):
        p = shutil.which(tool)
        ver = ""
        if p:
            ver = subprocess.run([p, "-version"], capture_output=True, text=True).stdout
            ver = ver.splitlines()[0] if ver else ""
        line(tool, bool(p), ver or "not found")
    g = _godraw_bin()
    line("godraw", True, g or "not found (optional; boards still open in any .tldr app)")
    cfg = _cfg_from_args(a)
    for env in (cfg.providers.fal.api_key_env, cfg.providers.fish.api_key_env,
                cfg.providers.cloud_vlm.api_key_env):
        line("env", True, f"{env} {'set' if os.environ.get(env) else 'not set'}")
    try:
        from c2a.providers.registry import build_stack
        build_stack(cfg)
        line("stack", True, cfg.run.stack)
    except C2AError as e:
        line("stack", cfg.run.stack == "fake" or not a.strict, str(e))
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="c2a", description=__doc__)
    p.add_argument("--version", action="version", version=f"c2a {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def cfg_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("-c", "--config", help="YAML config (defaults: configs/default.yaml)")
        sp.add_argument("--set", action="append", metavar="KEY=VALUE",
                        help="override a config value, e.g. output.resolution=1080p")
        sp.add_argument("--stack", choices=["local", "cloud", "fake"])

    sp = sub.add_parser("run", help="start a run; stops at the style gate")
    sp.add_argument("input")
    sp.add_argument("--workdir")
    cfg_args(sp)
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("resume", help="continue a run from where it stopped")
    sp.add_argument("run")
    sp.set_defaults(fn=cmd_resume)

    sp = sub.add_parser("approve", help="approve a style candidate without the canvas")
    sp.add_argument("run")
    sp.add_argument("--pick", required=True, metavar="SAMPLE:SEED")
    sp.add_argument("--no-resume", action="store_true")
    sp.set_defaults(fn=cmd_approve)

    sp = sub.add_parser("rerun", help="re-generate one chunk (new seed) and everything after")
    sp.add_argument("run")
    sp.add_argument("--chunk", required=True, metavar="SHOT.INDEX")
    sp.add_argument("--no-resume", action="store_true")
    sp.set_defaults(fn=cmd_rerun)

    sp = sub.add_parser("status", help="show run state")
    sp.add_argument("run")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("open", help="open the output (or a chunk) in the system player")
    sp.add_argument("run")
    sp.add_argument("--chunk")
    sp.add_argument("--print-only", action="store_true")
    sp.set_defaults(fn=cmd_open)

    sp = sub.add_parser("board", help="open or rebuild the GoDraw board")
    sp.add_argument("action", choices=["open", "rebuild"])
    sp.add_argument("run")
    sp.set_defaults(fn=cmd_board)

    sp = sub.add_parser("watch", help="apply board edits and keep the run moving")
    sp.add_argument("run")
    sp.add_argument("--interval", type=float, default=1.0)
    sp.add_argument("--once", action="store_true", help="check the board once and exit")
    sp.set_defaults(fn=cmd_watch)

    sp = sub.add_parser("doctor", help="check tools, keys and the provider stack")
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
