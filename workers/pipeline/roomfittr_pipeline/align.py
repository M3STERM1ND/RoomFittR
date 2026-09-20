"""S5 alignment: get the room level, then get it square (implementation-plan.md 3.5).

A reconstruction arrives in the model's own frame, which has no particular
relationship to the room. Two rotations fix that:

1. **Gravity.** Find the floor and make its normal +Y, so the floor plane is
   y = 0 and heights mean something.
2. **Manhattan.** Rooms are mostly rectilinear. Find the dominant wall
   direction and rotate it onto +X, so `floor_polygon` comes out axis-aligned
   and the solver's {0, 90, 180, 270} rotations line up with the walls.

**Why this module is paranoid.** Phase 0 shipped an evaluation adapter that
assumed the wrong up-axis. It did not crash; it produced rooms that passed
schema validation and were simply the wrong size and shape, and it survived
review because a 3.6 m ceiling over 5.6 m2 looks unusual rather than broken.
A wrong up-axis is not a loud failure, so every function here either returns
a result it can justify or raises. None of them guess.

Vector conventions: points are (N, 3) float arrays. Rotations are 3x3 and are
applied as `points @ R.T`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from .errors import PipelineError, Stage

Points = NDArray[np.float64]

# A floor is flat to within a centimetre or so over the span we care about.
# RANSAC's inlier band is wider than that to absorb reconstruction noise,
# which on monocular depth is a good deal worse than the floor's own
# flatness.
FLOOR_INLIER_MM = 50.0
FLOOR_RANSAC_ITERATIONS = 200

# A plane fitted to fewer points than this is not evidence of anything.
MIN_PLANE_POINTS = 50

# The floor must be more horizontal than this to be believed. 25 degrees is
# deliberately loose: it is not a claim about floors, it is a guard against
# fitting a *wall* and calling it the floor, and walls are 90 degrees away.
MAX_FLOOR_TILT_DEG = 25.0

# Gravity from the phone's accelerometer (the 3.2 motion sidecar) and gravity
# from the fitted floor should agree closely. Past this they disagree about
# which way is up, and the safe move is to say so rather than to pick one.
GRAVITY_DISAGREEMENT_DEG = 20.0

# Manhattan direction search: 0.5 degree bins over the 90-degree wedge.
# Finer than the angular error we can expect from monocular reconstruction,
# so the bin width is not the limiting factor.
MANHATTAN_BIN_DEG = 0.5

# A wall normal that is not roughly horizontal belongs to a floor, a ceiling
# or a sloped surface, and says nothing about the room's yaw.
MAX_WALL_NORMAL_TILT_DEG = 30.0


@dataclass(frozen=True, slots=True)
class GravityFit:
    """The floor plane, in the frame the points arrived in."""

    # Unit normal pointing *up*, i.e. into the room.
    normal: Points
    # Signed plane offset: the plane is {p : normal . p + offset = 0}.
    offset_mm: float
    inlier_count: int
    inlier_fraction: float
    source: str

    def height_of(self, points: Points) -> NDArray[np.float64]:
        """Signed height above the plane, in mm. Positive is into the room."""
        return np.asarray(points, dtype=np.float64) @ self.normal + self.offset_mm


def _unit(v: Points) -> Points:
    norm = float(np.linalg.norm(v))
    if norm < 1e-12:
        raise ValueError("cannot normalise a zero-length vector")
    return np.asarray(v, dtype=np.float64) / norm


def angle_between_deg(a: Points, b: Points) -> float:
    """Unsigned angle between two vectors, in degrees."""
    cos = float(np.clip(np.dot(_unit(a), _unit(b)), -1.0, 1.0))
    return math.degrees(math.acos(cos))


def fit_floor_plane(
    floor_points: Points,
    *,
    interior_points: Points | None = None,
    gravity_hint: Points | None = None,
    rng_seed: int = 0,
) -> GravityFit:
    """RANSAC a plane through points believed to be floor.

    **One of `interior_points` or `gravity_hint` is required**, and the
    requirement is the interesting part of this signature. A plane fitted to
    floor points alone is perfectly symmetric: nothing in that point set says
    which of its two normals points into the room. Defaulting to "whichever
    is closer to +Y" would be a guess, and a wrong guess here turns the room
    upside down while still producing a valid-looking `RoomModel` -- the exact
    shape of the bug Phase 0 shipped in the evaluation adapter. So the caller
    has to supply the evidence:

    - `interior_points`: any other structure points (walls, ceiling, objects).
      The room is above its floor, so the side holding them is up. This is
      free -- S5 already has them.
    - `gravity_hint`: the accelerometer's down-vector from the 3.2 motion
      sidecar, when the user granted permission. Good to a few degrees, and
      it says which way is down but nothing about where the floor is, which
      is why it orients the fit rather than replacing it.

    Seeded RNG, because two runs over one scan must produce one room.
    """
    if interior_points is None and gravity_hint is None:
        raise ValueError(
            "fit_floor_plane needs interior_points or gravity_hint to orient the "
            "normal; a floor-only point set cannot say which way is up"
        )

    points = np.asarray(floor_points, dtype=np.float64)
    if points.ndim != 2 or points.shape[1] != 3:
        raise ValueError(f"expected (N, 3) points, got {points.shape}")
    if len(points) < MIN_PLANE_POINTS:
        raise PipelineError(
            "INSUFFICIENT_COVERAGE",
            Stage.FUSE,
            f"{len(points)} floor points is below the {MIN_PLANE_POINTS} minimum",
        )

    rng = np.random.default_rng(rng_seed)
    best_normal: Points | None = None
    best_offset = 0.0
    best_inliers = 0

    for _ in range(FLOOR_RANSAC_ITERATIONS):
        sample = points[rng.choice(len(points), size=3, replace=False)]
        edge_a = sample[1] - sample[0]
        edge_b = sample[2] - sample[0]
        cross = np.cross(edge_a, edge_b)
        norm = float(np.linalg.norm(cross))
        if norm < 1e-9:  # collinear sample, no plane
            continue
        normal = cross / norm
        offset = -float(normal @ sample[0])

        inliers = int(np.count_nonzero(np.abs(points @ normal + offset) <= FLOOR_INLIER_MM))
        if inliers > best_inliers:
            best_normal, best_offset, best_inliers = normal, offset, inliers

    if best_normal is None:
        raise PipelineError(
            "INSUFFICIENT_COVERAGE", Stage.FUSE, "no plane could be fitted to the floor points"
        )

    # Refit on the inliers. The RANSAC winner is defined by three points and
    # inherits their noise; least squares over every inlier is a much better
    # plane and costs one SVD.
    inlier_mask = np.abs(points @ best_normal + best_offset) <= FLOOR_INLIER_MM
    inlier_points = points[inlier_mask]
    centroid = inlier_points.mean(axis=0)
    # Smallest singular vector of the centred inliers is the plane normal.
    _, _, vt = np.linalg.svd(inlier_points - centroid, full_matrices=False)
    normal = _unit(vt[-1])
    offset = -float(normal @ centroid)

    # Orient the normal into the room. Gravity is the stronger signal when we
    # have it; otherwise the interior points decide, since the room is above
    # its own floor. The median is used rather than the mean so a handful of
    # stray points below the floor plane cannot flip the room over.
    if gravity_hint is not None:
        if float(normal @ _unit(gravity_hint)) > 0:
            normal, offset = -normal, -offset
        source = "floor_plane+imu"
    else:
        interior = np.asarray(interior_points, dtype=np.float64)
        if len(interior) == 0:
            raise ValueError("interior_points is empty; it cannot orient the floor normal")
        if float(np.median(interior @ normal + offset)) < 0:
            normal, offset = -normal, -offset
        source = "floor_plane+interior"

    return GravityFit(
        normal=normal,
        offset_mm=offset,
        inlier_count=int(inlier_mask.sum()),
        inlier_fraction=float(inlier_mask.mean()),
        source=source,
    )


def check_against_gravity_hint(fit: GravityFit, gravity_hint: Points) -> float:
    """Cross-check the fitted up-axis against the accelerometer. Returns the angle.

    Two independent instruments. When they disagree by more than a wide
    tolerance, one of them is measuring something else -- most often the
    "floor" fit has latched onto a table top or a bed. Raising here is the
    whole lesson of Phase 0's adapter bug: a wrong up-axis produces a
    plausible room, so it has to be caught at the point of measurement.
    """
    up_from_imu = -_unit(gravity_hint)
    disagreement = angle_between_deg(fit.normal, up_from_imu)
    if disagreement > GRAVITY_DISAGREEMENT_DEG:
        raise PipelineError(
            "INSUFFICIENT_COVERAGE",
            Stage.FUSE,
            f"fitted floor normal is {disagreement:.1f} deg from device gravity; "
            "the surface found is probably not the floor",
        )
    return disagreement


def rotation_onto_y(up: Points) -> NDArray[np.float64]:
    """Rotation taking `up` to +Y, about the axis perpendicular to both.

    This is the minimal rotation, which matters: any rotation mapping `up` to
    +Y differs from this one by a spin about Y, and an arbitrary spin would
    scramble the wall directions that the Manhattan step is about to measure.
    """
    source = _unit(up)
    target = np.array([0.0, 1.0, 0.0])
    axis = np.cross(source, target)
    axis_norm = float(np.linalg.norm(axis))

    if axis_norm < 1e-9:
        # Already parallel: identity, or a 180 degree flip about any
        # perpendicular axis.
        if float(source @ target) > 0:
            return np.eye(3, dtype=np.float64)
        return np.diag(np.array([1.0, -1.0, -1.0]))

    axis = axis / axis_norm
    angle = math.acos(float(np.clip(source @ target, -1.0, 1.0)))
    kx, ky, kz = axis
    K = np.array([[0.0, -kz, ky], [kz, 0.0, -kx], [-ky, kx, 0.0]], dtype=np.float64)
    rotation: NDArray[np.float64] = np.eye(3, dtype=np.float64) + math.sin(angle) * K
    rotation += (1.0 - math.cos(angle)) * (K @ K)
    return rotation


def yaw_rotation(degrees: float) -> NDArray[np.float64]:
    """Rotation about +Y by `degrees`, right-handed (2.4).

    Mind the sign when using this to cancel a measured heading. In a
    right-handed Y-up frame, rotating by +theta carries +X towards -Z, so a
    heading read as `atan2(z, x)` comes out *reduced* by theta. To square a
    room whose walls sit at heading `yaw`, apply `yaw_rotation(+yaw)`, not
    the negation that reads more naturally.
    """
    radians = math.radians(degrees)
    cos, sin = math.cos(radians), math.sin(radians)
    return np.array([[cos, 0.0, sin], [0.0, 1.0, 0.0], [-sin, 0.0, cos]], dtype=np.float64)


@dataclass(frozen=True, slots=True)
class ManhattanFit:
    """The room's dominant wall direction, as a yaw to rotate away."""

    yaw_deg: float
    # Fraction of wall normals that fall within a bin of the winner, modulo
    # 90 degrees. Low means the room is not rectilinear (or the normals are
    # noise), and the caller should expect a warning rather than a clean
    # polygon.
    support: float
    normals_used: int


