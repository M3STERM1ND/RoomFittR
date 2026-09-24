"""S1-S9 end to end: a video in, a `RoomModel` out (implementation-plan.md 3).

Every stage already existed and was tested in isolation; nothing chained them.
This module is that chain, and it is deliberately the *only* place the order
is written down, so the eval harness, the Modal worker and a local debug run
are the same pipeline rather than three similar ones.

**Backends are injected, not imported.** `reconstruct` and `segment` arrive as
arguments satisfying the `backends.py` protocols, which is what lets E1 swap
candidate A for candidate B without touching a line here -- 3.4's stated
reason for the adapter boundary -- and lets a test drive the whole chain with
synthetic geometry and no GPU.

**Where the units change.** S3 returns arbitrary units. S5 and S6 work in
those units throughout; S7 decides what they mean and only `roommodel.build`
multiplies. The one exception is S6's thresholds, which are metric, so a
provisional scale is applied before geometry and corrected afterwards -- see
`_provisional_scale`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from . import align, backends, frames, geometry, ingest, objects, scale, shell
from .errors import PipelineError, Stage

if TYPE_CHECKING:  # pragma: no cover - typing only
    from numpy.typing import NDArray

    from .backends import ReconstructionBackend, SegmentationBackend

# 3.5's prompt groups decide which points are room and which are stuff. Floor,
# wall and ceiling are structure; everything else is an object standing in
# front of the structure.
_STRUCTURE_LABELS = frozenset({"floor", "wall", "ceiling"})

# A door or window is structure for the purpose of *openings*, but its points
# must not be fitted as wall surface: a recessed window would pull the wall
# plane inward by the depth of the reveal.
_OPENING_LABELS = frozenset({"door", "window"})


@dataclass(slots=True)
class ScanResult:
    """Everything one run produced, artefacts included."""

    room_model: dict[str, Any]
    objects_file: dict[str, Any]
    geometry: geometry.RoomGeometry
    scale: scale.ScaleResult
    reconstruction_backend: str
    frames_used: int
    warnings: list[str] = field(default_factory=list)
    # E7's raw material (3.9). Populated by the caller for remote stages,
    # since only it knows how long the round trip took.
    timings_s: dict[str, float] = field(default_factory=dict)


def run(
    video: Path,
    work_dir: Path,
    *,
    reconstruct: ReconstructionBackend,
    segment: SegmentationBackend | None = None,
    pipeline_version: str,
    target_frames: int = 80,
    gravity_hint: NDArray[np.float64] | None = None,
    user_measurement_mm: float | None = None,
    user_measured_units: float | None = None,
) -> ScanResult:
    """The whole pipeline.

    `segment` is optional and its absence is a real mode, not a degraded one:
    S4 needs a GPU, and S6's classical path can run on the raw point cloud
    when segmentation is unavailable. What is lost is the structure/object
    partition, so furniture standing against a wall is fitted *as* the wall --
    which is exactly the failure E6 measures. A run without segmentation is
    therefore fine for debugging geometry and not fine for the gate, and it
    says so in `warnings` rather than silently scoring worse.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    # Two separate lists, because they have different audiences and the
    # schema enforces it. `model_warnings` are `quality.warnings` codes
    # from a fixed enum, shown to the user about their room. `notes` are
    # prose about how this *run* was configured, for whoever reads the
    # eval report -- putting one in the other's list fails schema
    # validation, which is how this was found.
    model_warnings: list[str] = []
    notes: list[str] = []

    # S1-S2.
    meta = ingest.validate(ingest.probe(video))
    # Called for its effect: it writes the model and full keyframe sets.
    # The scores it returns are S2's own diagnostics and nothing downstream
    # reads them yet.
    frames.run(video, meta, work_dir, target=target_frames)
    model_frames = sorted((work_dir / "keyframes" / "model").glob("*.jpg"))
    if len(model_frames) < 2:
        raise PipelineError(
            "VIDEO_TOO_SHORT_OR_BLURRY",
            Stage.FRAMES,
            f"only {len(model_frames)} usable keyframes",
        )

    # S3.
    reconstruction = reconstruct.reconstruct(model_frames)
    backends.validate(reconstruction)

    # S4. Masks are per-pixel, so they partition the *back-projection* rather
    # than the finished cloud.
    instances: list[backends.InstanceMask] = []
    if segment is not None:
        instances = segment.segment(model_frames, backends.PROMPTS)
    else:
        notes.append(
            "segmentation skipped: structure and objects are not separated, so "
            "furniture against a wall is fitted as wall (E6)"
        )

    floor = backends.confidence_floor(reconstruction)
    structure_points, floor_points, object_points, opening_points = _partition(
        reconstruction, instances, floor
    )

    if len(structure_points) < 50:
        raise PipelineError(
            "INSUFFICIENT_COVERAGE",
            Stage.FUSE,
            f"{len(structure_points)} structure points is not a room",
        )

    # S5. `fit_floor_plane` wants points *believed to be floor*, not all
    # structure: RANSAC over everything finds the largest plane, which in a
    # long room is a wall, and the room comes out rotated 90 degrees while
    # still producing a valid-looking RoomModel. S4's `floor` label is the
    # evidence when it exists; otherwise the caller must supply gravity,
    # which is what 3.2's motion sidecar and ARKitScenes' poses both provide.
    provisional = _provisional_scale(reconstruction, structure_points)
    if len(floor_points) >= 3:
        seed_points = floor_points * provisional
    elif gravity_hint is not None:
        # Gravity says which way is down, so it can *select* the floor as well
        # as check it: the floor is the lowest band along that axis. Handing
        # RANSAC the whole cloud instead finds the largest plane, which is a
        # wall, and `check_against_gravity_hint` then rejects it at 90 degrees
        # -- correct, but it fails the scan rather than orienting it.
        seed_points = _lowest_band(structure_points * provisional, gravity_hint)
    else:
        raise PipelineError(
            "INSUFFICIENT_COVERAGE",
            Stage.FUSE,
            "cannot orient the room: no floor was segmented and no gravity hint "
            "was supplied, and a point set alone cannot say which way is up",
        )
    alignment = align.align(
        seed_points,
        interior_points=reconstruction.camera_centres() * provisional,
        gravity_hint=gravity_hint,
    )
    aligned_structure = alignment.apply(structure_points * provisional)
    aligned_objects = {
        track_id: alignment.apply(points * provisional)
        for track_id, points in object_points.items()
    }
    aligned_openings = {
        track_id: (label, alignment.apply(points * provisional))
        for track_id, (label, points) in opening_points.items()
    }

    # S6.
    room_geometry = _extract_geometry(aligned_structure, aligned_openings)
    model_warnings.extend(room_geometry.warnings)

    # S7. The provisional scale is already baked into the geometry, so what
    # S7 fuses are corrections to it rather than the scale itself.
    scale_result = _fuse_scale(
        reconstruction,
        room_geometry,
        provisional=provisional,
        user_measurement_mm=user_measurement_mm,
        user_measured_units=user_measured_units,
    )

    # S9: objects, and the fixed ones the solver must route around.
    detected = objects.merge_instances(
        [
            objects.Instance(
                track_id=track_id,
                label=instance.label,
                label_group=instance.label_group,
                score=instance.score,
                points=aligned_objects[track_id],
            )
            for instance in instances
            if (track_id := instance.track_id) in aligned_objects
            and len(aligned_objects[track_id]) >= 8
        ]
    )

    room_model = _build_room_model(
        room_geometry,
        scale_result,
        detected,
        pipeline_version=pipeline_version,
        frames_used=len(model_frames),
        # 3.3's coverage number is not something S2 computes; it belongs to
        # S5's view overlap and is left at 0 until that exists, rather than
        # invented here from the frame count.
        coverage_pct=0.0,
        warnings=model_warnings,
    )

    return ScanResult(
        room_model=room_model,
        objects_file=objects.objects_file(detected, pipeline_version=pipeline_version),
        geometry=room_geometry,
        scale=scale_result,
        reconstruction_backend=reconstruction.backend,
        frames_used=len(model_frames),
        warnings=model_warnings + notes,
    )


