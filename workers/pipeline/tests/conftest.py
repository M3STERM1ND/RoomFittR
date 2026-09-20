"""Shared fixtures for the pipeline tests.

Videos are synthesised with FFmpeg rather than committed. A 22-second 720p
clip is ~2 MB, it would be a binary blob in git that nobody can review, and
`testsrc2` gives us something with real texture and real motion that every
machine reproduces identically.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from roomfittr_pipeline.ingest import _resolve


def _synthesise(dst: Path, *, seconds: float, size: str, fps: int, rotate: int | None) -> Path:
    """Render a test clip. `testsrc2` moves and has texture, so it scores like real video.

    `rotate` is the clockwise display rotation we want `VideoMeta` to report,
    i.e. what a phone's portrait capture looks like. Getting that written is
    fiddlier than it sounds:

    - The legacy `-metadata:s:v:0 rotate=` tag is **silently ignored** by
      FFmpeg 8's mov muxer. A fixture built that way produces an unrotated
      file and a test that passes for the wrong reason.
    - `-display_rotation` is an *input* option and writes a real display
      matrix, which is what phones actually embed.
    - Its sign is opposite to ours: ffprobe reports a counter-clockwise angle,
      so a real iPhone portrait clip reads `-90` and is displayed rotated 90
      clockwise. Hence the negation here, which mirrors `_rotation_from`.
    - It must be applied over a **stream copy**. Combined with re-encoding,
      FFmpeg autorotates: it burns the rotation into the pixels and writes no
      matrix at all, giving a genuinely portrait file with `rotation_deg = 0`
      -- the opposite of the fixture we want. So the clip is encoded first and
      the matrix attached in a second, copy-only pass.
    """
    ffmpeg = _resolve("ffmpeg")
    encode_to = dst if rotate is None else dst.with_name(f"unrotated-{dst.name}")
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={size}:rate={fps}:duration={seconds}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(encode_to),
        ],
        check=True,
        capture_output=True,
        timeout=300,
    )
    if rotate is not None:
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-v",
                "error",
                "-display_rotation",
                str(-rotate),
                "-i",
                str(encode_to),
                "-c",
                "copy",
                str(dst),
            ],
            check=True,
            capture_output=True,
            timeout=300,
        )
        encode_to.unlink()
    return dst


@pytest.fixture(scope="session")
def good_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """22 s of 1280x720 at 30 fps: comfortably inside every 3.2 limit."""
    out = tmp_path_factory.mktemp("video") / "good.mp4"
    return _synthesise(out, seconds=22, size="1280x720", fps=30, rotate=None)


@pytest.fixture(scope="session")
def short_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """5 s: under the 20 s floor."""
    out = tmp_path_factory.mktemp("video") / "short.mp4"
    return _synthesise(out, seconds=5, size="1280x720", fps=30, rotate=None)


@pytest.fixture(scope="session")
def low_res_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """640x480: short side under the 720 px floor."""
    out = tmp_path_factory.mktemp("video") / "lowres.mp4"
    return _synthesise(out, seconds=22, size="640x480", fps=30, rotate=None)


@pytest.fixture(scope="session")
def rotated_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """1280x720 coded, tagged 90 degrees: displayed 720x1280, i.e. portrait."""
    out = tmp_path_factory.mktemp("video") / "rotated.mp4"
    return _synthesise(out, seconds=22, size="1280x720", fps=30, rotate=90)
