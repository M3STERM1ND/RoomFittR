"""Deriving a room's ground-truth layout from a point cloud.

Dataset-agnostic on purpose: everything here takes plain arrays and returns
plain numbers, so an adapter does the file reading and calls into this.
`arkitscenes.py` is the current caller.

**Why this is not circular.** S6 solves "recover a floor plan from phone video",
where the point cloud is sparse, noisy, partial and of unknown scale. Here the
input is a laser scan: dense, complete, metrically correct and already
gravity-aligned. Fitting a polygon to that is a different and far easier
problem, and none of this code is shared with the pipeline.

It is not *zero* circularity, and `TIER_A_GUIDE.md` says so plainly: the floor
is found geometrically, which is the same class of operation S6 performs. This
measures S6 against a much better instrument, not against an independent
oracle. Hand measurement (Tier B) is the independent check.

Conventions follow implementation-plan.md 2.4: integer millimetres, right-handed
Y-up, floor at y = 0, origin at the floor polygon's centroid. ARKit world space
is already Y-up and gravity-aligned, so no axis conversion is needed; a Z-up
dataset would need one adding here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import shapely
from shapely.geometry import MultiPolygon, Polygon, box
from shapely.geometry.base import BaseGeometry

# Floor points are rasterised before a boundary is traced. 50 mm is measured,
# not guessed: at 100 mm the worst-case wall error on decimated, jittered
# clouds was 5.3%, at 50 mm it is 2.6%, and at 25 mm the grid outruns the
# point density, cells go empty and the trace collapses entirely. The
# residual quantisation is then removed by `refine_edges`.
FLOOR_GRID_MM = 50.0

# A cell counts as floor if it holds at least this fraction of the *typical*
# cell's point count. An absolute threshold was the first attempt and was wrong:
# it silently assumes a uniform point density, so wherever the scan thins out
# (near walls, in a scanner's shadow) cells drop below it and the floor
# disintegrates into disconnected squares. Scoring against the observed median
# adapts to whatever density the scene actually has.
MIN_CELL_DENSITY_RATIO = 0.25

# After tracing, the boundary is a staircase of grid cells. Simplifying at half
# a grid step collapses it back to straight walls without eating real corners.
SIMPLIFY_TOLERANCE_MM = FLOOR_GRID_MM * 1.5

# How far the morphological closing reaches, as a multiple of the grid cell.
# Measured against a fused depth-camera scan, not guessed: the floor immediately
# against a wall is often never scanned (grazing incidence, furniture), leaving
# a band of empty cells that tears the outline into a staircase. At 0.75 cells
# the outline came out with 12 corners instead of 4; at 4 cells and above it is
# a clean rectangle. 5 cells (250 mm) bridges gaps up to half a metre while
# staying far below any genuine concave feature -- an L-shaped room's notch is
# metres across, and it survives this untouched.
CLOSING_CELLS = 5.0

# Polygon edges shorter than this are dropped: they are rasterisation artefacts
# at corners, not walls. A real wall segment in a home is longer than 25 cm.
MIN_WALL_LENGTH_MM = 250.0

# How far an opening's centre may sit from a wall and still be assigned to it.
# Door and window frames have depth, and the scan sees into the reveal.
OPENING_TO_WALL_MAX_MM = 700.0


class DerivationError(RuntimeError):
    """The cloud does not support an honest ground truth for this room.

    Raised rather than returning something approximate. A wrong yardstick is
    worse than a missing one: it silently moves every metric scored against it.
    """


@dataclass(frozen=True)
class Wall:
    id: str
    start: tuple[float, float]
    end: tuple[float, float]

    @property
    def length_mm(self) -> float:
        return math.dist(self.start, self.end)


@dataclass(frozen=True)
class DerivedLayout:
    ceiling_height_mm: int
    floor_polygon_mm: list[list[int]]
    walls: list[dict[str, Any]]
    openings: list[dict[str, Any]]
    # Kept so the caller can place openings and report what was thrown away.
    _walls: list[Wall]


def _largest_polygon(geom: BaseGeometry) -> Polygon:
    if isinstance(geom, Polygon):
        return geom
    if not isinstance(geom, MultiPolygon):
        raise DerivationError(f"expected polygonal geometry, got {geom.geom_type}")
    if not geom.geoms:
        raise DerivationError("floor rasterisation produced no area")
    # A scan often catches floor through an open door. The room is the biggest
    # connected piece; the hallway beyond it is not part of this room.
    return max(geom.geoms, key=lambda p: p.area)


def floor_polygon(floor_xz: np.ndarray) -> Polygon:
    """Trace the floor's outline by rasterising and dissolving the cells.

    A concave hull was the obvious first choice and is the wrong tool: its
    result swings on a ratio parameter, and it cuts corners off L-shaped rooms,
    which are exactly the rooms in the set for E4.
    """
    if len(floor_xz) < 4:
        raise DerivationError(f"only {len(floor_xz)} floor points; cannot trace an outline")

    cells = np.floor(floor_xz / FLOOR_GRID_MM).astype(np.int64)
    keys, counts = np.unique(cells, axis=0, return_counts=True)
    threshold = max(1.0, float(np.median(counts)) * MIN_CELL_DENSITY_RATIO)
    occupied = keys[counts >= threshold]
    if not len(occupied):
        raise DerivationError("no floor cell reached the density threshold")

    squares = [
        box(
            float(i) * FLOOR_GRID_MM,
            float(j) * FLOOR_GRID_MM,
            float(i + 1) * FLOOR_GRID_MM,
            float(j + 1) * FLOOR_GRID_MM,
        )
        for i, j in occupied
    ]
    merged = shapely.unary_union(squares)

    # Two corrections in one pair of buffers, both with mitred joins so the
    # right angles a room is made of survive (the default round join bevels
    # every corner).
    #
    # 1. Closing. Two problems at once: cells meeting only at a corner are not
    #    merged by unary_union, and real scans leave whole bands of floor
    #    unscanned against the walls. Growing by CLOSING_CELLS and shrinking
    #    back bridges both.
    # 2. De-biasing. A cell covers [i*g, (i+1)*g) and is marked occupied by a
    #    point anywhere inside it, so the traced outline sits up to a full cell
    #    outside the real boundary -- a 4000 mm room rasterises to 4100 mm.
    #    That is a systematic 2.5% overestimate on a 4 m wall, which is large
    #    next to E2's 5% threshold, and it inflates every wall in the same
    #    direction rather than averaging out. Shrinking by half a cell more
    #    than we grew centres the estimate.
    grow = FLOOR_GRID_MM * CLOSING_CELLS
    shrink = grow + FLOOR_GRID_MM / 2.0
    closed = merged.buffer(grow, join_style="mitre").buffer(-shrink, join_style="mitre")
    dissolved = _largest_polygon(closed if not closed.is_empty else merged)

    # Interior holes are furniture the scanner could not see under, never real
    # holes in a floor. Keeping only the exterior ring discards them.
    outline = Polygon(dissolved.exterior)
    simplified = outline.simplify(SIMPLIFY_TOLERANCE_MM, preserve_topology=True)
    result = _largest_polygon(simplified) if not simplified.is_empty else outline
    if result.area <= 0:
        raise DerivationError("traced floor has no area")
    # Orient *before* refining, not just at the end. `refine_edges` works with
    # each edge's outward normal, and which way that points depends entirely on
    # the ring's winding -- on a ring of unknown orientation the refinement
    # pushes some walls out and others in, and quietly cancels itself out.
    result = shapely.geometry.polygon.orient(result, sign=1.0)  # CCW, per 2.4
    result = refine_edges(result, floor_xz)
    return shapely.geometry.polygon.orient(result, sign=1.0)


# How far from an edge a floor point may be and still be treated as evidence of
# where that edge belongs. Just over a cell: far enough to catch the points the
# rasterisation rounded away, near enough to ignore the opposite wall.
EDGE_REFINE_BAND_MM = FLOOR_GRID_MM * 1.5

# The outermost floor points against a wall, but not literally the outermost:
# a percentile ignores the stray reflections every scan has beyond the wall.
# Tuned down from 97: on a noisy cloud the top few percent are scatter past
# the wall, and chasing them pushed walls outward by more than the
# quantisation the refinement exists to remove.
EDGE_REFINE_PERCENTILE = 90.0

# Corners are recovered by intersecting adjacent edges, which is unstable when
# they are nearly parallel. Below this angle the original corner is kept.
MIN_CORNER_ANGLE_DEG = 20.0


def refine_edges(poly: Polygon, floor_xz: np.ndarray) -> Polygon:
    """Slide each traced edge onto the floor points that justify it.

    Rasterising is good at *topology* -- how many walls there are and how they
    connect -- and bad at position, because a cell is half a grid step wide and
    the outline can only land on cell boundaries. That leaves every wall with up
    to a half-cell error in the same outward direction. On a 4 m wall at a
    50 mm grid that is well over 0.5%, and since this polygon is the yardstick
    E2 and E4 are scored against, its error comes straight off their budget.

    So the shape is kept and each edge is re-fitted: take the floor points lying
    in a band along it, move the edge's line out to the 97th percentile of their
    perpendicular distance, then re-intersect consecutive lines to get corners.
    Edges with too little evidence, and corners between near-parallel edges, are
    left exactly where tracing put them.
    """
    ring: list[tuple[float, float]] = [(float(x), float(y)) for x, y in poly.exterior.coords[:-1]]
    n = len(ring)
    if n < 3 or len(floor_xz) < 8:
        return poly

    pts = np.asarray(floor_xz, dtype=float)
    lines: list[tuple[np.ndarray, np.ndarray]] = []  # (point on line, unit normal)

    for i in range(n):
        a = np.array(ring[i], dtype=float)
        b = np.array(ring[(i + 1) % n], dtype=float)
        edge = b - a
        length = float(np.hypot(*edge))
        if length < MIN_WALL_LENGTH_MM:
            lines.append((a, np.array([0.0, 0.0])))
            continue

        direction = edge / length
        # CCW ring: the interior is to the left, so this points outward.
        normal = np.array([direction[1], -direction[0]])

        rel = pts - a
        along = rel @ direction
        perp = rel @ normal
        # Ignore the ends: points near a corner belong to both walls and would
        # drag each one towards the other.
        band = (
            (along > length * 0.15) & (along < length * 0.85) & (np.abs(perp) < EDGE_REFINE_BAND_MM)
        )
        if band.sum() < 8:
            lines.append((a, normal))
            continue

        # perp is measured along the outward normal, so the floor points sit
        # at negative values and the wall is where they run out: the high
        # percentile is the outermost evidence, trimmed of stray reflections.
        offset = float(np.percentile(perp[band], EDGE_REFINE_PERCENTILE))

        # De-biasing already centred the trace, so the error left to remove is
        # at most half a cell. A larger correction is therefore not
        # quantisation: it is the band having caught something it should not
        # have, such as a doorway reveal, or scatter past the wall on a noisy
        # cloud. Refusing those bounds the worst case to the accuracy the raster
        # already had, instead of trading a reliable small error for an
        # occasional large one. Note the bound is per edge, so a room's *width*
        # can still move by a full cell when both its walls shift outward.
        if abs(offset) > FLOOR_GRID_MM / 2.0:
            lines.append((a, normal))
            continue
        lines.append((a + normal * offset, normal))

    refined: list[tuple[float, float]] = []
    for i in range(n):
        p0, n0 = lines[i - 1]
        p1, n1 = lines[i]
        if not n0.any() or not n1.any():
            refined.append(ring[i])
            continue
        cross = float(n0[0] * n1[1] - n0[1] * n1[0])
        if abs(cross) < math.sin(math.radians(MIN_CORNER_ANGLE_DEG)):
            refined.append(ring[i])
            continue
        # Intersect the two lines n.(x - p) = 0.
        c0 = float(n0 @ p0)
        c1 = float(n1 @ p1)
        x = (c0 * n1[1] - c1 * n0[1]) / cross
        y = (c1 * n0[0] - c0 * n1[0]) / cross
        refined.append((x, y))

    candidate = Polygon(refined)
    # Refinement must not be allowed to make things worse: a self-intersecting
    # or wildly different polygon means the evidence did not support it.
    if not candidate.is_valid or candidate.area <= 0:
        return poly
    if not 0.8 <= candidate.area / poly.area <= 1.25:
        return poly
    return candidate


def polygon_to_walls(poly: Polygon) -> tuple[list[list[int]], list[Wall]]:
    """Recentre on the centroid and turn each edge into a wall.

    One wall per polygon edge is the same invariant the room fixtures hold and
    that `test_fixture_geometry_is_self_consistent` enforces, so ground truth
    and fixtures stay describable by the same schema.
    """
    cx, cy = poly.centroid.x, poly.centroid.y
    ring = [(x - cx, y - cy) for x, y in poly.exterior.coords[:-1]]

    # Drop near-duplicate corners left by simplification before measuring, or a
    # 3 mm sliver becomes a "wall" and shifts every subsequent wall id.
    kept: list[tuple[float, float]] = []
    for pt in ring:
        if not kept or math.dist(pt, kept[-1]) >= MIN_WALL_LENGTH_MM:
            kept.append(pt)
    if len(kept) >= 2 and math.dist(kept[0], kept[-1]) < MIN_WALL_LENGTH_MM:
        kept.pop()
    if len(kept) < 3:
        raise DerivationError(f"floor outline collapsed to {len(kept)} corner(s)")

    rounded = [[int(round(x)), int(round(y))] for x, y in kept]
    walls = [
        Wall(
            id=f"W{i + 1}",
            start=(rounded[i][0], rounded[i][1]),
            end=(rounded[(i + 1) % len(rounded)][0], rounded[(i + 1) % len(rounded)][1]),
        )
        for i in range(len(rounded))
    ]
    return rounded, walls


# Height histogram bin for finding the floor and ceiling planes. Fine enough to
# separate a floor from a rug or a low plinth, coarse enough that a real plane
# lands in one bin rather than smearing across ten.
SURFACE_BIN_MM = 50.0

# Points within this of a detected plane's peak belong to it. A real floor is
# not perfectly flat and the scan is not perfectly registered.
SURFACE_BAND_MM = 80.0

# The floor is searched for in the bottom of the height range and the ceiling in
# the top. This is a coarse guard only; what actually picks the planes is the
# rule below.
SURFACE_SEARCH_FRACTION = 0.35

# A histogram bin is a real surface if it holds at least this share of the
# busiest bin. Everything else is scatter.
SURFACE_PEAK_RATIO = 0.05


def split_horizontal_surfaces(points_mm: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Find the floor and ceiling planes by height histogram.

    A dataset that ships human-annotated `floor` and `ceiling` labels makes this
    unnecessary. ARKitScenes does not -- its taxonomy is 17 furniture classes --
    so the two dominant horizontal planes have to be found geometrically.

    Returns boolean masks over `points_mm` for (floor, ceiling). Raises rather
    than guessing when either plane is not clearly there: a room whose ceiling
    was never scanned has no ceiling height, and inventing one would put a wrong
    number into the yardstick.
    """
    if points_mm.ndim != 2 or points_mm.shape[1] != 3:
        raise DerivationError(f"expected an (N, 3) array of points, got {points_mm.shape}")
    if len(points_mm) < 100:
        raise DerivationError(f"only {len(points_mm)} points; cannot find floor and ceiling")

    y = points_mm[:, 1]
    low, high = float(y.min()), float(y.max())
    span = high - low
    if span < 2000.0:
        raise DerivationError(
            f"vertical extent is only {span:.0f} mm; this is not a whole room "
            "(or the camera convention is wrong and the cloud is collapsed)"
        )

    bins = np.arange(low, high + SURFACE_BIN_MM, SURFACE_BIN_MM)
    counts, edges = np.histogram(y, bins=bins)
    centres = (edges[:-1] + edges[1:]) / 2.0

    window = span * SURFACE_SEARCH_FRACTION
    lower_zone = centres <= low + window
    upper_zone = centres >= high - window
    if not lower_zone.any() or not upper_zone.any():
        raise DerivationError("height histogram has no distinct lower and upper zones")

    # The floor is the *lowest* substantial horizontal surface, not the busiest
    # one, and the ceiling is the highest. Taking the busiest bin was the first
    # attempt and it fails exactly where it matters: in a cluttered room a desk
    # or table top easily holds more points than the floor around it, and the
    # room then comes out 750 mm tall. Height ordering is what distinguishes a
    # floor from furniture; point count is not.
    significant = counts >= max(counts.max() * SURFACE_PEAK_RATIO, 1.0)
    floor_candidates = np.flatnonzero(significant & lower_zone)
    ceiling_candidates = np.flatnonzero(significant & upper_zone)
    if not len(floor_candidates) or not len(ceiling_candidates):
        raise DerivationError("no substantial horizontal surface near the floor or the ceiling")

    floor_y = float(centres[floor_candidates[0]])
    ceiling_y = float(centres[ceiling_candidates[-1]])

    floor_mask = np.abs(y - floor_y) <= SURFACE_BAND_MM
    ceiling_mask = np.abs(y - ceiling_y) <= SURFACE_BAND_MM

    # A plane made of a handful of points is noise, not a surface. 0.5% of the
    # cloud is a low bar that a real floor clears by an order of magnitude.
    floor_share = floor_mask.sum() / len(points_mm)
    ceiling_share = ceiling_mask.sum() / len(points_mm)
    if floor_share < 0.005:
        raise DerivationError(f"no floor plane found (best candidate held {floor_share:.2%})")
    if ceiling_share < 0.005:
        raise DerivationError(
            f"no ceiling plane found (best candidate held {ceiling_share:.2%}); "
            "the ceiling was probably never scanned"
        )
    return floor_mask, ceiling_mask


