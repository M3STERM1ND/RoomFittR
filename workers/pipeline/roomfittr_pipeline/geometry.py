"""S6 room geometry: floor, ceiling, walls, openings (implementation-plan.md 3.6).

The guaranteed path. 3.6 offers SpatialLM as a learned alternative, but
Phase 0's licence review found it non-commercial on every variant, so it is
an E4 baseline only and this is the code that ships.

**The shape of the method.** Walls are found as *lines fitted to wall
evidence*, and the floor plan is assembled from where those lines cross. It
is not a traced outline of the floor points. That distinction is worth
stating because the evaluation adapter in `eval/adapters/geometry.py` does
trace a floor raster, and if S6 did the same thing E4 would be scoring a
method against itself. It also produces a better answer: a traced raster
boundary is quantised to its cell size and wanders wherever the floor was
poorly scanned, whereas a fitted line is supported by the whole height of the
wall and lands where the wall actually is.

**The one rule that matters most.** Wall evidence excludes points belonging
to furniture. A sofa against a wall is a dense vertical slab 900 mm into the
room, and if it votes for wall position then the wall moves in by 900 mm and
the room loses most of a metre. 3.9's R3 calls this out and E6 measures it.
Object exclusion happens at the caller's boundary -- `wall_evidence_grid`
takes structure points only -- and `walls_are_not_pulled_in_by_furniture` in
the tests is the regression guard.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray
from shapely.geometry import MultiPolygon, Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .align import Points
from .errors import PipelineError, Stage

# 3.6 step 2: 2 cm cells for the wall-evidence grid.
WALL_GRID_MM = 20.0

# 3.6 step 2: only points between 0.3 m and (ceiling - 0.3 m) vote for walls.
# Below that band is skirting, floor bleed and low furniture; above it is
# coving and ceiling bleed. Both would blur the wall position.
WALL_BAND_MARGIN_MM = 300.0

# Height histogram for finding floor and ceiling. 50 mm bins: fine enough to
# separate a floor from a rug, coarse enough that a sparse ceiling still
# forms a peak.
HEIGHT_BIN_MM = 50.0

# A height peak counts as a real surface if it holds at least this share of
# the points in the fullest bin. Relative, because a ceiling seen at a
# glancing angle for two seconds has far fewer points than a floor walked
# across for a minute.
SURFACE_PEAK_RATIO = 0.25

# 3.6 step 1: the fallback when the ceiling was never properly seen.
CEILING_PRIOR_MM = 2400
MIN_CEILING_MM = 2000
MAX_CEILING_MM = 4500

# Candidate wall lines: a projected-density peak must carry this share of the
# strongest peak's votes to be a candidate. Set low because a wall seen
# briefly still matters to the room's shape; false candidates are cheap,
# since the interior test discards lines that bound nothing.
WALL_PEAK_RATIO = 0.12

# Two candidate lines closer together than this are the same wall seen with
# noise. 150 mm is under the thinnest real feature we model (a 100 mm wall)
# and over the reconstruction's positional noise.
WALL_MERGE_MM = 150.0

# 3.6 step 4: corners within this of square are snapped to square.
CORNER_SNAP_DEG = 12.0

# 3.6 step 5: a boundary edge with wall evidence along less than this share
# of its length is an `open` segment -- an open-plan edge or a wide doorway,
# not a wall. The solver treats it as a boundary but never places against it.
OPEN_EDGE_COVERAGE = 0.35

# Edges shorter than this are rasterisation and intersection artefacts.
MIN_WALL_LENGTH_MM = 250.0

# Past this share of the perimeter with no wall behind it, the capture did not
# measure a room. An open-plan living room legitimately has one or two open
# edges; half the perimeter open means the user filmed one wall and stopped,
# and the floor extent standing in for the rest only records where they walked.
MAX_OPEN_PERIMETER_FRACTION = 0.5

# 3.6 sanity checks.
MIN_FLOOR_AREA_M2 = 3.0
MAX_FLOOR_AREA_M2 = 150.0

# 3.6 step 6: an opening's points must lie within this of a wall plane to be
# assigned to it. Frames have depth and the scan sees into the reveal.
OPENING_TO_WALL_MM = 300.0

# 3.6 step 6: plausible door dimensions, used to clamp a noisy fit.
DOOR_WIDTH_RANGE_MM = (600, 1200)
DOOR_HEIGHT_RANGE_MM = (1900, 2400)
# A door's sill is the floor. Anything fitted higher than this is a window
# that was labelled as a door.
MAX_DOOR_SILL_MM = 150


# Warning codes, from the closed `warning_code` enum in common.schema.json.
# Named here rather than spelled at each raise site so a typo is an
# AttributeError at import instead of a schema violation at publish.
WARN_CEILING_NOT_OBSERVED = "CEILING_NOT_OBSERVED"
WARN_OPEN_BOUNDARY = "OPEN_BOUNDARY_PRESENT"
WARN_DOOR_POSSIBLY_MISSED = "DOOR_POSSIBLY_MISSED"
WARN_DOOR_SWING_UNKNOWN = "DOOR_SWING_UNKNOWN"


@dataclass(frozen=True, slots=True)
class HeightProfile:
    """Where the floor and ceiling sit, measured along +Y.

    In the units the geometry arrived in. S6's thresholds are metric, so in
    practice S5 has applied a provisional metric scale and those units are
    approximately millimetres -- but `height` stays a float, because S7's
    correction is applied later and rounding here would quantise the ceiling
    to whole units before it is refined.
    """

    floor_mm: float
    ceiling_mm: float
    ceiling_observed: bool

    @property
    def height(self) -> float:
        """Floor-to-ceiling, unrounded, in the input's units."""
        return self.ceiling_mm - self.floor_mm

    @property
    def height_mm(self) -> int:
        """The same, rounded, for the sanity checks and denormalised columns."""
        return int(round(self.height))