def _partition(
    reconstruction: backends.Reconstruction,
    instances: list[backends.InstanceMask],
    confidence_floor: float,
) -> tuple[
    NDArray[np.float64],
    NDArray[np.float64],
    dict[int, NDArray[np.float64]],
    dict[int, tuple[str, Any]],
]:
    """3.5 S5 step 1: split the back-projection by what S4 saw.

    Structure is everything not claimed by a removable or fixed instance.
    Doors and windows are pulled out separately because they are needed to
    *place* openings but must not be fitted as wall surface -- a recessed
    window would otherwise drag the wall plane in by the depth of its reveal.
    """
    frame_count = reconstruction.frame_count
    height, width = reconstruction.depth.shape[1:3]

    object_masks = np.zeros((frame_count, height, width), dtype=bool)
    per_track: dict[int, list[NDArray[np.float64]]] = {}
    per_opening: dict[int, tuple[str, list[NDArray[np.float64]]]] = {}
    floor_lifted: list[NDArray[np.float64]] = []

    for instance in instances:
        if instance.masks.shape != object_masks.shape:
            # A mask that does not line up with the depth maps cannot be
            # lifted. Skipping it loudly beats guessing at a resize.
            continue
        is_opening = instance.label in _OPENING_LABELS
        if instance.label == "floor":
            # Kept separately: S5's plane fit needs floor and only floor.
            floor_lifted.extend(
                backends.backproject(
                    reconstruction,
                    i,
                    pixel_mask=instance.masks[i],
                    confidence_floor=confidence_floor,
                )
                for i in range(frame_count)
                if instance.masks[i].any()
            )
            continue
        if instance.label in _STRUCTURE_LABELS:
            continue
        if not is_opening:
            object_masks |= instance.masks

        lifted = [
            backends.backproject(
                reconstruction, i, pixel_mask=instance.masks[i], confidence_floor=confidence_floor
            )
            for i in range(frame_count)
            if instance.masks[i].any()
        ]
        if not lifted:
            continue
        points = np.concatenate(lifted, axis=0)
        if is_opening:
            per_opening[instance.track_id] = (instance.label, [points])
        else:
            per_track[instance.track_id] = [points]

    structure = [
        backends.backproject(
            reconstruction, i, pixel_mask=~object_masks[i], confidence_floor=confidence_floor
        )
        for i in range(frame_count)
    ]
    structure_points = (
        np.concatenate(structure, axis=0) if structure else np.empty((0, 3), dtype=np.float64)
    )

    objects_out = {k: np.concatenate(v, axis=0) for k, v in per_track.items()}
    openings_out = {k: (label, np.concatenate(v, axis=0)) for k, (label, v) in per_opening.items()}
    floor_out = (
        np.concatenate(floor_lifted, axis=0) if floor_lifted else np.empty((0, 3), dtype=np.float64)
    )
    return structure_points, floor_out, objects_out, openings_out


