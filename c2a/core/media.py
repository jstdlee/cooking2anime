"""Thin, strict ffmpeg/ffprobe wrappers. Frames are HxWx3 uint8 RGB numpy arrays."""

from __future__ import annotations

import itertools
import json
import re
import shutil
import subprocess
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from c2a.core.errors import MediaError

Frame = NDArray[np.uint8]


def _bin(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise MediaError(f"{name} not found on PATH")
    return path


def run(args: list[str], *, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    proc = subprocess.run(args, input=stdin, capture_output=True, check=False)
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace")[-2000:]
        raise MediaError(f"{Path(args[0]).name} failed ({proc.returncode}): {tail}")
    return proc


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    fps: Fraction
    frames: int
    duration: Fraction
    has_audio: bool


def probe(path: Path, *, count_frames: bool = False) -> VideoInfo:
    if not path.exists():
        raise MediaError(f"no such file: {path}")
    args = [_bin("ffprobe"), "-v", "error", "-print_format", "json", "-show_streams",
            "-show_format"]
    if count_frames:
        args.append("-count_frames")
    proc = run([*args, str(path)])
    data = json.loads(proc.stdout)
    vstreams = [s for s in data.get("streams", []) if s.get("codec_type") == "video"]
    if not vstreams:
        raise MediaError(f"{path}: no video stream")
    v = vstreams[0]
    fps = Fraction(v.get("r_frame_rate", "0/1"))
    if fps <= 0:
        fps = Fraction(v.get("avg_frame_rate", "0/1"))
    if fps <= 0:
        raise MediaError(f"{path}: unknown frame rate")
    frames = int(v.get("nb_read_frames" if count_frames else "nb_frames", 0) or 0)
    dur = Fraction(str(data.get("format", {}).get("duration", "0")))
    if frames == 0:
        frames = round(dur * fps)
    return VideoInfo(
        width=int(v["width"]),
        height=int(v["height"]),
        fps=fps.limit_denominator(1001),
        frames=frames,
        duration=dur,
        has_audio=any(s.get("codec_type") == "audio" for s in data.get("streams", [])),
    )


def normalize(src: Path, dst: Path, fps: Fraction) -> VideoInfo:
    """Constant frame rate at `fps`, audio dropped, near-lossless intra-friendly encode."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    run([_bin("ffmpeg"), "-y", "-v", "error", "-i", str(src), "-an",
         "-vf", f"fps={fps.numerator}/{fps.denominator},format=yuv420p",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", "-g", "1",
         str(dst)])
    return probe(dst, count_frames=True)


def extract_audio(src: Path, dst: Path, sr: int = 48_000) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    run([_bin("ffmpeg"), "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "2", "-ar", str(sr),
         "-c:a", "pcm_s16le", str(dst)])


def read_gray(path: Path, width: int) -> Iterator[NDArray[np.uint8]]:
    """Every frame as a small grey image (width × proportional height, even)."""
    info = probe(path)
    h = max(2, round(info.height * width / info.width / 2) * 2)
    proc = subprocess.Popen(
        [_bin("ffmpeg"), "-v", "error", "-i", str(path), "-vf",
         f"scale={width}:{h}:flags=area", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None
    n = width * h
    try:
        while True:
            buf = proc.stdout.read(n)
            if len(buf) < n:
                break
            yield np.frombuffer(buf, np.uint8).reshape(h, width)
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()


def detect_cuts_ffmpeg(path: Path, fps: Fraction, threshold: float) -> list[int]:
    """Scene cuts as source frame indices, using ffmpeg's scene score."""
    proc = run([_bin("ffmpeg"), "-v", "info", "-i", str(path),
                "-vf", f"select='gt(scene,{threshold})',showinfo", "-an", "-f", "null", "-"])
    cuts = []
    for m in re.finditer(r"pts_time:([0-9.]+)", proc.stderr.decode(errors="replace")):
        cuts.append(round(Fraction(m.group(1)) * fps))
    return sorted(set(cuts))


def gen_size(width: int, height: int, resolution: str = "720p") -> tuple[int, int]:
    """Working size for generation: 16:9 → 1280x720, 9:16 → 720x1280 (multiples of 16)."""
    short = 720 if resolution == "720p" else 1080
    long = short * 16 // 9
    long -= long % 16
    return (long, short) if width >= height else (short, long)


def _fit_filter(w: int, h: int) -> str:
    return (f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={w}:{h},setsar=1")


def extract_frames(path: Path, indices: list[int], size: tuple[int, int]) -> dict[int, Frame]:
    """Exact frames by index (0-based), center-cropped to `size`."""
    if not indices:
        return {}
    out: dict[int, Frame] = {}
    wanted = set(indices)
    for i, frame in enumerate(read_frames(path, size)):
        if i in wanted:
            out[i] = frame
            if len(out) == len(wanted):
                break
    missing = wanted - out.keys()
    if missing:
        raise MediaError(f"{path}: frames not found: {sorted(missing)[:5]}")
    return out


def read_frames(path: Path, size: tuple[int, int]) -> Iterator[Frame]:
    w, h = size
    proc = subprocess.Popen(
        [_bin("ffmpeg"), "-v", "error", "-i", str(path), "-vf", _fit_filter(w, h),
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None
    n = w * h * 3
    try:
        while True:
            buf = proc.stdout.read(n)
            if len(buf) < n:
                break
            yield np.frombuffer(buf, np.uint8).reshape(h, w, 3)
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()


def write_video(frames: Iterable[Frame], dst: Path, fps: Fraction, *,
                out_size: tuple[int, int] | None = None, crf: int = 18,
                extra_vf: str | None = None, preset: str = "medium") -> int:
    """Encode frames (all the same size) to H.264 yuv420p. Returns frames written."""
    it = iter(frames)
    try:
        first = next(it)
    except StopIteration:
        raise MediaError("write_video: no frames") from None
    h, w = first.shape[:2]
    vf = []
    if out_size and out_size != (w, h):
        vf.append(f"scale={out_size[0]}:{out_size[1]}:flags=lanczos")
    if extra_vf:
        vf.append(extra_vf)
    vf.append("format=yuv420p")
    dst.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [_bin("ffmpeg"), "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{w}x{h}", "-r", f"{fps.numerator}/{fps.denominator}", "-i", "-",
         "-vf", ",".join(vf), "-c:v", "libx264", "-profile:v", "high", "-preset", preset,
         "-crf", str(crf), "-movflags", "+faststart", str(dst)],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    count = 0
    try:
        for frame in itertools.chain([first], it):
            if frame.shape != first.shape:
                raise MediaError("write_video: frame size changed")
            proc.stdin.write(np.ascontiguousarray(frame).tobytes())
            count += 1
    except BrokenPipeError:
        pass
    finally:
        proc.stdin.close()
    err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
    if proc.wait() != 0:
        raise MediaError(f"ffmpeg encode failed: {err[-2000:]}")
    return count


def mux(video: Path, audio: Path | None, dst: Path, duration: Fraction) -> None:
    args = [_bin("ffmpeg"), "-y", "-v", "error", "-i", str(video)]
    if audio is not None:
        args += ["-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac",
                 "-b:a", "192k", "-af", f"apad,atrim=0:{float(duration):.6f}"]
    else:
        args += ["-map", "0:v:0", "-an"]
    args += ["-c:v", "copy", "-movflags", "+faststart", str(dst)]
    run(args)


def save_png(frame: Frame, path: Path) -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(frame).save(path, format="PNG", compress_level=1)


def load_png(path: Path) -> Frame:
    from PIL import Image

    with Image.open(path) as im:
        return np.asarray(im.convert("RGB"), dtype=np.uint8).copy()


def thumbnail_jpeg(frame: Frame, max_px: int, quality: int = 80) -> bytes:
    import io

    from PIL import Image

    im = Image.fromarray(frame)
    im.thumbnail((max_px, max_px))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()
