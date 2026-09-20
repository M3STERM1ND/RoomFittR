"""S3 and S4 adapter interfaces (implementation-plan.md 3.4, 3.5, and R15).

Phase 1's bake-off compares three reconstruction models (MapAnything, VGGT-1B,
Depth Anything 3) against a COLMAP baseline, and 3.4 asks for them behind "a
common interface: frames -> poses, intrinsics, depth, confidence". This module
is that interface, plus the equivalent for segmentation.

Two reasons it is worth defining before any model is wired up:

- **E1 compares models, not implementations.** If each candidate arrives with
  its own idea of what a pose is, the bake-off measures adapter quality as
  much as model quality. Fixing the contract first means S5 onwards is
  identical for every candidate, which is what makes the comparison mean
  something.
- **R15 (model churn) is rated "high likelihood".** A better model appears
  roughly monthly. The mitigation the plan names is exactly this adapter
  boundary: swapping S3 becomes a measured one-day decision instead of a
  rewrite.

**Nothing here loads a model.** The real backends need GPU weights (two of
them gated on Hugging Face) and a GPU to run on; they belong in their own
modules behind these protocols. What ships here is the contract, the
validation every backend's output must pass, and a synthetic backend that
lets the rest of the pipeline be tested end to end without any of that.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import numpy as np
from numpy.typing import NDArray

from .errors import PipelineError, Stage

# 3.4 step 4: drop the least confident points. The plan says the bottom 30%.
CONFIDENCE_PERCENTILE = 30.0

# A reconstruction whose camera centres are nearly collinear has no baseline
# in one direction, so depth along it is unconstrained. 3.4 calls this out as
# a failure signal ("poses that collapse into a line").
MIN_POSE_SPREAD_RATIO = 0.02


@dataclass(frozen=True, slots=True)
class Reconstruction:
    """S3's output, in the model's own arbitrary units.

    Scale is deliberately absent. Only S7 decides what these units mean in
    millimetres, and a backend that happens to predict metric depth reports
    that as a `ScaleSource` rather than by silently emitting millimetres --
    otherwise "is this reconstruction metric?" becomes a question each
    downstream stage has to ask.
    """

    # (N, 4, 4) camera-to-world matrices, one per keyframe.
    poses: NDArray[np.float64]
    # (N, 3, 3) pinhole intrinsics.
    intrinsics: NDArray[np.float64]
    # (N, H, W) depth along the camera's +Z, in reconstruction units.
    depth: NDArray[np.float32]
    # (N, H, W) per-pixel model confidence, 0-1.
    confidence: NDArray[np.float32]
    # What produced this, for `generation_meta` and the E1 report.
    backend: str
    # Set when the backend predicts metric depth (MapAnything, DA3-metric).
    metric_scale: float | None = None

    @property
    def frame_count(self) -> int:
        return int(len(self.poses))

    def camera_centres(self) -> NDArray[np.float64]:
        return np.asarray(self.poses[:, :3, 3], dtype=np.float64)


@dataclass(frozen=True, slots=True)
class InstanceMask:
    """One tracked instance from S4, as per-frame boolean masks.

    `label_group` carries the prompt group it came from (3.5 S4), which is
    what decides removability downstream -- never the label itself.
    """

    track_id: int
    label: str
    label_group: str
    score: float
    # (N, H, W) booleans, aligned with the reconstruction's frames. Mostly
    # empty: an instance is visible in a handful of keyframes.
    masks: NDArray[np.bool_]


@runtime_checkable
class ReconstructionBackend(Protocol):
    """S3. Frames in, geometry out (3.4)."""

    name: str

    def reconstruct(self, frame_paths: list[Path]) -> Reconstruction: ...


@runtime_checkable
class SegmentationBackend(Protocol):
    """S4. Frames plus text prompts in, tracked instance masks out (3.5).

    Open-vocabulary and prompt-driven, so no custom detector needs training.
    `prompts` maps a label group to its prompt list.
    """

    name: str

    def segment(
        self, frame_paths: list[Path], prompts: dict[str, list[str]]
    ) -> list[InstanceMask]: ...


# 3.5 S4's three prompt groups, verbatim. This is the single place the
# removable/fixed policy is stated: `label_group` flows from here into
# `ObjectsFile.removable` and into whether the solver must route around
# something.
PROMPTS: dict[str, list[str]] = {
    "removable": [
        "sofa",
        "armchair",
        "chair",
        "table",
        "coffee table",
        "desk",
        "bed",
        "nightstand",
        "dresser",
        "bookshelf",
        "cabinet",
        "tv stand",
        "television",
        "rug",
        "lamp",
        "plant",
        "ottoman",
        "bench",
        "box",
        "bag",
        "clothes",
        "clutter",
    ],
    "structure": ["door", "window", "wall", "floor", "ceiling"],
    "fixed": [
        "radiator",
        "fireplace",
        "built-in shelving",
        "kitchen counter",
        "column",
        "stairs",
    ],
}

# 3.11: mirrors and large glass create phantom geometry. Prompted for so those
# points can be excluded rather than fitted as a room that is not there.
EXCLUSION_PROMPTS = ["mirror"]


def validate(reconstruction: Reconstruction) -> None:
    """Reject a reconstruction that cannot support a room, before S5 runs.

    3.4's failure detection. Catching it here rather than in S5 means the
    error names the real cause -- the reconstruction did not work -- instead
    of surfacing as a strange floor plan three stages later.
    """
    if reconstruction.frame_count < 2:
        raise PipelineError(
            "RECON_FAILED",
            Stage.RECONSTRUCT,
            f"{reconstruction.frame_count} poses is not a reconstruction",
        )
    if len(reconstruction.depth) != reconstruction.frame_count:
        raise PipelineError(
            "RECON_FAILED",
            Stage.RECONSTRUCT,
            f"{len(reconstruction.depth)} depth maps for {reconstruction.frame_count} poses",
        )
    if reconstruction.confidence.shape != reconstruction.depth.shape:
        raise PipelineError(
            "RECON_FAILED",
            Stage.RECONSTRUCT,
            "confidence and depth maps differ in shape",
        )
    if not np.isfinite(reconstruction.poses).all():
        raise PipelineError("RECON_FAILED", Stage.RECONSTRUCT, "poses contain NaN or inf")

    centres = reconstruction.camera_centres()
    extent = centres.max(axis=0) - centres.min(axis=0)
    span = float(np.linalg.norm(extent))
    if span <= 0:
        raise PipelineError("RECON_FAILED", Stage.RECONSTRUCT, "every camera is in the same place")

    # Collinear cameras: the smallest principal spread is a tiny fraction of
    # the largest. Depth perpendicular to that line is then unconstrained,
    # and the room comes out as a fan rather than a box.
    centred = centres - centres.mean(axis=0)
    singular = np.linalg.svd(centred, compute_uv=False)
    if singular[0] > 0 and float(singular[1] / singular[0]) < MIN_POSE_SPREAD_RATIO:
        raise PipelineError(
            "RECON_FAILED",
            Stage.RECONSTRUCT,
            f"camera path is effectively a straight line "
            f"(spread ratio {singular[1] / singular[0]:.4f}); "
            "there is no baseline to triangulate the room from",
        )


def backproject(
    reconstruction: Reconstruction,
    frame_index: int,
    *,
    pixel_mask: NDArray[np.bool_] | None = None,
    confidence_floor: float | None = None,
) -> NDArray[np.float64]:
    """One frame's depth to world points, in reconstruction units.

    OpenCV pinhole convention: +x right, +y down, +z forward. `pixel_mask`
    restricts the lift to one instance's pixels, which is how S5 step 1
    accumulates points per track.
    """
    depth = np.asarray(reconstruction.depth[frame_index], dtype=np.float64)
    confidence = np.asarray(reconstruction.confidence[frame_index], dtype=np.float64)
    height, width = depth.shape

    usable = np.isfinite(depth) & (depth > 0)
    if confidence_floor is not None:
        usable &= confidence >= confidence_floor
    if pixel_mask is not None:
        if pixel_mask.shape != depth.shape:
            raise ValueError(f"mask shape {pixel_mask.shape} does not match depth {depth.shape}")
        usable &= pixel_mask
    if not usable.any():
        return np.empty((0, 3), dtype=np.float64)

    v, u = np.nonzero(usable)
    z = depth[v, u]
    K = np.asarray(reconstruction.intrinsics[frame_index], dtype=np.float64)
    fx, fy, cx, cy = K[0, 0], K[1, 1], K[0, 2], K[1, 2]

    camera = np.stack([(u - cx) * z / fx, (v - cy) * z / fy, z], axis=1)
    pose = np.asarray(reconstruction.poses[frame_index], dtype=np.float64)
    world: NDArray[np.float64] = camera @ pose[:3, :3].T + pose[:3, 3]
    return world


def confidence_floor(reconstruction: Reconstruction) -> float:
    """3.4 step 4: the value below which the bottom 30% of points sit."""
    finite = reconstruction.confidence[np.isfinite(reconstruction.confidence)]
    if finite.size == 0:
        return 0.0
    return float(np.percentile(finite, CONFIDENCE_PERCENTILE))