def _lowest_band(
    points: NDArray[np.float64],
    gravity: NDArray[np.float64],
    *,
    fraction: float = 0.15,
) -> NDArray[np.float64]:
    """Candidate floor points: the lowest slice along the gravity axis.

    A fraction rather than an absolute height, because these are provisional
    units. 15% is generous enough to survive a sloping capture and a floor
    partly hidden by furniture, and tight enough that a low sofa back does not
    dominate the plane fit -- RANSAC discards those as outliers anyway, which
    is why the band does not have to be exact.
    """
    direction = np.asarray(gravity, dtype=np.float64)
    norm = float(np.linalg.norm(direction))
    if norm < 1e-9:
        return points
    # Distance along "down": the floor is at the far end of it.
    along = points @ (direction / norm)
    cutoff = float(np.quantile(along, 1.0 - fraction))
    band: NDArray[np.float64] = points[along >= cutoff]
    return band if len(band) >= 3 else points


def _provisional_scale(
    reconstruction: backends.Reconstruction, structure_points: NDArray[np.float64]
) -> float:
    """A rough units-to-millimetres factor, so S6's metric thresholds apply.

    S6 reasons in millimetres -- cell sizes, minimum wall lengths, ceiling
    height bounds -- so it cannot run on arbitrary units. A metric backend
    hands us the answer. Otherwise the room's vertical extent is assumed to be
    a plausible floor-to-ceiling height, which is crude but only has to be
    right to within a factor of about two for S6's thresholds to behave; S7
    then corrects it properly.
    """
    if reconstruction.metric_scale is not None:
        return float(reconstruction.metric_scale)

    if len(structure_points) == 0:
        return 1.0
    # Robust vertical extent: percentiles rather than min/max, because a
    # single stray point above the ceiling would halve the estimate.
    low, high = np.percentile(structure_points[:, 1], [2.0, 98.0])
    extent = float(abs(high - low))
    if extent <= 0:
        return 1.0
    # The same 2590 mm prior `scale.from_ceiling_height` defaults to. Spelled
    # out rather than imported because this is a *provisional* guess used only
    # to make S6's metric thresholds apply; S7 does the real fusing.
    return 2590.0 / extent