def height_profile(structure_points: Points) -> HeightProfile:
    """3.6 step 1: floor and ceiling from a height histogram.

    Both are found as *peaks*, not as extremes. The lowest point in a room
    scan is frequently a stray below the floor and the highest is frequently
    a light fitting; a peak is a surface that many points agree on.
    """
    points = np.asarray(structure_points, dtype=np.float64)
    if len(points) < 100:
        raise PipelineError(
            "INSUFFICIENT_COVERAGE",
            Stage.GEOMETRY,
            f"{len(points)} structure points is too few to find floor and ceiling",
        )

    y = points[:, 1]
    lo, hi = float(y.min()), float(y.max())
    if hi - lo < MIN_CEILING_MM / 2:
        raise PipelineError(
            "INSUFFICIENT_COVERAGE",
            Stage.GEOMETRY,
            f"scene spans only {hi - lo:.0f} mm vertically; no room fits in that",
        )

    bins = max(int(math.ceil((hi - lo) / HEIGHT_BIN_MM)), 2)
    counts, edges = np.histogram(y, bins=bins, range=(lo, hi))
    centres = (edges[:-1] + edges[1:]) / 2.0
    strong = counts >= counts.max() * SURFACE_PEAK_RATIO

    floor = float(centres[strong][0])
    ceiling_candidates = centres[strong]
    ceiling = float(ceiling_candidates[-1])

    # A ceiling is "observed" when it is a peak in its own right and far
    # enough above the floor to be a ceiling. Phone captures point downward
    # and forward far more than up, so this often fails -- 3.6 expects it.
    observed = (ceiling - floor) >= MIN_CEILING_MM
    if not observed:
        # Fall back to the tallest wall evidence, then to the prior. Wall
        # tops are a better guess than a flat 2400 because they are measured
        # from this room, even if the ceiling itself was never framed.
        wall_top = float(np.percentile(y, 99.5))
        ceiling = wall_top if (wall_top - floor) >= MIN_CEILING_MM else floor + CEILING_PRIOR_MM

    return HeightProfile(floor_mm=floor, ceiling_mm=ceiling, ceiling_observed=observed)