def dominant_wall_yaw(wall_normals: Points, *, bin_deg: float = MANHATTAN_BIN_DEG) -> ManhattanFit:
    """Find the yaw that squares the walls to the axes.

    Wall normals are projected onto the floor plane (their Y component is
    dropped) and their headings histogrammed **modulo 90 degrees**, which is
    the trick that makes this work: the four walls of a rectangular room point
    in four directions 90 degrees apart, and all four land in the same bin.

    Expects points already gravity-aligned, so +Y is up.
    """
    normals = np.asarray(wall_normals, dtype=np.float64)
    if normals.ndim != 2 or normals.shape[1] != 3:
        raise ValueError(f"expected (N, 3) normals, got {normals.shape}")

    # Drop normals that are not roughly horizontal: they are floor, ceiling or
    # something sloped, and their heading is meaningless.
    horizontal_component = np.linalg.norm(normals[:, [0, 2]], axis=1)
    tilt_ok = horizontal_component > math.cos(math.radians(90.0 - MAX_WALL_NORMAL_TILT_DEG))
    usable = normals[tilt_ok]
    if len(usable) < MIN_PLANE_POINTS:
        raise PipelineError(
            "INSUFFICIENT_COVERAGE",
            Stage.FUSE,
            f"{len(usable)} horizontal wall normals is too few to square the room",
        )

    headings = np.degrees(np.arctan2(usable[:, 2], usable[:, 0])) % 90.0

    # Circular histogram over [0, 90). A plain argmax would split a peak that
    # straddles the wrap-around at 0/90 -- the common case, since a room whose
    # walls are nearly axis-aligned already has its normals sitting right on
    # the boundary.
    bins = int(round(90.0 / bin_deg))
    counts, edges = np.histogram(headings, bins=bins, range=(0.0, 90.0))
    # Smooth with a 3-bin circular window so a peak spread across two
    # adjacent bins by noise still wins against an unsmoothed neighbour.
    smoothed = counts + np.roll(counts, 1) + np.roll(counts, -1)
    winner = int(np.argmax(smoothed))

    # Refine to the mean heading of the winning bin and its neighbours,
    # unwrapped around the winner so the wrap-around does not drag the mean
    # to the middle of the range.
    centre = (edges[winner] + edges[winner + 1]) / 2.0
    delta = (headings - centre + 45.0) % 90.0 - 45.0
    near = np.abs(delta) <= bin_deg * 1.5
    yaw = float(centre + delta[near].mean()) if near.any() else float(centre)

    return ManhattanFit(
        yaw_deg=yaw % 90.0,
        support=float(smoothed[winner] / max(len(usable), 1)),
        normals_used=int(len(usable)),
    )


