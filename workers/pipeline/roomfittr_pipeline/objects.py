"""S5 step 6: detected instances as oriented bounding boxes (3.5).

What comes out of segmentation is a set of point clusters, one per tracked
instance. What the rest of the system needs is a box: the viewer draws ghosts
of what was removed, and the layout engine treats anything the user kept as an
obstacle it must not place into.

Three jobs:

1. **Trim.** A segmentation mask bleeds onto whatever is behind the object,
   so the tails of a cluster are wall and floor. 3.5 specifies the 2nd-98th
   percentile, and it matters more than it sounds: untrimmed, a sofa's box
   commonly reaches the far wall.
2. **Fit.** A minimum-area rectangle on the XZ projection gives position,
   footprint and yaw in one step, and gravity alignment from S5 means the
   vertical extent is just the Y range.
3. **Merge.** One physical object often becomes several tracks -- it left the
   frame and came back, or the user walked round it. 3.5 merges tracks with
   3D IoU > 0.3 in the same label group; `MERGE_CONTAINMENT` explains why a
   second criterion is needed alongside it for partial views.

The label *group*, not the label, decides `removable` (see the schema). That
keeps one list of prompts in S4 in charge of the policy, rather than scattering
"is a radiator furniture?" decisions through the code.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from .align import Points

# 3.5: trim the tails before fitting. Mask edges are bleed onto the background.
TRIM_PERCENTILE = (2.0, 98.0)

# Below this an instance is noise, not an object.
MIN_INSTANCE_POINTS = 30

# 3.5: merge tracks whose boxes overlap this much and share a label group.
MERGE_IOU = 0.3

# Containment, as a second merge criterion alongside 3.5's IoU.
#
# IoU alone does not do the job the plan asks of it ("merge tracks that refer
# to the same physical object"). A partial view is *contained* in the full
# view rather than similar to it, and containment drives IoU down: a 0.4 m
# glimpse of a 2 m sofa scores 0.09, far under the 0.3 threshold, so the
# glimpse survives as a second object sitting inside the first. The viewer
# then draws two ghosts and the keep-or-remove UI asks about the same sofa
# twice.
#
# Merging on containment is safe in a way merging on proximity would not be,
# because a box wholly inside another box is not a neighbouring object. The
# cases it does catch besides partial views -- cushions on a sofa, books on a
# shelf -- are things the user wants removed along with their container
# anyway. E6 is where this gets measured against real annotations.
MERGE_CONTAINMENT = 0.8

# Anything smaller than this in every dimension is not worth modelling and
# not worth showing the user a "keep or remove" card for. E6 measures recall
# of furniture 400 mm and larger, so this sits comfortably below the bar.
MIN_OBJECT_EXTENT_MM = 150.0

# The three prompt groups of 3.5 S4. `removable` is derived from this.
REMOVABLE_GROUP = "removable"
STRUCTURE_GROUP = "structure"
FIXED_GROUP = "fixed"


@dataclass(frozen=True, slots=True)
class OrientedBox:
    """A gravity-aligned box: free yaw about +Y, level in the other two axes."""

    center: tuple[float, float, float]
    size: tuple[float, float, float]
    yaw_deg: float

    @property
    def footprint_area_mm2(self) -> float:
        return self.size[0] * self.size[1]

    def corners_xz(self) -> NDArray[np.float64]:
        """The footprint's four corners, in order, for overlap tests.

        `yaw_deg` is a right-handed rotation about +Y, matching 2.4,
        `align.yaw_rotation` and three.js. A positive yaw carries +X towards
        **-Z**, which is the opposite of the 2D rotation matrix one writes by
        reflex -- and getting it backwards turns every ghost box 90 degrees
        from the furniture it represents without failing anything.
        """
        half_w, half_d = self.size[0] / 2.0, self.size[1] / 2.0
        local = np.array(
            [[-half_w, -half_d], [half_w, -half_d], [half_w, half_d], [-half_w, half_d]],
            dtype=np.float64,
        )
        angle = math.radians(self.yaw_deg)
        cos, sin = math.cos(angle), math.sin(angle)
        rotation = np.array([[cos, sin], [-sin, cos]], dtype=np.float64)
        offset = np.array([self.center[0], self.center[2]], dtype=np.float64)
        corners: NDArray[np.float64] = local @ rotation.T + offset
        return corners

    @property
    def y_range(self) -> tuple[float, float]:
        half = self.size[2] / 2.0
        return self.center[1] - half, self.center[1] + half


def _min_area_rectangle(
    points_xz: NDArray[np.float64],
) -> tuple[tuple[float, float], tuple[float, float], float]:
    """Smallest-area enclosing rectangle, by rotating calipers on the hull.

    Returns (centre, (width, depth), yaw degrees). The classic result this
    relies on: a minimum-area enclosing rectangle always has one side flush
    with an edge of the convex hull, so testing one orientation per hull edge
    is exhaustive rather than a search.
    """
    from scipy.spatial import ConvexHull  # local import: scipy is heavy

    if len(points_xz) < 3:
        # Degenerate, but a two-point cluster still has an extent worth
        # reporting rather than discarding.
        lo, hi = points_xz.min(axis=0), points_xz.max(axis=0)
        centre = (lo + hi) / 2.0
        return (
            (float(centre[0]), float(centre[1])),
            (
                float(hi[0] - lo[0]),
                float(hi[1] - lo[1]),
            ),
            0.0,
        )

    try:
        hull = points_xz[ConvexHull(points_xz).vertices]
    except Exception:  # noqa: BLE001 - collinear input; fall back to the AABB
        lo, hi = points_xz.min(axis=0), points_xz.max(axis=0)
        centre = (lo + hi) / 2.0
        return (
            (float(centre[0]), float(centre[1])),
            (
                float(hi[0] - lo[0]),
                float(hi[1] - lo[1]),
            ),
            0.0,
        )

    best_area = math.inf
    best: tuple[tuple[float, float], tuple[float, float], float] = (
        (0.0, 0.0),
        (0.0, 0.0),
        0.0,
    )
    for index in range(len(hull)):
        edge = hull[(index + 1) % len(hull)] - hull[index]
        length = float(np.hypot(edge[0], edge[1]))
        if length < 1e-9:
            continue
        angle = math.atan2(edge[1], edge[0])
        cos, sin = math.cos(-angle), math.sin(-angle)
        rotation = np.array([[cos, -sin], [sin, cos]], dtype=np.float64)
        rotated = hull @ rotation.T

        lo, hi = rotated.min(axis=0), rotated.max(axis=0)
        extent = hi - lo
        area = float(extent[0] * extent[1])
        if area < best_area:
            centre_rotated = (lo + hi) / 2.0
            centre = np.linalg.inv(rotation) @ centre_rotated
            best_area = area
            best = (
                (float(centre[0]), float(centre[1])),
                (float(extent[0]), float(extent[1])),
                # Negated: `angle` is the heading of the rectangle's long
                # edge, and a +Y yaw of theta puts local +X at heading
                # -theta. Reporting the heading directly would mirror every
                # box's orientation.
                -math.degrees(angle),
            )
    return best


def fit_box(points: Points) -> OrientedBox | None:
    """Fit a trimmed, gravity-aligned box to one instance's points.

    Returns None for a cluster too small or too thin to be an object, which
    is the right answer for a stray mask that caught a patch of wall.
    """
    cloud = np.asarray(points, dtype=np.float64)
    if cloud.ndim != 2 or cloud.shape[1] != 3:
        raise ValueError(f"expected (N, 3) points, got {cloud.shape}")
    if len(cloud) < MIN_INSTANCE_POINTS:
        return None

    # 3.5: trim per axis before fitting. Done on the raw cloud rather than
    # after projection so a mask that bled onto the floor does not stretch
    # the height either.
    lo = np.percentile(cloud, TRIM_PERCENTILE[0], axis=0)
    hi = np.percentile(cloud, TRIM_PERCENTILE[1], axis=0)
    trimmed = cloud[np.all((cloud >= lo) & (cloud <= hi), axis=1)]
    if len(trimmed) < 3:
        trimmed = cloud

    (cx, cz), (width, depth), yaw = _min_area_rectangle(trimmed[:, [0, 2]])
    y_lo, y_hi = float(trimmed[:, 1].min()), float(trimmed[:, 1].max())
    height = y_hi - y_lo

    if max(width, depth, height) < MIN_OBJECT_EXTENT_MM:
        return None

    return OrientedBox(
        center=(cx, (y_lo + y_hi) / 2.0, cz),
        size=(width, depth, height),
        yaw_deg=yaw % 360.0,
    )


def box_iou_3d(a: OrientedBox, b: OrientedBox) -> float:
    """Intersection over union, footprint overlap times height overlap.

    An approximation -- it treats the two boxes' footprints as separable from
    their heights, which is exact only when their yaws agree. That is good
    enough for its one job, deciding whether two tracks are the same sofa,
    and it avoids a full 3D polyhedron intersection for no benefit.
    """
    from shapely.geometry import Polygon

    footprint_a = Polygon(a.corners_xz())
    footprint_b = Polygon(b.corners_xz())
    if not footprint_a.is_valid or not footprint_b.is_valid:
        return 0.0
    footprint_intersection = footprint_a.intersection(footprint_b).area
    if footprint_intersection <= 0:
        return 0.0

    a_lo, a_hi = a.y_range
    b_lo, b_hi = b.y_range
    height_intersection = max(0.0, min(a_hi, b_hi) - max(a_lo, b_lo))
    if height_intersection <= 0:
        return 0.0

    intersection = footprint_intersection * height_intersection
    volume_a = footprint_a.area * a.size[2]
    volume_b = footprint_b.area * b.size[2]
    union = volume_a + volume_b - intersection
    return float(intersection / union) if union > 0 else 0.0


def box_containment(a: OrientedBox, b: OrientedBox) -> float:
    """How much of the *smaller* box lies inside the larger one, 0 to 1.

    The companion to `box_iou_3d` for partial views: see `MERGE_CONTAINMENT`.
    Normalising by the smaller volume is what makes this insensitive to the
    size difference that defeats IoU.
    """
    from shapely.geometry import Polygon

    footprint_a = Polygon(a.corners_xz())
    footprint_b = Polygon(b.corners_xz())
    if not footprint_a.is_valid or not footprint_b.is_valid:
        return 0.0
    footprint_intersection = footprint_a.intersection(footprint_b).area
    if footprint_intersection <= 0:
        return 0.0

    a_lo, a_hi = a.y_range
    b_lo, b_hi = b.y_range
    height_intersection = max(0.0, min(a_hi, b_hi) - max(a_lo, b_lo))
    if height_intersection <= 0:
        return 0.0

    intersection = footprint_intersection * height_intersection
    smaller = min(footprint_a.area * a.size[2], footprint_b.area * b.size[2])
    return float(intersection / smaller) if smaller > 0 else 0.0


def same_object(a: OrientedBox, b: OrientedBox) -> bool:
    """Whether two fitted boxes are two views of one physical object."""
    return box_iou_3d(a, b) > MERGE_IOU or box_containment(a, b) > MERGE_CONTAINMENT


@dataclass(frozen=True, slots=True)
class Instance:
    """One segmentation track, before merging."""

    track_id: int
    label: str
    label_group: str
    score: float
    points: Points


@dataclass(frozen=True, slots=True)
class DetectedObject:
    """One physical object, after merging. Serialises into `ObjectsFile`."""

    id: str
    label: str
    label_group: str
    score: float
    box: OrientedBox
    track_ids: list[int]
    point_count: int

    @property
    def removable(self) -> bool:
        """3.5: the prompt *group* decides, never the label.

        A radiator and a sofa are both "furniture" to a person; only one of
        them is going anywhere, and the S4 prompt groups are where that
        distinction is stated once.
        """
        return self.label_group == REMOVABLE_GROUP

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "label_group": self.label_group,
            "score": round(self.score, 4),
            "obb": {
                "center": [int(round(v)) for v in self.box.center],
                "size": [max(int(round(v)), 1) for v in self.box.size],
                "yaw_deg": round(self.box.yaw_deg, 2),
            },
            "removable": self.removable,
            "track_id": self.track_ids[0],
            "point_count": self.point_count,
        }


def merge_instances(instances: list[Instance]) -> list[DetectedObject]:
    """Fit every track, then merge the ones that are the same object.

    Merging is greedy over boxes sorted by descending footprint: the largest
    box absorbs the smaller ones that sit inside it. That ordering matters
    because the alternative -- merging in track order -- lets a sliver of a
    sofa seen early claim the identity of the whole sofa seen later, and the
    merged box then inherits the sliver's label and score.
    """
    fitted: list[tuple[Instance, OrientedBox]] = []
    for instance in instances:
        box = fit_box(instance.points)
        if box is not None:
            fitted.append((instance, box))

    fitted.sort(key=lambda pair: pair[1].footprint_area_mm2, reverse=True)

    merged: list[DetectedObject] = []
    claimed: list[OrientedBox] = []
    for instance, box in fitted:
        absorbed_into: int | None = None
        for index, existing in enumerate(claimed):
            if merged[index].label_group == instance.label_group and same_object(existing, box):
                absorbed_into = index
                break

        if absorbed_into is None:
            merged.append(
                DetectedObject(
                    id=f"D{len(merged) + 1}",
                    label=instance.label,
                    label_group=instance.label_group,
                    score=instance.score,
                    box=box,
                    track_ids=[instance.track_id],
                    point_count=len(instance.points),
                )
            )
            claimed.append(box)
        else:
            previous = merged[absorbed_into]
            merged[absorbed_into] = DetectedObject(
                id=previous.id,
                label=previous.label,
                label_group=previous.label_group,
                # The best view of an object is the most confident thing we
                # saw of it, not the average of every glimpse.
                score=max(previous.score, instance.score),
                box=previous.box,
                track_ids=[*previous.track_ids, instance.track_id],
                point_count=previous.point_count + len(instance.points),
            )

    return merged


def objects_file(objects: list[DetectedObject], *, pipeline_version: str) -> dict[str, Any]:
    """Serialise to the `ObjectsFile` schema."""
    return {
        "schema_version": 1,
        "pipeline_version": pipeline_version,
        "units": "mm",
        "objects": [obj.as_json() for obj in objects],
    }


def fixed_obstacles(objects: list[DetectedObject]) -> list[dict[str, Any]]:
    """The `fixed_obstacles` entries a RoomModel needs (3.7 "ghost objects").

    Fixed-group detections default to "keep": a radiator, a fireplace or a
    built-in is not going anywhere, so the solver has to route around it
    whether or not the user ever opens the keep-or-remove UI.
    """
    return [
        {
            "id": f"F{index + 1}",
            "label": obj.label,
            "center": [int(round(obj.box.center[0])), int(round(obj.box.center[2]))],
            "size": [max(int(round(v)), 1) for v in obj.box.size],
            "yaw_deg": round(obj.box.yaw_deg, 2),
            "source": "detected",
        }
        for index, obj in enumerate(o for o in objects if o.label_group == FIXED_GROUP)
    ]
