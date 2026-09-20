"""Geometric primitives for layout (implementation-plan.md 5.5).

5.5 names exactly two primitives: a 2D oriented rectangle plus a height
interval for items, and a polygon for the room. Everything the validator and
the solver do is expressed in those terms, which is what makes it feasible to
implement the same rules twice -- once here and once in TypeScript -- and get
identical answers.

**The rotation convention, stated once.** `rotation_deg` is a right-handed
rotation about **+Y**, which is what 2.4 specifies, what `three.js` does for
`object.rotation.y`, and what `align.yaw_rotation` does in the pipeline. In
the XZ plane that works out as:

    x' = x*cos(t) + z*sin(t)
    z' = -x*sin(t) + z*cos(t)

Note the sign: a *positive* rotation carries +X towards **-Z**, so a heading
measured as `atan2(z, x)` decreases. The other handedness -- the one that
falls out of writing the familiar 2D rotation matrix by reflex -- is wrong
here, and wrong in a way nothing downstream catches: every sofa faces 90
degrees from where it should, the layout still validates, and the error only
shows up as "why is the sofa facing the wall?" in the viewer.

**The 5 mm tolerance is load-bearing.** Coordinates are integer millimetres
and rotations are degrees, so a sofa placed flush against a wall lands a
fraction of a millimetre outside it after the trigonometry. Without a
tolerance the validator rejects its own solver's output; with one too large,
a real 4 mm overlap passes. 5 mm is the plan's number, applied in one place.

Parity note: every function here has a TypeScript twin in
`packages/geometry/`. Changing the maths on one side without the other breaks
the CI parity suite, which is the intended behaviour -- the AI and the editor
must agree on what "fits" means.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from shapely.geometry import Point, Polygon

# 5.5: "polygon containment with a tolerance of 5 mm".
TOLERANCE_MM = 5.0


def rotate_xz(x: float, z: float, degrees: float) -> tuple[float, float]:
    """Rotate an XZ point about +Y, right-handed. See the module docstring."""
    angle = math.radians(degrees)
    cos, sin = math.cos(angle), math.sin(angle)
    return (x * cos + z * sin, -x * sin + z * cos)


@dataclass(frozen=True, slots=True)
class Footprint:
    """An oriented rectangle on the floor, plus the height interval above it.

    `elevation_mm` is the bottom of the item: 0 for anything standing on the
    floor, non-zero for a wall-mounted shelf or a picture.
    """

    center_x_mm: float
    center_z_mm: float
    width_mm: float
    depth_mm: float
    height_mm: float
    rotation_deg: float = 0.0
    elevation_mm: float = 0.0

    @property
    def top_mm(self) -> float:
        return self.elevation_mm + self.height_mm

    def corners(self) -> NDArray[np.float64]:
        """The four XZ corners after rotation.

        Local axes before rotation: width along X, depth along Z, and the
        item's **front is -Z** (2.4).

        Written as one small matrix product rather than four calls to
        `rotate_xz`: the solver evaluates thousands of candidate poses and
        this was the single hottest line in the profile.
        """
        half_w, half_d = self.width_mm / 2.0, self.depth_mm / 2.0
        local = np.array(
            [[-half_w, -half_d], [half_w, -half_d], [half_w, half_d], [-half_w, half_d]],
            dtype=np.float64,
        )
        angle = math.radians(self.rotation_deg)
        cos, sin = math.cos(angle), math.sin(angle)
        # Right-handed about +Y; see the module docstring for the sign.
        rotation = np.array([[cos, -sin], [sin, cos]], dtype=np.float64)
        offset = np.array([self.center_x_mm, self.center_z_mm], dtype=np.float64)
        placed: NDArray[np.float64] = (local @ rotation) + offset
        return placed

    def polygon(self) -> Polygon:
        return Polygon(self.corners())

    def front_normal(self) -> tuple[float, float]:
        """The unit direction the item faces: local -Z, rotated.

        Used by S4 (an against-wall item whose back is not to the wall) and by
        the solver's intent scoring. A sign error here faces every sofa at the
        wall, which is a valid layout and an absurd one.
        """
        return rotate_xz(0.0, -1.0, self.rotation_deg)

    def height_overlaps(self, other: Footprint) -> bool:
        """Whether the two height intervals genuinely intersect.

        Tolerant on purpose: a rug 10 mm tall and a sofa whose legs start at
        exactly 10 mm are stacked, not colliding.
        """
        shared = min(self.top_mm, other.top_mm) - max(self.elevation_mm, other.elevation_mm)
        return shared > TOLERANCE_MM


def corners_of(f: Footprint) -> NDArray[np.float64]:
    """Free-function form of `Footprint.corners`, for callers that have one."""
    return f.corners()


def separating_axis_overlap(a: Footprint, b: Footprint) -> float:
    """Overlap depth between two oriented rectangles in mm; 0 means separate.

    The separating axis theorem: two convex shapes are disjoint exactly when
    some axis exists on which their projections do not overlap. For rectangles
    the only candidate axes are the edge normals, so checking four is
    exhaustive rather than approximate.

    Returning the *depth* rather than a boolean is what lets the UI say "the
    sofa overlaps the table by 40 mm", which 5.5 requires of every violation.
    """
    corners_a, corners_b = a.corners(), b.corners()
    smallest = math.inf

    for corners in (corners_a, corners_b):
        for index in range(2):  # a rectangle has two distinct edge directions
            edge = corners[(index + 1) % 4] - corners[index]
            axis = np.array([-edge[1], edge[0]], dtype=np.float64)
            norm = float(np.linalg.norm(axis))
            if norm < 1e-9:
                continue
            axis /= norm

            projected_a = corners_a @ axis
            projected_b = corners_b @ axis
            overlap = min(projected_a.max(), projected_b.max()) - max(
                projected_a.min(), projected_b.min()
            )
            if overlap <= 0:
                return 0.0
            smallest = min(smallest, overlap)

    return 0.0 if math.isinf(smallest) else float(smallest)


def gap_between(a: Footprint, b: Footprint) -> float:
    """Shortest distance between two footprints; 0 if they touch or overlap.

    Shapely rather than the separating axis, because for disjoint shapes the
    answer is a distance between edges and SAT does not give that: its
    per-axis separations are projections, and the largest understates a
    diagonal gap.
    """
    return float(a.polygon().distance(b.polygon()))


def protrusion(footprint: Footprint, room: Polygon) -> float:
    """How far the footprint sticks out of the room, in mm; 0 if inside.

    Measured from the worst corner, because a corner is a point and a point's
    distance to the boundary it escaped is exactly the number the user should
    read ("the bookshelf is 40 mm into the wall").

    The early `covers` is not a micro-optimisation. The solver calls this on
    every candidate pose and almost all of them are inside the room, so the
    fast path is the only path that runs at scale: one polygon test instead
    of four point constructions and four distance queries. It measured as
    the single largest cost in generating a layout.
    """
    shape = footprint.polygon()
    if room.covers(shape):
        return 0.0

    worst = 0.0
    for x, z in footprint.corners():
        point = Point(float(x), float(z))
        if not room.covers(point):
            worst = max(worst, float(room.exterior.distance(point)))
    return worst


def rectangle_from_wall(
    start: tuple[float, float],
    end: tuple[float, float],
    offset_mm: float,
    width_mm: float,
    depth_mm: float,
    *,
    height_mm: float = 1.0,
) -> Footprint:
    """A rectangle sitting against a wall, extending `depth_mm` into the room.

    Used for door keep-outs and window zones (5.2). "Into the room" is the
    wall's left normal, which for the counter-clockwise floor polygon the
    schema requires points inwards.

    Returns a `Footprint` rather than a polygon, deliberately. 5.5 says the
    validator has exactly two primitives, an oriented rectangle and the room;
    a keep-out expressed as a general polygon would be a third, and every
    operation on it would then need a polygon-clipping implementation that
    the TypeScript validator has to reproduce exactly. As a rectangle it is
    handled by the separating-axis test both sides already share.
    """
    dx = end[0] - start[0]
    dz = end[1] - start[1]
    length = math.hypot(dx, dz)
    if length < 1e-9:
        return Footprint(0.0, 0.0, 0.0, 0.0, 0.0)
    ux, uz = dx / length, dz / length
    nx, nz = -uz, ux  # left normal: into the room for a CCW polygon

    along = offset_mm + width_mm / 2.0
    out = depth_mm / 2.0
    return Footprint(
        center_x_mm=start[0] + ux * along + nx * out,
        center_z_mm=start[1] + uz * along + nz * out,
        width_mm=width_mm,
        depth_mm=depth_mm,
        height_mm=height_mm,
        # The rectangle's local +X runs along the wall. A +Y yaw of theta puts
        # local +X at heading -theta, hence the negation.
        rotation_deg=-math.degrees(math.atan2(uz, ux)) % 360.0,
    )


def distance_to_footprint(point: tuple[float, float], f: Footprint) -> float:
    """Distance from an XZ point to an oriented rectangle; 0 if inside.

    Computed in the rectangle's own frame, where the problem is the
    axis-aligned one. Exact, dependency-free, and identical in three lines of
    TypeScript -- which is the reason it exists rather than a shapely call:
    the circulation grid asks this question for every cell, and the browser
    has to get the same answer.
    """
    local_x, local_z = rotate_xz(
        point[0] - f.center_x_mm, point[1] - f.center_z_mm, -f.rotation_deg
    )
    dx = max(abs(local_x) - f.width_mm / 2.0, 0.0)
    dz = max(abs(local_z) - f.depth_mm / 2.0, 0.0)
    return math.hypot(dx, dz)


def distance_to_boundary(point: tuple[float, float], polygon: Polygon) -> float:
    """Distance from an XZ point to a polygon's boundary."""
    coords = list(polygon.exterior.coords)[:-1]
    best = math.inf
    for index in range(len(coords)):
        a = coords[index]
        b = coords[(index + 1) % len(coords)]
        best = min(best, _point_to_segment(point, (a[0], a[1]), (b[0], b[1])))
    return best


def _point_to_segment(
    p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
) -> float:
    vx, vz = b[0] - a[0], b[1] - a[1]
    length_squared = vx * vx + vz * vz
    if length_squared < 1e-12:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    t = max(0.0, min(1.0, ((p[0] - a[0]) * vx + (p[1] - a[1]) * vz) / length_squared))
    return math.hypot(p[0] - (a[0] + t * vx), p[1] - (a[1] + t * vz))