@dataclass(frozen=True, slots=True)
class EvidenceGrid:
    """Wall evidence rasterised onto the floor plane.

    `density[i, j]` counts points in the cell whose XZ corner is
    `(origin_x + j * cell_mm, origin_z + i * cell_mm)`. Row is Z, column is X,
    which matches image conventions and keeps the numpy indexing readable.
    """

    density: NDArray[np.float64]
    origin_x_mm: float
    origin_z_mm: float
    cell_mm: float

    def x_of(self, column: float) -> float:
        return self.origin_x_mm + column * self.cell_mm

    def z_of(self, row: float) -> float:
        return self.origin_z_mm + row * self.cell_mm

    def column_of(self, x_mm: float) -> float:
        return (x_mm - self.origin_x_mm) / self.cell_mm

    def row_of(self, z_mm: float) -> float:
        return (z_mm - self.origin_z_mm) / self.cell_mm


def wall_evidence_grid(
    structure_points: Points,
    profile: HeightProfile,
    *,
    cell_mm: float = WALL_GRID_MM,
) -> EvidenceGrid:
    """3.6 step 2. **Structure points only** -- see the module docstring.

    Passing object points here is the single most damaging mistake available
    in S6, so the parameter is named for what it must receive and the caller
    does the partition.
    """
    points = np.asarray(structure_points, dtype=np.float64)
    band_lo = profile.floor_mm + WALL_BAND_MARGIN_MM
    band_hi = profile.ceiling_mm - WALL_BAND_MARGIN_MM
    if band_hi <= band_lo:
        # A very low ceiling, or a bad profile. Use the middle half rather
        # than an empty band.
        band_lo = profile.floor_mm + (profile.ceiling_mm - profile.floor_mm) * 0.25
        band_hi = profile.floor_mm + (profile.ceiling_mm - profile.floor_mm) * 0.75

    in_band = points[(points[:, 1] >= band_lo) & (points[:, 1] <= band_hi)]
    if len(in_band) < 50:
        raise PipelineError(
            "INSUFFICIENT_COVERAGE",
            Stage.GEOMETRY,
            f"only {len(in_band)} points lie in the wall band; the walls were not seen",
        )

    xs, zs = in_band[:, 0], in_band[:, 2]
    origin_x = float(np.floor(xs.min() / cell_mm) * cell_mm)
    origin_z = float(np.floor(zs.min() / cell_mm) * cell_mm)
    columns = int(np.ceil((xs.max() - origin_x) / cell_mm)) + 1
    rows = int(np.ceil((zs.max() - origin_z) / cell_mm)) + 1

    density, _, _ = np.histogram2d(
        zs,
        xs,
        bins=(rows, columns),
        range=((origin_z, origin_z + rows * cell_mm), (origin_x, origin_x + columns * cell_mm)),
    )
    return EvidenceGrid(
        density=density.astype(np.float64),
        origin_x_mm=origin_x,
        origin_z_mm=origin_z,
        cell_mm=cell_mm,
    )


def candidate_lines(grid: EvidenceGrid, axis: int) -> list[float]:
    """3.6 step 3, with the Manhattan prior applied as hard as it goes.

    A general Hough transform searches every orientation. After S5 the room
    is square to the axes, so the search collapses to two families -- lines of
    constant X and lines of constant Z -- and each family is found by
    projecting the density onto that axis and picking peaks. This is a Hough
    transform with the prior built in, and it is far more robust on sparse
    evidence than fitting free-orientation lines to it.

    `axis` is 0 for lines of constant X, 2 for constant Z (matching the
    coordinate's index).
    """
    if axis not in (0, 2):
        raise ValueError("axis must be 0 (constant X) or 2 (constant Z)")

    # Summing across the other axis: a wall of constant X is a column of
    # cells, so its votes pile up in one entry of the column sum.
    projection = grid.density.sum(axis=0 if axis == 0 else 1)
    if projection.max() <= 0:
        return []

    threshold = projection.max() * WALL_PEAK_RATIO
    positions: list[float] = []
    index = 0
    while index < len(projection):
        if projection[index] < threshold:
            index += 1
            continue
        # Walk the contiguous run above threshold and take its centre of
        # mass. A wall is several cells wide once noise and real thickness
        # are accounted for, and its centre is a better estimate than the
        # single fullest cell.
        start = index
        while index < len(projection) and projection[index] >= threshold:
            index += 1
        run = projection[start:index]
        centre = start + float(np.average(np.arange(len(run)), weights=run))
        positions.append(grid.x_of(centre) if axis == 0 else grid.z_of(centre))

    return _merge_close(positions, WALL_MERGE_MM)


