"""S9 publish: assemble the `RoomModel` document (implementation-plan.md 2.4, 3.8).

Decision 3 of the plan: one machine-readable `RoomModel` JSON is the contract
between the CV pipeline, the layout engine, the database and the viewer. This
module is the only place that document is built, which means the conventions
of 2.4 are enforced in one place rather than assumed in four:

- **Integer millimetres**, everywhere. Rounding happens here, at the boundary,
  not scattered through the stages where it would compound.
- **Origin at the floor polygon's centroid**, and **+X along the longest wall**.
  Neither is true of what S6 produces -- S6 works in whatever frame the
  reconstruction arrived in -- so the recentring and the final yaw are applied
  here, once the polygon exists to define them.
- **Counter-clockwise floor polygon**, which the schema requires and the
  viewer's floor winding depends on.

Scale is applied here too, for the same reason: `rescale_scan` (7.1) re-runs
S7 and this module on stored intermediates, so keeping every stage before S7
in reconstruction units is what makes a re-scale cheap.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from shapely.geometry import Polygon

from .geometry import RoomGeometry, orient_ccw
from .scale import ScaleResult

SCHEMA_VERSION = 1

# From the closed `warning_code` enum in common.schema.json.
WARN_SCALE_UNCALIBRATED = "SCALE_UNCALIBRATED"
WARN_LOW_COVERAGE = "LOW_COVERAGE"

# Below this share of the room seen, the geometry is an extrapolation as much
# as a measurement, and the user should be told before they trust a fit.
LOW_COVERAGE_PCT = 60.0


def _longest_wall_yaw(polygon: Polygon) -> float:
    """Heading of the polygon's longest edge, in degrees.

    2.4 puts +X along the longest wall. It is an arbitrary choice made
    non-arbitrary by being fixed: the solver's {0, 90, 180, 270} rotations and
    the viewer's default camera both assume it, and a room that picks a
    different axis each run would have its furniture face a different way
    each time it is regenerated.
    """
    coords = list(polygon.exterior.coords)[:-1]
    best_length = 0.0
    best_yaw = 0.0
    for start, end in zip(coords, coords[1:] + coords[:1], strict=False):
        length = math.dist(start, end)
        if length > best_length:
            best_length = length
            best_yaw = math.degrees(math.atan2(end[1] - start[1], end[0] - start[0]))
    return best_yaw


def canonicalise(geometry: RoomGeometry) -> tuple[Polygon, list[tuple[float, float]]]:
    """Rotate the longest wall onto +X and move the centroid to the origin.

    Returns the canonical polygon and its vertices. Walls and openings are
    reindexed by the caller against the same transform, so everything stays
    consistent.
    """
    polygon = orient_ccw(geometry.polygon)
    yaw = _longest_wall_yaw(polygon)

    # Rotating by +yaw reduces a heading measured as atan2(z, x) -- the same
    # sign trap as `align.yaw_rotation`, and wrong here would leave the room
    # at twice its yaw with no test between this and the viewer to catch it.
    angle = math.radians(yaw)
    cos, sin = math.cos(angle), math.sin(angle)

    coords = [(float(x), float(z)) for x, z in list(polygon.exterior.coords)[:-1]]
    rotated = [(x * cos + z * sin, -x * sin + z * cos) for x, z in coords]

    centroid = Polygon(rotated).centroid
    centred = [(x - centroid.x, z - centroid.y) for x, z in rotated]
    return orient_ccw(Polygon(centred)), centred


def build(
    geometry: RoomGeometry,
    scale: ScaleResult,
    *,
    pipeline_version: str,
    appearance: dict[str, Any] | None = None,
    fixed_obstacles: list[dict[str, Any]] | None = None,
    frames_used: int = 0,
    coverage_pct: float = 0.0,
    extra_warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Produce the `RoomModel` document.

    `geometry` is in reconstruction units; `scale.factor` converts to
    millimetres. Everything is rounded to integers at the end, per 2.4.
    """
    canonical, vertices = canonicalise(geometry)
    factor = scale.factor

    floor_polygon = [[int(round(x * factor)), int(round(z * factor))] for x, z in vertices]

    # Walls are rebuilt from the canonical polygon's edges rather than
    # transformed individually, so wall ids stay in step with the vertex
    # order the viewer draws. Their kind and evidence come from S6 by
    # position, matched on the original ordering, which the canonical
    # transform preserves (it is a rotation and a translation).
    walls: list[dict[str, Any]] = []
    for index, (start, end) in enumerate(zip(vertices, vertices[1:] + vertices[:1], strict=False)):
        source = geometry.walls[index] if index < len(geometry.walls) else None
        length_mm = int(round(math.dist(start, end) * factor))
        walls.append(
            {
                "id": f"W{index + 1}",
                "start": [int(round(start[0] * factor)), int(round(start[1] * factor))],
                "end": [int(round(end[0] * factor)), int(round(end[1] * factor))],
                "thickness_mm": 100,
                "kind": source.kind if source is not None else "solid",
                "length_mm": length_mm,
            }
        )

    wall_lengths = {wall["id"]: wall["length_mm"] for wall in walls}
    openings: list[dict[str, Any]] = []
    for opening in geometry.openings:
        available = wall_lengths.get(opening.wall_id)
        if available is None:
            # S6's sanity check already rejects this; skipping rather than
            # raising keeps `build` a pure serialiser.
            continue
        offset = int(round(opening.offset_mm * factor))
        width = max(int(round(opening.width_mm * factor)), 1)
        # Clamp after scaling: rounding can push an opening one millimetre
        # past the end of its wall, which the schema forbids.
        offset = min(offset, max(available - width, 0))
        width = min(width, max(available - offset, 1))
        openings.append(
            {
                "id": opening.id,
                "wall_id": opening.wall_id,
                "type": opening.type,
                "offset_mm": offset,
                "width_mm": width,
                "sill_mm": max(int(round(opening.sill_mm * factor)), 0),
                "height_mm": max(int(round(opening.height_mm * factor)), 1),
                "swing": opening.swing,
                "confidence": round(opening.confidence, 3),
            }
        )

    # `height`, not `height_mm`: rounding before the factor is applied
    # quantises the ceiling to whole input units, which for a reconstruction
    # in metres turns 2.5 m into 2 m.
    ceiling_mm = int(round(geometry.profile.height * factor))

    warnings = set(geometry.warnings) | set(extra_warnings or [])
    if not scale.user_calibrated:
        # 3.7 and 3.11: an uncalibrated scale is a best guess and has to say
        # so. Set here rather than left to the UI, so the caveat travels with
        # the document into the database and the layout engine.
        warnings.add(WARN_SCALE_UNCALIBRATED)
    if coverage_pct and coverage_pct < LOW_COVERAGE_PCT:
        warnings.add(WARN_LOW_COVERAGE)

    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "pipeline_version": pipeline_version,
        "units": "mm",
        "up": "+Y",
        "floor_polygon": floor_polygon,
        "ceiling_height_mm": ceiling_mm,
        "ceiling_observed": geometry.profile.ceiling_observed,
        "walls": walls,
        "openings": openings,
        "fixed_obstacles": fixed_obstacles or [],
        "scale": scale.as_json(),
        "quality": {
            "coverage_pct": round(coverage_pct, 2),
            "frames_used": frames_used,
            "warnings": sorted(set(warnings)),
        },
    }
    if appearance is not None:
        document["appearance"] = appearance
    return document


def floor_area_mm2(document: dict[str, Any]) -> int:
    """Floor area of a built RoomModel, for the denormalised DB column (6.1)."""
    return int(round(Polygon(document["floor_polygon"]).area))


def longest_wall_is_x_aligned(document: dict[str, Any], *, tolerance_deg: float = 1.0) -> bool:
    """Check 2.4's axis convention on a built document.

    Exposed rather than kept private because it is the kind of invariant that
    is easy to break from a long way away -- a change in S6's vertex ordering,
    say -- and cheap for a caller or a test to assert.
    """
    polygon = Polygon(document["floor_polygon"])
    yaw = _longest_wall_yaw(polygon)
    # Modulo 180: a wall running along -X is just as axis-aligned as +X.
    return bool(min(abs(yaw % 180.0), 180.0 - abs(yaw % 180.0)) <= tolerance_deg)


def centroid_is_origin(document: dict[str, Any], *, tolerance_mm: float = 2.0) -> bool:
    """Check 2.4's origin convention on a built document."""
    centroid = Polygon(document["floor_polygon"]).centroid
    return bool(np.hypot(centroid.x, centroid.y) <= tolerance_mm)