@dataclass(frozen=True, slots=True)
class Alignment:
    """The full rotation from reconstruction frame to room frame."""

    rotation: NDArray[np.float64]
    gravity: GravityFit
    manhattan: ManhattanFit | None
    # Translation applied after rotation, putting the floor at y = 0. The XZ
    # origin is set later, in S6, once the floor polygon exists (2.4 puts it
    # at the polygon centroid, which is not known yet).
    floor_offset_mm: float

    def apply(self, points: Points) -> Points:
        """Rotate and drop points into the room frame."""
        rotated: Points = np.asarray(points, dtype=np.float64) @ self.rotation.T
        rotated[:, 1] -= self.floor_offset_mm
        return rotated


def align(
    floor_points: Points,
    wall_normals: Points | None = None,
    *,
    interior_points: Points | None = None,
    gravity_hint: Points | None = None,
    rng_seed: int = 0,
) -> Alignment:
    """S5 steps 3 and 4: level the room, then square it.

    Manhattan alignment is skipped when there are too few wall normals to
    support it. That is not a failure -- an L-shaped or partly-scanned room
    still produces a valid `RoomModel`, it just is not axis-aligned, and S6
    is written to cope. Forcing a yaw from six noisy normals would be worse
    than leaving it.

    Levelling, by contrast, is not optional: see `fit_floor_plane` for why
    one of `interior_points` or `gravity_hint` must be supplied.
    """
    gravity = fit_floor_plane(
        floor_points,
        interior_points=interior_points,
        gravity_hint=gravity_hint,
        rng_seed=rng_seed,
    )
    if gravity_hint is not None:
        check_against_gravity_hint(gravity, gravity_hint)

    to_y = rotation_onto_y(gravity.normal)

    manhattan: ManhattanFit | None = None
    rotation = to_y
    if wall_normals is not None and len(wall_normals) >= MIN_PLANE_POINTS:
        try:
            levelled_normals = np.asarray(wall_normals, dtype=np.float64) @ to_y.T
            manhattan = dominant_wall_yaw(levelled_normals)
            # Positive, not negative: see `yaw_rotation`. Negating here
            # doubles the room's yaw instead of cancelling it, and the result
            # is a valid `RoomModel` of a room rotated the wrong way.
            rotation = yaw_rotation(manhattan.yaw_deg) @ to_y
        except PipelineError:
            # Not enough horizontal normals survived. Level but unsquared is
            # a worse room, not a broken one.
            manhattan = None

    # Where the floor plane sits after rotation. Computed from the plane
    # rather than from the points so a sparse floor does not bias it.
    floor_point_in_plane = -gravity.offset_mm * gravity.normal
    floor_offset = float((floor_point_in_plane @ rotation.T)[1])

    return Alignment(
        rotation=rotation,
        gravity=gravity,
        manhattan=manhattan,
        floor_offset_mm=floor_offset,
    )