# How far a floor edge may sit from the nearest fitted wall line before it is
# treated as an unwalled boundary in its own right. Generous, because floor
# points thin out near walls (grazing incidence, skirting, furniture) and a
# spurious line just inside a real wall is the pull-in failure all over again.
FLOOR_EDGE_TO_WALL_MM = 500.0


def _bounded_lines(wall_lines: list[float], floor_coordinate: NDArray[np.float64]) -> list[float]:
    """Add the floor's own extent as a boundary where the floor runs past the walls.

    3.6 step 5 requires open boundaries to work: an open-plan edge or a wide
    doorway has no wall behind it, so `candidate_lines` finds nothing there
    and the arrangement is missing a side. The room is still bounded -- by
    where its floor stops -- and that is the line to use.

    The direction of the test is the whole of the logic. A floor edge is
    promoted only when it lies **outside** every wall line, because that is
    the only case where it carries information the walls do not:

    - Floor running *past* the outermost wall means there is no wall there.
      That is an open boundary.
    - Floor stopping *short* of a wall means the floor was occluded -- a
      wardrobe, a bed, a sofa hid it. The wall is the better evidence and the
      floor's edge must be ignored, or the room loses everything behind the
      furniture.

    Reversing that test (promoting any extent far from a wall line) shrinks
    every room with a full-width obstruction against a wall, and the result
    still looks like a tidy rectangle.
    """
    lines = list(wall_lines)
    if not lines:
        return sorted(
            {
                float(np.percentile(floor_coordinate, 0.5)),
                float(np.percentile(floor_coordinate, 99.5)),
            }
        )

    low_extent = float(np.percentile(floor_coordinate, 0.5))
    high_extent = float(np.percentile(floor_coordinate, 99.5))
    if low_extent < min(lines) - FLOOR_EDGE_TO_WALL_MM:
        lines.append(low_extent)
    if high_extent > max(lines) + FLOOR_EDGE_TO_WALL_MM:
        lines.append(high_extent)
    return sorted(lines)


def _merge_close(values: list[float], tolerance_mm: float) -> list[float]:
    """Collapse values within `tolerance_mm` into their mean."""
    if not values:
        return []
    ordered = sorted(values)
    merged: list[list[float]] = [[ordered[0]]]
    for value in ordered[1:]:
        if value - merged[-1][-1] <= tolerance_mm:
            merged[-1].append(value)
        else:
            merged.append([value])
    return [float(np.mean(group)) for group in merged]


def _cell_polygon(x0: float, x1: float, z0: float, z1: float) -> Polygon:
    return box(x0, z0, x1, z1)


