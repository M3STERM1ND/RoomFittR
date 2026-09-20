"""S2 frame extraction and keyframe selection (implementation-plan.md 3.3).

Decode at 3 fps, score every candidate, drop the bad ones, then pick a spread
of `N` keyframes that actually covers the room rather than the parts of it the
user lingered on.

The split in this module is deliberate: the scoring and selection functions
are pure and take arrays, while `run()` does the decoding. Every threshold in
here is a judgement call that needs testing against hand-built inputs, and
none of those tests should need a video file.

**Why selection is by motion and not by time.** Uniform temporal sampling
sounds fair and is not: somebody walking a room stops at the interesting
corner and sweeps past the boring wall, so equal time gives many near-identical
views of one corner and two of the far wall. Reconstruction quality depends on
*baseline* between views, so frames are kept when the camera has actually
moved. Uniform temporal coverage comes back in at the end, as a tie-break when
trimming to N.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from numpy.typing import NDArray

from .errors import PipelineError, Stage
from .ingest import VideoMeta, _resolve

# What OpenCV hands back. Its stubs widen every return to a union over integer
# and floating dtypes, because OpenCV genuinely can return either depending on
# the operation. Annotating our own parameters as `NDArray[np.uint8]` is
# therefore a lie that mypy correctly rejects the moment a cv2 result is passed
# along: `cv2.GaussianBlur(u8)` is not statically known to be uint8 even though
# it always is. Taking `MatLike` at the boundary is the honest signature, and
# every function here works on whatever integer image it is given.
Image = cv2.typing.MatLike

# 3.3: decode at 3 fps. A 90 s video yields ~270 candidates, which is enough
# choice for keyframe selection without the decode dominating the stage.
DECODE_FPS = 3.0

# Scoring runs on a 640 px downscale. Sharpness ranking is scale-sensitive in
# absolute terms but stable in rank, and the downscale makes the whole stage
# roughly 10x cheaper.
SCORE_LONG_SIDE_PX = 640

# 3.3: drop frames below the 25th percentile of sharpness. This is relative on
# purpose -- absolute Laplacian variance depends on the scene's texture, so a
# fixed threshold throws away every frame of a white-walled room and none of a
# bookshelf.
SHARPNESS_PERCENTILE = 25.0

# Exposure limits. A frame is unusable if most of it carries no recoverable
# detail, in either direction.
MAX_CLIPPED_HIGHLIGHT_FRAC = 0.25
MAX_CRUSHED_SHADOW_FRAC = 0.50
CLIPPED_HIGHLIGHT_LEVEL = 250
CRUSHED_SHADOW_LEVEL = 12

# 3.3 early exits.
MIN_USABLE_FRAMES = 30
MIN_MEDIAN_LUMINANCE = 35.0

# 3.3: keep a frame once accumulated flow exceeds 8-12% of the image diagonal.
# The midpoint is used; the band is the plan's uncertainty, not a tuning knob
# we have data for yet. E1 gets to move it.
FLOW_KEYFRAME_FRAC = 0.10

# 3.3 target count, clamped by the GPU memory profile (3.9).
DEFAULT_TARGET_FRAMES = 100
MIN_TARGET_FRAMES = 40
MAX_TARGET_FRAMES = 150

# Optical flow is tracked on FAST corners with Lucas-Kanade (3.3). A few
# hundred corners is plenty for a median magnitude and keeps the per-frame
# cost negligible.
_MAX_CORNERS = 300
_CORNER_QUALITY = 0.01
_CORNER_MIN_DISTANCE_PX = 8


@dataclass(frozen=True, slots=True)
class FrameScore:
    """Per-candidate quality metrics. Kept on the scan for debugging."""

    index: int
    timestamp_s: float
    sharpness: float
    mean_luminance: float
    clipped_highlight_frac: float
    crushed_shadow_frac: float
    # Median flow magnitude in pixels since the previous candidate, in the
    # scoring downscale's pixel units. None for the first frame.
    flow_px: float | None = None

    @property
    def exposure_ok(self) -> bool:
        return (
            self.clipped_highlight_frac <= MAX_CLIPPED_HIGHLIGHT_FRAC
            and self.crushed_shadow_frac <= MAX_CRUSHED_SHADOW_FRAC
        )


def sharpness(gray: Image) -> float:
    """Variance of the Laplacian: the standard cheap focus measure.

    Blur suppresses high spatial frequencies, and the Laplacian is a
    second-derivative operator, so its variance collapses as a frame softens.
    It is not comparable across scenes, which is why callers rank rather than
    threshold it.
    """
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def exposure(gray: Image) -> tuple[float, float, float]:
    """Return (mean luminance, clipped-highlight fraction, crushed-shadow fraction)."""
    total = int(np.asarray(gray).size)
    if total == 0:
        return 0.0, 0.0, 1.0
    return (
        float(np.asarray(gray).mean()),
        float(np.count_nonzero(np.asarray(gray) >= CLIPPED_HIGHLIGHT_LEVEL) / total),
        float(np.count_nonzero(np.asarray(gray) <= CRUSHED_SHADOW_LEVEL) / total),
    )


def flow_magnitude(prev_gray: Image, gray: Image) -> float:
    """Median sparse optical-flow magnitude between two frames, in pixels.

    Returns 0.0 when the frame has too little texture to track. That is the
    safe direction: a featureless frame reports "camera did not move", so
    selection does not keep it, and a wall of flat paint is exactly the case
    where a tracked estimate would be noise anyway.
    """
    corners = cv2.goodFeaturesToTrack(
        prev_gray,
        maxCorners=_MAX_CORNERS,
        qualityLevel=_CORNER_QUALITY,
        minDistance=_CORNER_MIN_DISTANCE_PX,
    )
    if corners is None or len(corners) < 8:
        return 0.0

    # `nextPts=None` asks OpenCV to allocate the output, which is the normal
    # way to call this. The bundled stub types that parameter as required and
    # non-optional, so the correct call does not match any overload; the
    # ignore is on the stub's behalf, not ours.
    nxt, status, _ = cv2.calcOpticalFlowPyrLK(prev_gray, gray, corners, None)  # type: ignore[call-overload]
    if nxt is None or status is None:
        return 0.0
    tracked = np.asarray(status).ravel() == 1
    if np.count_nonzero(tracked) < 8:
        return 0.0

    delta = np.asarray(nxt)[tracked].reshape(-1, 2) - np.asarray(corners)[tracked].reshape(-1, 2)
    return float(np.median(np.linalg.norm(delta, axis=1)))


def usable(scores: list[FrameScore]) -> list[FrameScore]:
    """Drop frames that are blurry relative to the clip, or badly exposed.

    Raises `VIDEO_TOO_DARK` before the sharpness filter, because a dark clip
    fails both tests and "turn the lights on" is the useful instruction. A
    dark room is also genuinely blurry: the phone drops its shutter speed, so
    reporting blur first would send the user off to hold the camera steadier,
    which will not help.
    """
    if not scores:
        raise PipelineError("VIDEO_TOO_SHORT_OR_BLURRY", Stage.FRAMES, "no frames decoded")

    median_luminance = float(np.median([s.mean_luminance for s in scores]))
    if median_luminance < MIN_MEDIAN_LUMINANCE:
        raise PipelineError(
            "VIDEO_TOO_DARK",
            Stage.FRAMES,
            f"median luminance {median_luminance:.1f} < {MIN_MEDIAN_LUMINANCE}",
        )

    well_exposed = [s for s in scores if s.exposure_ok]
    if not well_exposed:
        raise PipelineError("VIDEO_TOO_DARK", Stage.FRAMES, "every frame is clipped or crushed")

    cutoff = float(np.percentile([s.sharpness for s in well_exposed], SHARPNESS_PERCENTILE))
    kept = [s for s in well_exposed if s.sharpness >= cutoff]

    if len(kept) < MIN_USABLE_FRAMES:
        raise PipelineError(
            "VIDEO_TOO_SHORT_OR_BLURRY",
            Stage.FRAMES,
            f"{len(kept)} usable frames < {MIN_USABLE_FRAMES}",
        )
    return kept


def select_keyframes(
    scores: list[FrameScore], diagonal_px: float, target: int = DEFAULT_TARGET_FRAMES
) -> list[FrameScore]:
    """Greedily keep frames once the camera has moved far enough, then fit `target`.

    Deterministic by construction: no randomness, no dict ordering, no
    floating-point accumulation across runs. 3.10's E1 compares models by
    rerunning the same keyframes, which only means anything if the same video
    yields the same set every time.
    """
    target = max(MIN_TARGET_FRAMES, min(MAX_TARGET_FRAMES, target))
    if not scores:
        raise PipelineError("VIDEO_TOO_SHORT_OR_BLURRY", Stage.FRAMES, "no usable frames")

    threshold = diagonal_px * FLOW_KEYFRAME_FRAC
    chosen = [scores[0]]
    accumulated = 0.0
    for score in scores[1:]:
        accumulated += score.flow_px or 0.0
        if accumulated >= threshold:
            chosen.append(score)
            accumulated = 0.0

    if len(chosen) > target:
        chosen = _thin_uniformly(chosen, target)
    elif len(chosen) < target:
        chosen = _pad_from(chosen, scores, target)
    return chosen


def _thin_uniformly(chosen: list[FrameScore], target: int) -> list[FrameScore]:
    """Keep `target` frames spread evenly in time.

    Selects by timestamp rather than by list position so a burst of keyframes
    from one busy corner cannot crowd out the rest of the room: evenly spaced
    *indices* into a list that is itself unevenly distributed in time would
    inherit exactly the bias selection just removed.
    """
    if target >= len(chosen):
        return chosen
    times = np.array([s.timestamp_s for s in chosen])
    wanted = np.linspace(times[0], times[-1], target)
    picked: list[int] = []
    for t in wanted:
        # np.argmin over the whole array each time is O(n*target), which at
        # n <= a few hundred is far cheaper than the bookkeeping to avoid it.
        order = np.argsort(np.abs(times - t))
        for candidate in order:
            if int(candidate) not in picked:
                picked.append(int(candidate))
                break
    return [chosen[i] for i in sorted(picked)]


def _pad_from(chosen: list[FrameScore], pool: list[FrameScore], target: int) -> list[FrameScore]:
    """Top back up to `target` with the sharpest unused frames.

    Reached when the camera barely moved -- a short or static capture. The
    extra views add little baseline, but starving the reconstruction of frames
    is worse than feeding it redundant ones, and S3 weights by its own
    confidence anyway.
    """
    have = {s.index for s in chosen}
    spare = sorted((s for s in pool if s.index not in have), key=lambda s: -s.sharpness)
    chosen = chosen + spare[: target - len(chosen)]
    return sorted(chosen, key=lambda s: s.index)


def _downscale(frame: Image) -> Image:
    h, w = frame.shape[:2]
    longest = max(h, w)
    if longest <= SCORE_LONG_SIDE_PX:
        return frame
    scale = SCORE_LONG_SIDE_PX / longest
    return cv2.resize(frame, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)


@dataclass(frozen=True, slots=True)
class FramesResult:
    """S2's output.

    Two copies of every keyframe, per 3.3 step 5: `model_dir` holds the
    resolution the reconstruction model wants, `full_dir` the full-resolution
    originals that S8 needs for texturing. Writing both now avoids decoding
    the video twice.
    """

    keyframes: list[FrameScore]
    model_dir: Path
    full_dir: Path
    candidates_scored: int
    diagonal_px: float


def decode_candidates(
    video: Path, meta: VideoMeta, fps: float = DECODE_FPS
) -> tuple[list[FrameScore], list[NDArray[np.uint8]]]:
    """Decode at `fps` with rotation applied, scoring as we go.

    Rotation is applied by FFmpeg rather than OpenCV: `cv2.VideoCapture`
    ignores the display matrix on most builds, which would silently feed
    sideways frames to a model that has no way to tell.
    """
    ffmpeg = _resolve("ffmpeg")
    width, height = meta.width, meta.height
    cmd = [
        ffmpeg,
        "-v",
        "error",
        "-i",
        str(video),
        "-vf",
        f"fps={fps}",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-",
    ]
    frame_bytes = width * height * 3
    frames: list[NDArray[np.uint8]] = []
    try:
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
            assert proc.stdout is not None
            while True:
                buf = proc.stdout.read(frame_bytes)
                if len(buf) < frame_bytes:
                    break
                frames.append(np.frombuffer(buf, dtype=np.uint8).reshape(height, width, 3).copy())
            proc.wait(timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PipelineError("VIDEO_UNREADABLE", Stage.FRAMES, f"decode failed: {exc}") from exc

    if not frames:
        raise PipelineError("VIDEO_UNREADABLE", Stage.FRAMES, "decoded zero frames")

    scores: list[FrameScore] = []
    prev_small: Image | None = None
    for i, frame in enumerate(frames):
        small = cv2.cvtColor(_downscale(frame), cv2.COLOR_BGR2GRAY)
        mean_luma, clipped, crushed = exposure(small)
        scores.append(
            FrameScore(
                index=i,
                timestamp_s=i / fps,
                sharpness=sharpness(small),
                mean_luminance=mean_luma,
                clipped_highlight_frac=clipped,
                crushed_shadow_frac=crushed,
                flow_px=None if prev_small is None else flow_magnitude(prev_small, small),
            )
        )
        prev_small = small
    return scores, frames


def run(
    video: Path,
    meta: VideoMeta,
    work_dir: Path,
    target: int = DEFAULT_TARGET_FRAMES,
    model_long_side_px: int = 518,
) -> FramesResult:
    """S2 end to end: decode, score, filter, select, write both copies.

    `model_long_side_px` defaults to 518, the input size of the VGGT-class
    models in 3.4. It is an argument because E1 compares models that disagree
    about it.
    """
    scores, frames = decode_candidates(video, meta)
    kept = usable(scores)

    small_h, small_w = _downscale(frames[0]).shape[:2]
    diagonal = float(np.hypot(small_w, small_h))
    keyframes = select_keyframes(kept, diagonal, target)

    model_dir = work_dir / "keyframes" / "model"
    full_dir = work_dir / "keyframes" / "full"
    model_dir.mkdir(parents=True, exist_ok=True)
    full_dir.mkdir(parents=True, exist_ok=True)

    for position, score in enumerate(keyframes):
        frame = frames[score.index]
        name = f"{position:04d}.jpg"
        cv2.imwrite(str(full_dir / name), frame, [cv2.IMWRITE_JPEG_QUALITY, 95])

        h, w = frame.shape[:2]
        scale = model_long_side_px / max(h, w)
        model_frame = (
            cv2.resize(frame, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
            if scale < 1.0
            else frame
        )
        cv2.imwrite(str(model_dir / name), model_frame, [cv2.IMWRITE_JPEG_QUALITY, 95])

    return FramesResult(
        keyframes=keyframes,
        model_dir=model_dir,
        full_dir=full_dir,
        candidates_scored=len(scores),
        diagonal_px=diagonal,
    )
