"""S1 ingest: probe the upload, enforce the input contract, strip metadata.

Three jobs, in order (implementation-plan.md 3.3):

1. **Probe.** `ffprobe` for duration, codec, dimensions, rotation, fps.
   Unreadable input stops here as `VIDEO_UNREADABLE`.
2. **Validate** against the hard limits of 3.2. These are checked again here
   even though the browser checked them, because the browser is not a
   trust boundary -- the upload goes straight to R2 and the client is the
   only thing that looked at it before now.
3. **Strip.** Remux with `-map_metadata -1 -an`. Home footage carries GPS in
   the container metadata and a room's audio is nobody's business. The
   stripped file replaces the original (7.5: "videos are untrusted input").

Rotation deserves a note. Phone video is almost always stored in the sensor's
native orientation with a display-rotation tag, so pixel dimensions lie: a
"1920x1080" portrait clip is displayed 1080x1920. Every downstream stage wants
displayed dimensions, so `VideoMeta` exposes those and keeps the raw pair for
debugging.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from .errors import PipelineError, Stage

# 3.2 "Hard limits". Enforced here and mirrored client-side for fast feedback.
MIN_DURATION_S = 20.0
MAX_DURATION_S = 180.0
MIN_SHORT_SIDE_PX = 720
MIN_FPS = 24.0
MAX_BYTES = 750 * 1024 * 1024

# Containers we accept. ffprobe reports `format_name` as a comma-separated list
# of everything the demuxer could be, so this is matched by intersection.
ALLOWED_FORMATS = frozenset({"mp4", "mov", "m4a", "3gp", "3g2", "mj2", "matroska", "webm"})

# Over-long videos are truncated rather than rejected (3.2: "truncate/downsample
# > 3 min"). Rejecting a 3:05 recording that is otherwise perfect would be a
# poor trade for the user.
_TRUNCATION_TOLERANCE_S = 0.5


@cache
def _resolve(tool: str) -> str:
    """Find `ffmpeg`/`ffprobe`, strongly preferring a system build.

    The Modal worker image pins its own patched FFmpeg (7.5) and that is what
    runs in production, so PATH is checked first and is the only branch
    production ever takes. GitHub's runners ship both binaries too, so CI also
    stops at the first line.

    The fallbacks exist for developer machines, where Windows in particular
    has no system FFmpeg and no package manager to get one from.
    `static-ffmpeg` is a dev dependency that downloads a build on first use
    and caches it; `imageio-ffmpeg` bundles ffmpeg (but not ffprobe) in the
    wheel itself. Neither is a runtime dependency of this package: shipping a
    downloaded, unpinned binary into a container that decodes untrusted user
    video is exactly what 7.5 forbids.
    """
    found = shutil.which(tool)
    if found:
        return found

    try:
        import static_ffmpeg.run as static_run

        ffmpeg_path, ffprobe_path = static_run.get_or_fetch_platform_executables_else_raise()
        return ffmpeg_path if tool == "ffmpeg" else ffprobe_path
    except Exception:  # noqa: BLE001 - any failure here just means "try the next one"
        pass

    if tool == "ffmpeg":
        try:
            import imageio_ffmpeg

            return str(imageio_ffmpeg.get_ffmpeg_exe())
        except Exception:  # noqa: BLE001
            pass

    raise PipelineError(
        "INTERNAL",
        Stage.INGEST,
        f"{tool} not found on PATH; install FFmpeg or add it to the worker image",
    )


@dataclass(frozen=True, slots=True)
class VideoMeta:
    """What S1 learned about the upload. Persisted to `scans.video_meta`."""

    duration_s: float
    # As stored, before the rotation tag is applied.
    coded_width: int
    coded_height: int
    rotation_deg: int
    fps: float
    codec: str
    format_name: str
    size_bytes: int
    has_audio: bool
    truncated_to_s: float | None = None

    @property
    def width(self) -> int:
        """Displayed width, i.e. after rotation. This is what stages should use."""
        return self.coded_height if self.rotation_deg % 180 else self.coded_width

    @property
    def height(self) -> int:
        return self.coded_width if self.rotation_deg % 180 else self.coded_height

    @property
    def short_side(self) -> int:
        return min(self.width, self.height)

    @property
    def is_portrait(self) -> bool:
        return self.height > self.width

    def as_json(self) -> dict[str, Any]:
        return {
            "duration_s": round(self.duration_s, 3),
            "width": self.width,
            "height": self.height,
            "rotation_deg": self.rotation_deg,
            "fps": round(self.fps, 3),
            "codec": self.codec,
            "format_name": self.format_name,
            "size_bytes": self.size_bytes,
            "has_audio": self.has_audio,
            "truncated_to_s": self.truncated_to_s,
        }


def _parse_fps(rate: str) -> float:
    """Turn ffprobe's `"30000/1001"` rational into a float.

    Returns 0.0 for the `"0/0"` ffprobe emits when it cannot tell, which
    validation then rejects; raising here would conflate "no frame rate" with
    "corrupt file".
    """
    if "/" in rate:
        num, _, den = rate.partition("/")
        try:
            denominator = float(den)
            if denominator == 0:
                return 0.0
            return float(num) / denominator
        except ValueError:
            return 0.0
    try:
        return float(rate)
    except ValueError:
        return 0.0


def _rotation_from(stream: dict[str, Any]) -> int:
    """Read display rotation, normalised to one of 0/90/180/270.

    Two places carry it and they disagree between FFmpeg versions: the legacy
    `tags.rotate` string, and a `displaymatrix` side-data entry whose
    `rotation` is *negative* of the clockwise angle. Newer FFmpeg drops the
    tag, older builds lack the side data, so both are read and side data wins.
    """
    for side in stream.get("side_data_list", []):
        if "rotation" in side:
            return int(round(-float(side["rotation"]))) % 360
    tag = stream.get("tags", {}).get("rotate")
    if tag is not None:
        try:
            return int(round(float(tag))) % 360
        except ValueError:
            pass
    return 0


def probe(path: Path) -> VideoMeta:
    """Run ffprobe. Raises `VIDEO_UNREADABLE` for anything we cannot decode."""
    ffprobe = _resolve("ffprobe")
    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PipelineError("VIDEO_UNREADABLE", Stage.INGEST, f"ffprobe failed: {exc}") from exc

    if completed.returncode != 0:
        raise PipelineError(
            "VIDEO_UNREADABLE",
            Stage.INGEST,
            f"ffprobe exit {completed.returncode}: {completed.stderr[:400]}",
        )

    try:
        info = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise PipelineError(
            "VIDEO_UNREADABLE", Stage.INGEST, "ffprobe emitted invalid JSON"
        ) from exc

    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise PipelineError("VIDEO_UNREADABLE", Stage.INGEST, "no video stream")

    fmt = info.get("format", {})
    # Duration lives on the container normally, but a stream-copied or
    # fragmented MP4 (which is what MediaRecorder produces) can omit it there
    # and carry it on the stream instead.
    duration_raw = fmt.get("duration") or video.get("duration")
    try:
        duration = float(duration_raw)
    except (TypeError, ValueError):
        raise PipelineError("VIDEO_UNREADABLE", Stage.INGEST, "no duration") from None

    width, height = video.get("width"), video.get("height")
    if not width or not height:
        raise PipelineError("VIDEO_UNREADABLE", Stage.INGEST, "no frame dimensions")

    # `avg_frame_rate` is the honest number for variable-frame-rate phone
    # video; `r_frame_rate` is the container's nominal base rate and reads
    # far too high on VFR files.
    fps = _parse_fps(video.get("avg_frame_rate", "0/0")) or _parse_fps(
        video.get("r_frame_rate", "0/0")
    )

    try:
        size = int(fmt.get("size", 0)) or path.stat().st_size
    except (TypeError, ValueError):
        size = path.stat().st_size

    return VideoMeta(
        duration_s=duration,
        coded_width=int(width),
        coded_height=int(height),
        rotation_deg=_rotation_from(video),
        fps=fps,
        codec=str(video.get("codec_name", "unknown")),
        format_name=str(fmt.get("format_name", "unknown")),
        size_bytes=size,
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
    )


def validate(meta: VideoMeta) -> VideoMeta:
    """Enforce 3.2. Returns meta, with `truncated_to_s` set if over-long.

    Ordered cheapest-signal-first so the user gets the most actionable
    complaint: a 5-second clip is told it is too short even if it is also
    480p, because re-recording for longer is the instruction either way.
    """
    if meta.size_bytes > MAX_BYTES:
        raise PipelineError(
            "VIDEO_UNREADABLE",
            Stage.INGEST,
            f"{meta.size_bytes} bytes exceeds the {MAX_BYTES} limit",
        )
    if meta.duration_s < MIN_DURATION_S:
        raise PipelineError(
            "VIDEO_TOO_SHORT_OR_BLURRY",
            Stage.INGEST,
            f"{meta.duration_s:.1f}s is under the {MIN_DURATION_S}s minimum",
        )
    if meta.short_side < MIN_SHORT_SIDE_PX:
        raise PipelineError(
            "VIDEO_UNREADABLE",
            Stage.INGEST,
            f"short side {meta.short_side}px is under {MIN_SHORT_SIDE_PX}px",
        )
    if meta.fps < MIN_FPS:
        raise PipelineError(
            "VIDEO_UNREADABLE", Stage.INGEST, f"{meta.fps:.1f} fps is under {MIN_FPS}"
        )
    container_names = {name.strip() for name in meta.format_name.split(",")}
    if not container_names & ALLOWED_FORMATS:
        raise PipelineError(
            "VIDEO_UNREADABLE", Stage.INGEST, f"container {meta.format_name!r} not accepted"
        )

    if meta.duration_s > MAX_DURATION_S + _TRUNCATION_TOLERANCE_S:
        return replace_truncated(meta, MAX_DURATION_S)
    return meta


def replace_truncated(meta: VideoMeta, seconds: float) -> VideoMeta:
    """Return `meta` marked as truncated to `seconds`."""
    return VideoMeta(
        duration_s=meta.duration_s,
        coded_width=meta.coded_width,
        coded_height=meta.coded_height,
        rotation_deg=meta.rotation_deg,
        fps=meta.fps,
        codec=meta.codec,
        format_name=meta.format_name,
        size_bytes=meta.size_bytes,
        has_audio=meta.has_audio,
        truncated_to_s=seconds,
    )


def strip_metadata(src: Path, dst: Path, meta: VideoMeta) -> None:
    """Remux without metadata or audio, truncating if S1 asked for it.

    Stream-copied (`-c copy`), so this is fast and lossless; it rewrites the
    container, not the pixels. The rotation tag is deliberately *preserved* --
    dropping it here would silently turn every portrait video sideways, and
    rotation is applied at decode time in S2 instead.
    """
    ffmpeg = _resolve("ffmpeg")
    cmd = [ffmpeg, "-y", "-v", "error", "-i", str(src)]
    if meta.truncated_to_s is not None:
        cmd += ["-t", str(meta.truncated_to_s)]
    cmd += ["-map_metadata", "-1", "-an", "-c", "copy", str(dst)]

    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=900, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PipelineError("INTERNAL", Stage.INGEST, f"remux failed: {exc}") from exc
    if completed.returncode != 0:
        raise PipelineError(
            "VIDEO_UNREADABLE",
            Stage.INGEST,
            f"remux exit {completed.returncode}: {completed.stderr[:400]}",
        )
    if not dst.exists() or dst.stat().st_size == 0:
        raise PipelineError("VIDEO_UNREADABLE", Stage.INGEST, "remux produced an empty file")


@dataclass(frozen=True, slots=True)
class IngestResult:
    """S1's output. `video_path` is the stripped file, which replaces the upload."""

    video_path: Path
    meta: VideoMeta
    warnings: list[str] = field(default_factory=list)


def run(src: Path, work_dir: Path) -> IngestResult:
    """S1 end to end: probe, validate, strip."""
    meta = validate(probe(src))
    work_dir.mkdir(parents=True, exist_ok=True)
    # Always .mp4: the stripped file is ours, every downstream reader is
    # FFmpeg, and a single extension keeps the R2 key predictable.
    dst = work_dir / "video.mp4"
    strip_metadata(src, dst, meta)

    warnings: list[str] = []
    if meta.truncated_to_s is not None:
        warnings.append("video_truncated")
    if meta.is_portrait:
        # Not an error -- 3.2 accepts portrait -- but it narrows the field of
        # view, which shows up later as thinner wall coverage.
        warnings.append("portrait_capture")
    return IngestResult(video_path=dst, meta=meta, warnings=warnings)