def floor_polygon(
    grid: EvidenceGrid,
    floor_points_xz: NDArray[np.float64],
    *,
    min_floor_points_per_m2: float = 8.0,
) -> Polygon:
    """3.6 step 4: assemble the floor plan from where the wall lines cross.

    The candidate lines cut the plane into a grid of rectangular cells (the
    "arrangement"). Each cell is either inside the room or outside it, and
    the floor points decide which: a cell with floor under it is interior.
    The union of the interior cells is the floor plan, and because every one
    of its edges lies on a fitted wall line, the boundary is exactly on the
    walls rather than quantised to a raster.

    The floor points are used only to *classify* cells, never to position an
    edge. That is what keeps this independent of the evaluation adapter's
    traced outline.
    """
    floor_xz = np.asarray(floor_points_xz, dtype=np.float64)
    if floor_xz.ndim != 2 or floor_xz.shape[1] != 2:
        raise ValueError(f"expected (N, 2) floor points, got {floor_xz.shape}")
    if len(floor_xz) < 50:
        raise PipelineError(
            "INSUFFICIENT_COVERAGE",
            Stage.GEOMETRY,
            f"{len(floor_xz)} floor points is too few to place the room's interior",
        )

    xs = _bounded_lines(candidate_lines(grid, axis=0), floor_xz[:, 0])
    zs = _bounded_lines(candidate_lines(grid, axis=2), floor_xz[:, 1])
    if len(xs) < 2 or len(zs) < 2:
        raise PipelineError(
            "INSUFFICIENT_COVERAGE",
            Stage.GEOMETRY,
            f"found {len(xs)} x-bounds and {len(zs)} z-bounds; a room needs two of each",
        )

    interior: list[Polygon] = []
    for x0, x1 in zip(xs[:-1], xs[1:], strict=False):
        for z0, z1 in zip(zs[:-1], zs[1:], strict=False):
            area_m2 = ((x1 - x0) / 1000.0) * ((z1 - z0) / 1000.0)
            if area_m2 <= 0:
                continue
            inside = (
                (floor_xz[:, 0] >= x0)
                & (floor_xz[:, 0] < x1)
                & (floor_xz[:, 1] >= z0)
                & (floor_xz[:, 1] < z1)
            )
            # A density rather than a count, so a large cell is not favoured
            # merely for being large and a small one is not starved.
            if int(inside.sum()) / area_m2 >= min_floor_points_per_m2:
                interior.append(_cell_polygon(x0, x1, z0, z1))

    if not interior:
        raise PipelineError(
            "INSUFFICIENT_COVERAGE",
            Stage.GEOMETRY,
            "no cell of the wall arrangement contains floor; the floor was not seen",
        )

    merged = unary_union(interior)
    polygon = _largest_polygon(merged)
    # Interior holes are always artefacts here: a freestanding column would
    # be a fixed obstacle, not a hole in the floor.
    polygon = Polygon(polygon.exterior)
    return _tidy(polygon)


def _largest_polygon(geometry: BaseGeometry) -> Polygon:
    """The biggest piece. Detached fragments are scan noise, not annexes."""
    if isinstance(geometry, Polygon):
        return geometry
    if isinstance(geometry, MultiPolygon):
        return max(geometry.geoms, key=lambda g: g.area)
    raise PipelineError(
        "LAYOUT_EXTRACTION_FAILED",
        Stage.GEOMETRY,
        f"floor union produced a {geometry.geom_type}, which is not a room",
    )


def _exterior_xz(polygon: Polygon) -> list[tuple[float, float]]:
    """The exterior ring's vertices as plain (x, z) pairs, without the repeat.

    Shapely types a coordinate as a variable-length tuple because a ring may
    carry a Z ordinate. Ours never does, and narrowing here once keeps every
    caller working in the 2D pairs the rest of S6 is written in.
    """
    return [(float(point[0]), float(point[1])) for point in polygon.exterior.coords[:-1]]


def _tidy(polygon: Polygon) -> Polygon:
    """3.6 step 4: drop collinear vertices and stubs, then snap square corners."""
    # `simplify` is typed as returning BaseGeometry because in general it can
    # degenerate; for a polygon it returns a polygon, and `_largest_polygon`
    # both narrows the type and turns the degenerate case into a named error
    # rather than an AttributeError.
    simplified = _largest_polygon(polygon.simplify(1.0, preserve_topology=True))
    coords = _exterior_xz(simplified)

    kept: list[tuple[float, float]] = []
    for point in coords:
        if not kept or math.dist(point, kept[-1]) >= MIN_WALL_LENGTH_MM:
            kept.append(point)
    if len(kept) >= 3 and math.dist(kept[0], kept[-1]) < MIN_WALL_LENGTH_MM:
        kept.pop()

    if len(kept) < 3:
        raise PipelineError(
            "LAYOUT_EXTRACTION_FAILED",
            Stage.GEOMETRY,
            f"floor plan collapsed to {len(kept)} corners",
        )

    result = Polygon(kept)
    if not result.is_valid:
        # `buffer(0)` is shapely's idiom for repairing a self-intersection.
        repaired = result.buffer(0)
        result = _largest_polygon(repaired)
    return orient_ccw(result)


