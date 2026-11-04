"""Generate synthetic fixture clips with ffmpeg (deterministic, no downloads).

cut.mp4      6 s, 29.97 fps CFR, 640x360, hard cut at 3.2 s, sine audio, burned-in text
vfr.mp4      4 s, variable frame rate, 640x360, sine audio
vertical.mp4 3 s, 30 fps, 360x640, no audio
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def ff(*args: str) -> None:
    subprocess.run([shutil.which("ffmpeg") or "ffmpeg", "-y", "-v", "error", *args],
                   check=True)


def make(out: Path) -> dict[str, Path]:
    out.mkdir(parents=True, exist_ok=True)
    cut, vfr, vert = out / "cut.mp4", out / "vfr.mp4", out / "vertical.mp4"
    if not cut.exists():
        ff("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30000/1001:duration=3.2",
           "-f", "lavfi", "-i", "mandelbrot=size=640x360:rate=30000/1001",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
           "-filter_complex",
           "[1:v]trim=duration=2.8,setpts=PTS-STARTPTS[b];[0:v][b]concat=n=2:v=1[v];"
           "[v]drawbox=x=420:y=300:w=200:h=40:color=white@0.8:t=fill[vt]",
           "-map", "[vt]", "-map", "2:a", "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-shortest", str(cut))
    if not vfr.exists():
        args = ["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=4",
                "-f", "lavfi", "-i", "sine=frequency=330:duration=4",
                "-vf", "setpts='if(lt(N,60),PTS,PTS*1.5)'"]
        tail = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(vfr)]
        try:
            ff(*args, "-fps_mode", "vfr", *tail)
        except subprocess.CalledProcessError:  # ffmpeg < 5.1
            ff(*args, "-vsync", "vfr", *tail)
    if not vert.exists():
        ff("-f", "lavfi", "-i", "testsrc2=size=360x640:rate=30:duration=3",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", str(vert))
    return {"cut": cut, "vfr": vfr, "vertical": vert}


if __name__ == "__main__":
    for k, v in make(Path(sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures/out")).items():
        print(k, v)
