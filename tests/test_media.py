import asyncio
import io
import json
import subprocess
import tempfile
from pathlib import Path

import pytest
from PIL import Image

from stickerbot import media


def ff(*args) -> bytes:
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / ("o" + args[-1])
        subprocess.run(["ffmpeg", "-y", "-v", "error", *args[:-1], str(out)], check=True)
        return out.read_bytes()


def info(data: bytes) -> dict:
    with tempfile.NamedTemporaryFile(suffix=".webm") as f:
        f.write(data)
        f.flush()
        r = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", f.name],
                           capture_output=True, check=True)
    return json.loads(r.stdout)


def check_video_sticker(data: bytes):
    assert len(data) <= 256 * 1024
    i = info(data)
    kinds = [s["codec_type"] for s in i["streams"]]
    assert kinds == ["video"], "must have no audio"
    v = i["streams"][0]
    assert v["codec_name"] == "vp9"
    assert max(v["width"], v["height"]) == 512 and min(v["width"], v["height"]) <= 512
    assert float(i["format"]["duration"]) <= 3.0
    n, d = v["r_frame_rate"].split("/")
    assert float(n) / float(d) <= 30.01


@pytest.mark.parametrize("w,h", [(1000, 600), (100, 300), (512, 512), (2000, 2000)])
def test_static_fits(w, h):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 200, 30)).save(buf, "JPEG")
    out, name = media.to_static(buf.getvalue())
    img = Image.open(io.BytesIO(out))
    assert max(img.size) == 512 and len(out) <= 512 * 1024


def test_static_noise_is_compressed_under_limit():
    import os
    img = Image.frombytes("RGB", (1024, 1024), os.urandom(1024 * 1024 * 3))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    out, name = media.to_static(buf.getvalue())
    assert len(out) <= 512 * 1024


def test_long_high_fps_video_with_audio():
    src = ff("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=60:duration=6",
             "-f", "lavfi", "-i", "sine=duration=6", "-shortest", "-c:v", "libx264", "-c:a", "aac", ".mp4")
    check_video_sticker(asyncio.run(media.to_video(src, ".mp4")))


def test_very_long_video_is_trimmed():
    src = ff("-f", "lavfi", "-i", "testsrc2=size=480x854:rate=24:duration=20", "-c:v", "libx264", ".mp4")
    check_video_sticker(asyncio.run(media.to_video(src, ".mp4")))


def test_gif():
    src = ff("-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=2", ".gif")
    check_video_sticker(asyncio.run(media.to_video(src, ".gif")))


def test_garbage_raises():
    with pytest.raises(media.ConversionError):
        media.to_static(b"not an image")
    with pytest.raises(media.ConversionError):
        asyncio.run(media.to_video(b"not a video", ".mp4"))