def orient_ccw(polygon: Polygon) -> Polygon:
    """Counter-clockwise exterior, as the RoomModel schema requires."""
    if polygon.exterior.is_ccw:
        return polygon
    return Polygon(list(polygon.exterior.coords)[::-1])


@dataclass(frozen=True, slots=True)
class WallSegment:
    """One edge of the floor plan."""

    id: str
    start: tuple[float, float]
    end: tuple[float, float]
    kind: str
    evidence_coverage: float

    @property
    def length_mm(self) -> float:
        return math.dist(self.start, self.end)

    def direction(self) -> tuple[float, float]:
        length = self.length_mm
        if length < 1e-9:
            return (0.0, 0.0)
        return ((self.end[0] - self.start[0]) / length, (self.end[1] - self.start[1]) / length)


def walls_from_polygon(polygon: Polygon, grid: EvidenceGrid) -> list[WallSegment]:
    """Turn the floor plan's edges into walls, classifying solid versus open.

    3.6 step 5: a boundary run with no wall evidence behind it is an open-plan
    edge or a wide doorway. It bounds the room but nothing can be placed
    against it, so the distinction has to survive into the RoomModel.
    """
    coords = _exterior_xz(polygon)
    walls: list[WallSegment] = []
    for index, (start, end) in enumerate(zip(coords, coords[1:] + coords[:1], strict=False)):
        coverage = _edge_evidence_coverage(grid, start, end)
        walls.append(
            WallSegment(
                id=f"W{index + 1}",
                start=(float(start[0]), float(start[1])),
                end=(float(end[0]), float(end[1])),
                kind="solid" if coverage >= OPEN_EDGE_COVERAGE else "open",
                evidence_coverage=coverage,
            )
        )
    return walls


def _edge_evidence_coverage(
    grid: EvidenceGrid,
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    search_mm: float = 150.0,
) -> float:
    """Fraction of an edge with wall evidence within `search_mm` of it.

    Sampled along the edge rather than integrated over a band, because what
    matters is whether the wall is present *along its length*: a doorway is a
    gap in an otherwise well-evidenced edge, and an averaged density cannot
    tell that from a uniformly faint wall.
    """
    length = math.dist(start, end)
    if length < 1e-6:
        return 0.0

    steps = max(int(length / grid.cell_mm), 4)
    radius = max(int(round(search_mm / grid.cell_mm)), 1)
    rows, columns = grid.density.shape

    hits = 0
    for step in range(steps):
        t = (step + 0.5) / steps
        x = start[0] + (end[0] - start[0]) * t
        z = start[1] + (end[1] - start[1]) * t
        column = int(round(grid.column_of(x)))
        row = int(round(grid.row_of(z)))
        c0, c1 = max(column - radius, 0), min(column + radius + 1, columns)
        r0, r1 = max(row - radius, 0), min(row + radius + 1, rows)
        if c0 < c1 and r0 < r1 and grid.density[r0:r1, c0:c1].any():
            hits += 1
    return hits / steps


@dataclass(frozen=True, slots=True)
class Opening:
    """A door or window, in its wall's local coordinates."""

    id: str
    wall_id: str
    type: str
    offset_mm: int
    width_mm: int
    sill_mm: int
    height_mm: int
    swing: str = "unknown"
    confidence: float = 0.5