def ceiling_height(floor_y: np.ndarray, ceiling_y: np.ndarray) -> int:
    """Median-to-median, so a light fitting or a rug does not set the height."""
    if not len(floor_y) or not len(ceiling_y):
        raise DerivationError("need both floor and ceiling points for a height")
    height = float(np.median(ceiling_y) - np.median(floor_y))
    # 3.6's sanity window, and the range the schema accepts. Outside it the
    # labels are wrong -- usually a mezzanine or a stairwell caught in the scan.
    if not 2000.0 <= height <= 4500.0:
        raise DerivationError(f"derived ceiling height {height:.0f} mm is outside 2000-4500")
    return int(round(height))


def _point_to_segment(
    p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
) -> tuple[float, float]:
    """Distance from p to segment ab, and how far along ab the foot sits."""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return math.dist(p, a), 0.0
    t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    foot = (ax + t * dx, ay + t * dy)
    return math.dist(p, foot), t * math.sqrt(length_sq)


def assign_opening(corners_xz: np.ndarray, walls: list[Wall]) -> tuple[str, float, float] | None:
    """Put an opening on a wall: (wall_id, offset from wall start, width).

    Returns None when nothing is close enough. A door in an interior partition
    that is not part of this room's outline has no wall to belong to, and
    inventing one would create a false positive in E5's ground truth.
    """
    if not len(corners_xz):
        return None
    centre = (float(corners_xz[:, 0].mean()), float(corners_xz[:, 1].mean()))

    best: tuple[float, Wall] | None = None
    for wall in walls:
        dist, _ = _point_to_segment(centre, wall.start, wall.end)
        if best is None or dist < best[0]:
            best = (dist, wall)
    if best is None or best[0] > OPENING_TO_WALL_MAX_MM:
        return None

    wall = best[1]
    # Width is measured along the wall, not as a bounding-box diagonal: a
    # window in a wall running diagonally in world space would otherwise come
    # out wider than it is.
    offsets = [
        _point_to_segment((float(x), float(z)), wall.start, wall.end)[1] for x, z in corners_xz
    ]
    lo, hi = min(offsets), max(offsets)
    width = hi - lo
    if width < 1.0:
        return None
    return wall.id, lo, width
