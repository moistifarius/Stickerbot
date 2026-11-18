"""Convert arbitrary images / GIFs / videos into Telegram sticker files.

Telegram rules (core.telegram.org/stickers):
* static: PNG/WEBP, one side exactly 512 px, the other <= 512, <= 512 KB
* video:  WEBM/VP9, one side exactly 512 px, <= 3 s, <= 30 fps, <= 256 KB, no audio
"""
from __future__ import annotations

import asyncio
import io
import json
import tempfile
from pathlib import Path

from PIL import Image, ImageOps

SIDE = 512
STATIC_MAX_BYTES = 500 * 1024
VIDEO_MAX_BYTES = 255 * 1024
VIDEO_MAX_SECONDS = 2.95
SPEEDUP_LIMIT = 3.0  # clips up to 3x the limit are sped up instead of cut


class ConversionError(Exception):
    pass


def fit_size(w: int, h: int, side: int = SIDE) -> tuple[int, int]:
    """Scale so the longest side is exactly `side` (up- or down-scaling)."""
    if w <= 0 or h <= 0:
        raise ConversionError("image has no size")
    if w >= h:
        return side, max(1, round(h * side / w))
    return max(1, round(w * side / h)), side


def to_static(data: bytes) -> tuple[bytes, str]:
    """Return (bytes, filename) of a valid static sticker image."""
    try:
        img = Image.open(io.BytesIO(data))
        img.seek(0)
        img = ImageOps.exif_transpose(img).convert("RGBA")
    except Exception as e:  # noqa: BLE001
        raise ConversionError(f"cannot read image: {e}") from e
    if img.size != fit_size(*img.size):
        img = img.resize(fit_size(*img.size), Image.LANCZOS)

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    if out.tell() <= STATIC_MAX_BYTES:
        return out.getvalue(), "sticker.png"
    for quality in (95, 88, 80, 70, 60, 45):
        out = io.BytesIO()
        img.save(out, format="WEBP", quality=quality, method=6)
        if out.tell() <= STATIC_MAX_BYTES:
            return out.getvalue(), "sticker.webp"
    raise ConversionError("image too large even after compression")


async def _run(*cmd: str) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=180)
    except asyncio.TimeoutError:
        proc.kill()
        raise ConversionError(f"{cmd[0]} timed out")
    return proc.returncode, out, err


async def probe(path: Path) -> tuple[float, float, int, int]:
    """Return (duration_s, fps, width, height)."""
    rc, out, err = await _run(
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
        "-show_entries", "stream=width,height,r_frame_rate,avg_frame_rate,duration,nb_read_packets",
        "-show_entries", "format=duration", "-of", "json", str(path),
    )
    if rc != 0:
        raise ConversionError(f"ffprobe failed: {err.decode(errors='replace')[-300:]}")
    info = json.loads(out or b"{}")
    streams = info.get("streams") or []
    if not streams:
        raise ConversionError("no video stream")
    s = streams[0]

    def _rate(v: str | None) -> float:
        try:
            n, d = (v or "0/1").split("/")
            return float(n) / float(d) if float(d) else 0.0
        except ValueError:
            return 0.0

    fps = _rate(s.get("avg_frame_rate")) or _rate(s.get("r_frame_rate")) or 30.0
    dur = 0.0
    for v in (s.get("duration"), (info.get("format") or {}).get("duration")):
        try:
            dur = float(v)
            if dur > 0:
                break
        except (TypeError, ValueError):
            pass
    if dur <= 0 and s.get("nb_read_packets"):
        dur = int(s["nb_read_packets"]) / fps
    return dur, fps, int(s.get("width") or 0), int(s.get("height") or 0)


# (crf, max bitrate) ladder; first result under the size cap wins
_LADDER = [(30, "700k"), (36, "450k"), (42, "300k"), (48, "180k"), (55, "110k"), (63, "60k")]


async def to_video(data: bytes, suffix: str = ".mp4") -> bytes:
    """Return bytes of a valid video sticker (.webm VP9)."""
    with tempfile.TemporaryDirectory(prefix="stk") as td:
        src = Path(td) / f"in{suffix if suffix.startswith('.') else '.' + suffix}"
        src.write_bytes(data)
        dur, fps, w, h = await probe(src)
        if not w or not h:
            raise ConversionError("video has no size")

        filters = []
        if dur > VIDEO_MAX_SECONDS and dur <= VIDEO_MAX_SECONDS * SPEEDUP_LIMIT:
            filters.append(f"setpts=PTS*{VIDEO_MAX_SECONDS / dur:.5f}")
        if fps > 30 or (dur > VIDEO_MAX_SECONDS and dur <= VIDEO_MAX_SECONDS * SPEEDUP_LIMIT):
            filters.append("fps=30")
        tw, th = fit_size(w, h)
        filters.append(f"scale={tw}:{th}:flags=lanczos,format=yuva420p")

        dst = Path(td) / "out.webm"
        last_size = 0
        for crf, rate in _LADDER:
            rc, _, err = await _run(
                "ffmpeg", "-y", "-v", "error", "-i", str(src),
                "-vf", ",".join(filters), "-t", f"{VIDEO_MAX_SECONDS}",
                "-an", "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
                "-crf", str(crf), "-b:v", rate, "-maxrate", rate,
                "-deadline", "good", "-cpu-used", "4", "-row-mt", "1",
                "-auto-alt-ref", "0", str(dst),
            )
            if rc != 0:
                raise ConversionError(f"ffmpeg failed: {err.decode(errors='replace')[-300:]}")
            last_size = dst.stat().st_size
            if last_size <= VIDEO_MAX_BYTES:
                return dst.read_bytes()
        raise ConversionError(f"video still {last_size // 1024} KB after max compression")