def fit_opening(
    points: Points,
    walls: list[WallSegment],
    *,
    opening_id: str,
    label: str,
    floor_mm: float = 0.0,
    confidence: float = 0.5,
) -> Opening | None:
    """3.6 step 6: place one detected door/window onto its wall.

    Returns None when the instance cannot be assigned to a wall, which is
    the honest outcome for a window seen through a doorway into the next
    room, or a mirror reflecting one.
    """
    cloud = np.asarray(points, dtype=np.float64)
    if len(cloud) < 20:
        return None

    centre = cloud.mean(axis=0)
    best: WallSegment | None = None
    best_distance = math.inf
    for wall in walls:
        distance = _point_to_segment_xz(centre, wall)
        if distance < best_distance:
            best, best_distance = wall, distance
    if best is None or best_distance > OPENING_TO_WALL_MM:
        return None

    # Project into wall-local coordinates: u along the wall from its start,
    # v vertical from the floor.
    dx, dz = best.direction()
    u = (cloud[:, 0] - best.start[0]) * dx + (cloud[:, 2] - best.start[1]) * dz
    v = cloud[:, 1] - floor_mm

    # 2nd-98th percentile, as 3.5 does for object boxes: the tails of a
    # segmentation mask are bleed onto the surrounding wall.
    u_lo, u_hi = np.percentile(u, [2.0, 98.0])
    v_lo, v_hi = np.percentile(v, [2.0, 98.0])

    width = int(round(u_hi - u_lo))
    height = int(round(v_hi - v_lo))
    sill = int(round(max(v_lo, 0.0)))
    offset = int(round(max(u_lo, 0.0)))

    if label == "door":
        # 3.6: doors get clamped to plausible dimensions, because a partly
        # occluded door fits a box far too small and the solver would then
        # keep a walkway clear of the wrong opening.
        width = int(np.clip(width, *DOOR_WIDTH_RANGE_MM))
        height = int(np.clip(height, *DOOR_HEIGHT_RANGE_MM))
        if sill > MAX_DOOR_SILL_MM:
            # Sitting well off the floor, so it is not a door whatever the
            # segmentation called it. Recorded as a window rather than
            # discarded: something is there, and a window is the safer guess
            # for the validator (it blocks less).
            label = "window"
        else:
            sill = 0

    if width < 100 or height < 100:
        return None
    # An opening cannot extend past the end of its wall.
    wall_length = best.length_mm
    offset = int(min(offset, max(wall_length - width, 0)))
    width = int(min(width, max(wall_length - offset, 1)))

    return Opening(
        id=opening_id,
        wall_id=best.id,
        type=label if label in ("door", "window", "passage") else "window",
        offset_mm=offset,
        width_mm=width,
        sill_mm=sill,
        height_mm=height,
        confidence=confidence,
    )


def _point_to_segment_xz(point: NDArray[np.float64], wall: WallSegment) -> float:
    """Distance from an XZ point to a wall segment, ignoring height."""
    px, pz = float(point[0]), float(point[2])
    ax, az = wall.start
    bx, bz = wall.end
    vx, vz = bx - ax, bz - az
    length_squared = vx * vx + vz * vz
    if length_squared < 1e-9:
        return math.dist((px, pz), (ax, az))
    t = max(0.0, min(1.0, ((px - ax) * vx + (pz - az) * vz) / length_squared))
    return math.dist((px, pz), (ax + t * vx, az + t * vz))


@dataclass(frozen=True, slots=True)
class RoomGeometry:
    """S6's output, before scale is applied and the RoomModel is assembled."""

    polygon: Polygon
    walls: list[WallSegment]
    openings: list[Opening]
    profile: HeightProfile
    warnings: list[str] = field(default_factory=list)

    @property
    def area_m2(self) -> float:
        return self.polygon.area / 1e6