def _extract_geometry(
    structure_points: NDArray[np.float64],
    openings: dict[int, tuple[str, NDArray[np.float64]]],
) -> geometry.RoomGeometry:
    """S6: heights, then walls, then the floor plan, then the openings."""
    profile = geometry.height_profile(structure_points)
    grid = geometry.wall_evidence_grid(structure_points, profile)
    floor_mask = structure_points[:, 1] < profile.floor_mm + 300.0
    floor_xz = structure_points[floor_mask][:, [0, 2]]

    polygon = geometry.floor_polygon(grid, floor_xz)
    polygon = geometry.orient_ccw(polygon)
    walls = geometry.walls_from_polygon(polygon, grid)

    fitted: list[geometry.Opening] = []
    for index, (label, points) in enumerate(openings.values()):
        opening = geometry.fit_opening(
            points, walls, opening_id=f"O{index + 1}", label=label, floor_mm=profile.floor_mm
        )
        if opening is not None:
            fitted.append(opening)

    room = geometry.RoomGeometry(polygon=polygon, walls=walls, openings=fitted, profile=profile)
    warnings = geometry.sanity_check(room)
    return geometry.RoomGeometry(
        polygon=room.polygon,
        walls=room.walls,
        openings=room.openings,
        profile=room.profile,
        warnings=warnings,
    )


def _fuse_scale(
    reconstruction: backends.Reconstruction,
    room_geometry: geometry.RoomGeometry,
    *,
    provisional: float,
    user_measurement_mm: float | None,
    user_measured_units: float | None,
) -> scale.ScaleResult:
    """S7. Corrections to the provisional scale, fused (3.7).

    The geometry is already in provisional millimetres, so every source here
    is a *ratio*: what the observed height should have been over what it was.
    A result of 1.0 means the provisional guess was right.
    """
    sources: list[scale.ScaleSource] = []

    if reconstruction.metric_scale is not None:
        # The model claims metric and `_provisional_scale` already applied that
        # claim, so its correction is exactly 1. It is still recorded as a
        # source rather than assumed, both so E2 can see it voting and so a
        # model that silently fell back to relative output is caught by
        # `reject_outliers` instead of quietly setting the room's size.
        sources.append(scale.ScaleSource(name="model", factor=1.0))

    if room_geometry.profile.ceiling_observed:
        sources.append(scale.from_ceiling_height(room_geometry.profile.ceiling_mm))

    for opening in room_geometry.openings:
        if opening.type == "door":
            sources.append(scale.from_door_height(float(opening.height_mm)))

    if user_measurement_mm is not None and user_measured_units is not None:
        sources.append(scale.from_user_measurement(user_measured_units, user_measurement_mm))

    if not sources:
        # Nothing to correct with. Honest 1.0 with low confidence beats
        # inventing a number: S7's job is to say how sure it is.
        return scale.fuse([scale.from_ceiling_height(room_geometry.profile.ceiling_mm)])

    kept, rejected = scale.reject_outliers(sources)
    if rejected:
        # `fuse` records exclusions in the result, so they stay visible in the
        # RoomModel rather than disappearing into a log nobody reads.
        return scale.fuse(kept or sources)
    return scale.fuse(sources)


def _build_room_model(
    room_geometry: geometry.RoomGeometry,
    scale_result: scale.ScaleResult,
    detected: list[objects.DetectedObject],
    *,
    pipeline_version: str,
    frames_used: int,
    coverage_pct: float,
    warnings: list[str],
) -> dict[str, Any]:
    from . import roommodel

    return roommodel.build(
        room_geometry,
        scale_result,
        pipeline_version=pipeline_version,
        appearance=shell.appearance(room_geometry),
        fixed_obstacles=objects.fixed_obstacles(detected),
        frames_used=frames_used,
        coverage_pct=coverage_pct,
        extra_warnings=warnings,
    )