def sanity_check(geometry: RoomGeometry) -> list[str]:
    """3.6's sanity checks. Returns warnings; raises on the fatal ones.

    The split is deliberate. A room with no openings is odd but usable, and
    telling the user "we didn't find your door" beats refusing to show them
    anything. A self-intersecting polygon is not usable by the solver at all.
    """
    warnings: list[str] = []

    if not geometry.polygon.is_valid or not geometry.polygon.is_simple:
        raise PipelineError(
            "LAYOUT_EXTRACTION_FAILED", Stage.GEOMETRY, "floor plan self-intersects"
        )

    area = geometry.area_m2
    if not (MIN_FLOOR_AREA_M2 <= area <= MAX_FLOOR_AREA_M2):
        raise PipelineError(
            "LAYOUT_EXTRACTION_FAILED",
            Stage.GEOMETRY,
            f"floor area {area:.1f} m2 is outside the plausible {MIN_FLOOR_AREA_M2}"
            f"-{MAX_FLOOR_AREA_M2} m2 range",
        )

    height = geometry.profile.height_mm
    if not (MIN_CEILING_MM <= height <= MAX_CEILING_MM):
        raise PipelineError(
            "LAYOUT_EXTRACTION_FAILED",
            Stage.GEOMETRY,
            f"ceiling height {height} mm is outside {MIN_CEILING_MM}-{MAX_CEILING_MM}",
        )

    if not geometry.profile.ceiling_observed:
        warnings.append(WARN_CEILING_NOT_OBSERVED)

    by_id = {wall.id: wall for wall in geometry.walls}
    for opening in geometry.openings:
        wall = by_id.get(opening.wall_id)
        if wall is None:
            raise PipelineError(
                "LAYOUT_EXTRACTION_FAILED",
                Stage.GEOMETRY,
                f"{opening.id} references missing wall {opening.wall_id}",
            )
        if opening.offset_mm + opening.width_mm > wall.length_mm + 1.0:
            raise PipelineError(
                "LAYOUT_EXTRACTION_FAILED",
                Stage.GEOMETRY,
                f"{opening.id} extends past the end of {wall.id}",
            )
        if opening.sill_mm + opening.height_mm > height:
            # An opening that runs past the ceiling means the ceiling is
            # wrong, not the opening: a door is a better-measured thing than
            # a ceiling a phone camera barely saw. Reported as a ceiling
            # caveat, which is what a reader needs to act on.
            warnings.append(WARN_CEILING_NOT_OBSERVED)

    doors = [o for o in geometry.openings if o.type == "door"]
    for first, second in zip(doors, doors[1:], strict=False):
        if first.wall_id == second.wall_id and _overlaps(first, second):
            raise PipelineError(
                "LAYOUT_EXTRACTION_FAILED",
                Stage.GEOMETRY,
                f"doors {first.id} and {second.id} overlap on {first.wall_id}",
            )

    if not doors:
        warnings.append(WARN_DOOR_POSSIBLY_MISSED)
    if any(opening.swing == "unknown" for opening in geometry.openings if opening.type == "door"):
        # 3.6: V1 leaves swing unknown, and the validator keeps both sides
        # clear as a result (5.5 H4). The user should know why their doorway
        # has a larger keep-out than they expected.
        warnings.append(WARN_DOOR_SWING_UNKNOWN)

    perimeter = sum(wall.length_mm for wall in geometry.walls)
    open_length = sum(wall.length_mm for wall in geometry.walls if wall.kind == "open")
    if perimeter > 0 and open_length / perimeter > MAX_OPEN_PERIMETER_FRACTION:
        # Not a room we measured, a room we glimpsed. One or two open edges
        # are a genuine open-plan boundary; most of the perimeter open means
        # the walls were never filmed, and the floor extent we fell back on is
        # only telling us how far the user walked.
        raise PipelineError(
            "INSUFFICIENT_COVERAGE",
            Stage.GEOMETRY,
            f"{open_length / perimeter:.0%} of the boundary has no wall behind it",
        )
    if open_length > 0:
        warnings.append(WARN_OPEN_BOUNDARY)

    return warnings


def _overlaps(a: Opening, b: Opening) -> bool:
    return a.offset_mm < b.offset_mm + b.width_mm and b.offset_mm < a.offset_mm + a.width_mm
