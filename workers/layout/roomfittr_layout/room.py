"""L1 room analysis: what the room offers before anything is placed (5.2).

Everything here is derived deterministically from a `RoomModel`. The LLM never
sees this -- it gets the compact summary at the bottom -- and the solver and
the validator both read it, so a door keep-out means the same thing to the
thing that places furniture and the thing that judges it.

The circulation grid deserves a note. H6 requires that every door stays
reachable and that most of the floor stays connected, which is a question
about *paths*, not about distances between pairs of objects. Two items can
each be a comfortable distance from everything and still seal off a corner
between them. So the room is rasterised, the occupied cells are grown by a
person's radius, and connectivity is answered by flood fill -- which is the
only way to ask the question that is actually being asked.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .geometry import Footprint, rectangle_from_wall

# 5.2: door keep-out is `width x width` into the room, plus a 900 mm approach.
DOOR_APPROACH_MM = 900.0

# 5.2: the zone in front of a window that tall items should not occupy.
WINDOW_ZONE_DEPTH_MM = 600.0

# 5.2 / H6: occupancy grid at 50 mm, dilated by a 380 mm person radius, which
# is the 760 mm minimum walkway H6 enforces.
GRID_MM = 50.0
PERSON_RADIUS_MM = 380.0

# H6: a layout must leave this share of the free floor connected. Below it,
# the room has been cut into pieces even if each piece is reachable from a door.
MIN_CONNECTED_FRACTION = 0.6

# A wall run shorter than this holds nothing in the V1 category vocabulary --
# the narrowest thing we place is a nightstand at ~400 mm.
MIN_USABLE_RUN_MM = 400.0


@dataclass(frozen=True, slots=True)
class Wall:
    id: str
    start: tuple[float, float]
    end: tuple[float, float]
    kind: str

    @property
    def length_mm(self) -> float:
        return math.dist(self.start, self.end)

    def direction(self) -> tuple[float, float]:
        length = self.length_mm
        if length < 1e-9:
            return (0.0, 0.0)
        return ((self.end[0] - self.start[0]) / length, (self.end[1] - self.start[1]) / length)

    def inward_normal(self) -> tuple[float, float]:
        """Left normal, which points into the room for a CCW floor polygon."""
        dx, dz = self.direction()
        return (-dz, dx)

    def point_at(self, offset_mm: float) -> tuple[float, float]:
        dx, dz = self.direction()
        return (self.start[0] + dx * offset_mm, self.start[1] + dz * offset_mm)


@dataclass(frozen=True, slots=True)
class Opening:
    id: str
    wall_id: str
    type: str
    offset_mm: float
    width_mm: float
    sill_mm: float
    height_mm: float
    swing: str = "unknown"


@dataclass(frozen=True, slots=True)
class Obstacle:
    id: str
    label: str
    footprint: Footprint


@dataclass(frozen=True, slots=True)
class FreeRun:
    """An uninterrupted stretch of a wall, available to place against."""

    wall_id: str
    start_mm: float
    length_mm: float

    @property
    def end_mm(self) -> float:
        return self.start_mm + self.length_mm


@dataclass(frozen=True, slots=True)
class CirculationGrid:
    """The room rasterised for connectivity questions."""

    walkable: NDArray[np.bool_]
    origin_x_mm: float
    origin_z_mm: float
    cell_mm: float

    def cell_of(self, x_mm: float, z_mm: float) -> tuple[int, int]:
        return (
            int((z_mm - self.origin_z_mm) // self.cell_mm),
            int((x_mm - self.origin_x_mm) // self.cell_mm),
        )

    @property
    def walkable_cells(self) -> int:
        return int(self.walkable.sum())


@dataclass(frozen=True, slots=True)
class RoomAnalysis:
    """Everything L1 derives. Read by the solver and by the validator."""

    floor: Polygon
    walls: list[Wall]
    openings: list[Opening]
    obstacles: list[Obstacle]
    ceiling_height_mm: float
    door_keepouts: dict[str, Polygon]
    window_zones: dict[str, Polygon]
    usable_floor: Polygon
    free_runs: list[FreeRun]
    warnings: list[str] = field(default_factory=list)

    @property
    def area_m2(self) -> float:
        return self.floor.area / 1e6

    def wall(self, wall_id: str) -> Wall | None:
        return next((w for w in self.walls if w.id == wall_id), None)

    def runs_for(self, wall_id: str) -> list[FreeRun]:
        return [run for run in self.free_runs if run.wall_id == wall_id]


def _footprint_from_obstacle(raw: dict[str, Any]) -> Footprint:
    size = raw["size"]
    return Footprint(
        center_x_mm=float(raw["center"][0]),
        center_z_mm=float(raw["center"][1]),
        width_mm=float(size[0]),
        depth_mm=float(size[1]),
        height_mm=float(size[2]),
        rotation_deg=float(raw.get("yaw_deg", 0.0)),
    )


def door_keepout(wall: Wall, opening: Opening) -> Polygon:
    """5.2: a `width x width` swing area plus a 900 mm approach zone.

    When the swing is unknown -- which V1 almost always leaves it (3.6) -- the
    keep-out covers the full width either side of the opening rather than
    guessing a hinge. Guessing wrong puts a bookcase where the door opens,
    and the user discovers it when the door hits it.
    """
    swing_depth = max(opening.width_mm, DOOR_APPROACH_MM)
    if opening.swing in ("left", "right"):
        return rectangle_from_wall(
            wall.start, wall.end, opening.offset_mm, opening.width_mm, swing_depth
        )

    # Unknown: widen along the wall by a door's width on each side, since the
    # leaf could sweep either way.
    start = max(opening.offset_mm - opening.width_mm, 0.0)
    end = min(opening.offset_mm + 2 * opening.width_mm, wall.length_mm)
    return rectangle_from_wall(wall.start, wall.end, start, max(end - start, 1.0), swing_depth)


def window_zone(wall: Wall, opening: Opening) -> Polygon:
    """5.2: the 600 mm-deep strip in front of a window.

    Not a keep-out. S3 only warns, and only for items tall enough to block
    the view -- a rug in front of a window is fine.
    """
    return rectangle_from_wall(
        wall.start, wall.end, opening.offset_mm, opening.width_mm, WINDOW_ZONE_DEPTH_MM
    )


def free_runs(wall: Wall, openings: list[Opening], obstacles: list[Obstacle]) -> list[FreeRun]:
    """The stretches of a wall with nothing in front of them.

    `open` walls return nothing: 3.6 says the solver treats them as a boundary
    but never places against them, and a sofa backed onto thin air is exactly
    the result that would embarrass the product.
    """
    if wall.kind != "solid" or wall.length_mm < MIN_USABLE_RUN_MM:
        return []

    blocked: list[tuple[float, float]] = []
    for opening in openings:
        if opening.wall_id != wall.id:
            continue
        if opening.type == "window" and opening.sill_mm >= 700.0:
            # A high window leaves the wall below it usable: a sideboard can
            # sit under it. Only floor-length openings interrupt the run.
            continue
        blocked.append((opening.offset_mm, opening.offset_mm + opening.width_mm))

    # An obstacle against this wall blocks the run behind it.
    dx, dz = wall.direction()
    for obstacle in obstacles:
        projections = [
            (x - wall.start[0]) * dx + (z - wall.start[1]) * dz
            for x, z in obstacle.footprint.corners()
        ]
        distances = [
            abs((x - wall.start[0]) * -dz + (z - wall.start[1]) * dx)
            for x, z in obstacle.footprint.corners()
        ]
        if min(distances) > 300.0:  # not against this wall
            continue
        lo, hi = max(min(projections), 0.0), min(max(projections), wall.length_mm)
        if hi > lo:
            blocked.append((lo, hi))

    blocked.sort()
    runs: list[FreeRun] = []
    cursor = 0.0
    for lo, hi in blocked:
        if lo - cursor >= MIN_USABLE_RUN_MM:
            runs.append(FreeRun(wall.id, cursor, lo - cursor))
        cursor = max(cursor, hi)
    if wall.length_mm - cursor >= MIN_USABLE_RUN_MM:
        runs.append(FreeRun(wall.id, cursor, wall.length_mm - cursor))
    return runs


def circulation_grid(
    floor: Polygon, blocked: list[Polygon], *, cell_mm: float = GRID_MM
) -> CirculationGrid:
    """Rasterise the room and grow the obstacles by a person's radius.

    Dilating the obstacles rather than shrinking the person is the standard
    configuration-space trick: it turns "can a 760 mm-wide person get
    through?" into "is there a path of free cells?", which a flood fill
    answers exactly.
    """
    min_x, min_z, max_x, max_z = floor.bounds
    columns = max(int(math.ceil((max_x - min_x) / cell_mm)), 1)
    rows = max(int(math.ceil((max_z - min_z) / cell_mm)), 1)

    xs = min_x + (np.arange(columns) + 0.5) * cell_mm
    zs = min_z + (np.arange(rows) + 0.5) * cell_mm
    grid_x, grid_z = np.meshgrid(xs, zs)

    # `Polygon.contains` per point is far too slow at this resolution --
    # a 5 x 4 m room is ~8000 cells. `contains_xy` is the vectorised form.
    from shapely import contains_xy

    inside: NDArray[np.bool_] = np.asarray(contains_xy(floor, grid_x, grid_z), dtype=bool)

    occupied: NDArray[np.bool_] = np.zeros((rows, columns), dtype=bool)
    if blocked:
        merged = unary_union([shape for shape in blocked if not shape.is_empty])
        if not merged.is_empty:
            # Dilate by the person radius, then mark the cells it covers.
            grown = merged.buffer(PERSON_RADIUS_MM)
            occupied = np.asarray(contains_xy(grown, grid_x, grid_z), dtype=bool)

    # The walls themselves are an obstacle: a person cannot stand with their
    # centre closer than their radius to a wall.
    near_wall: NDArray[np.bool_] = ~np.asarray(
        contains_xy(floor.buffer(-PERSON_RADIUS_MM), grid_x, grid_z), dtype=bool
    )

    walkable: NDArray[np.bool_] = inside & ~occupied & ~near_wall
    return CirculationGrid(walkable=walkable, origin_x_mm=min_x, origin_z_mm=min_z, cell_mm=cell_mm)


def connected_component(grid: CirculationGrid, seed: tuple[int, int]) -> NDArray[np.bool_]:
    """Flood fill from `seed` over walkable cells, 4-connected.

    4-connected rather than 8-: diagonal moves would let a person slip
    between two items touching at a corner, which they cannot do.
    """
    rows, columns = grid.walkable.shape
    seen = np.zeros_like(grid.walkable)
    if not (0 <= seed[0] < rows and 0 <= seed[1] < columns) or not grid.walkable[seed]:
        return seen

    queue = deque([seed])
    seen[seed] = True
    while queue:
        row, column = queue.popleft()
        for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nr, nc = row + dr, column + dc
            if 0 <= nr < rows and 0 <= nc < columns and grid.walkable[nr, nc] and not seen[nr, nc]:
                seen[nr, nc] = True
                queue.append((nr, nc))
    return seen


def door_standing_point(wall: Wall, opening: Opening) -> tuple[float, float]:
    """A point just inside the room in front of a door.

    Where a person entering would stand. H6 asks whether they can get from
    here to everywhere else, so the point has to be far enough in to clear
    the wall dilation.
    """
    centre = wall.point_at(opening.offset_mm + opening.width_mm / 2.0)
    nx, nz = wall.inward_normal()
    reach = PERSON_RADIUS_MM + GRID_MM
    return (centre[0] + nx * reach, centre[1] + nz * reach)


def analyse(room_model: dict[str, Any]) -> RoomAnalysis:
    """Run L1 over a `RoomModel` document."""
    floor = Polygon([(float(x), float(z)) for x, z in room_model["floor_polygon"]])
    if not floor.is_valid:
        floor = floor.buffer(0)

    walls = [
        Wall(
            id=raw["id"],
            start=(float(raw["start"][0]), float(raw["start"][1])),
            end=(float(raw["end"][0]), float(raw["end"][1])),
            kind=raw["kind"],
        )
        for raw in room_model["walls"]
    ]
    openings = [
        Opening(
            id=raw["id"],
            wall_id=raw["wall_id"],
            type=raw["type"],
            offset_mm=float(raw["offset_mm"]),
            width_mm=float(raw["width_mm"]),
            sill_mm=float(raw["sill_mm"]),
            height_mm=float(raw["height_mm"]),
            swing=raw.get("swing", "unknown"),
        )
        for raw in room_model["openings"]
    ]
    obstacles = [
        Obstacle(id=raw["id"], label=raw["label"], footprint=_footprint_from_obstacle(raw))
        for raw in room_model.get("fixed_obstacles", [])
    ]

    by_id = {wall.id: wall for wall in walls}
    door_keepouts: dict[str, Polygon] = {}
    window_zones: dict[str, Polygon] = {}
    for opening in openings:
        wall = by_id.get(opening.wall_id)
        if wall is None:
            continue
        if opening.type in ("door", "passage"):
            door_keepouts[opening.id] = door_keepout(wall, opening)
        elif opening.type == "window":
            window_zones[opening.id] = window_zone(wall, opening)

    blocking = [o.footprint.polygon() for o in obstacles] + list(door_keepouts.values())
    remaining: BaseGeometry = floor
    for shape in blocking:
        remaining = remaining.difference(shape)
    # A door keep-out can cut a corner off entirely. The largest piece is the
    # room; the offcut is a sliver behind the door that nothing fits in.
    usable = (
        max(remaining.geoms, key=lambda g: g.area)
        if isinstance(remaining, MultiPolygon)
        else remaining
    )

    runs: list[FreeRun] = []
    for wall in walls:
        runs.extend(free_runs(wall, openings, obstacles))

    warnings: list[str] = []
    if not any(o.type == "door" for o in openings):
        warnings.append("no_door")
    if not runs:
        warnings.append("no_free_wall")

    return RoomAnalysis(
        floor=floor,
        walls=walls,
        openings=openings,
        obstacles=obstacles,
        ceiling_height_mm=float(room_model["ceiling_height_mm"]),
        door_keepouts=door_keepouts,
        window_zones=window_zones,
        usable_floor=usable if isinstance(usable, Polygon) else floor,
        free_runs=runs,
        warnings=warnings,
    )


def llm_summary(analysis: RoomAnalysis, *, room_type_guess: str | None = None) -> dict[str, Any]:
    """The compact JSON the planner sends to Claude (5.2).

    Deliberately not a geometry dump. The LLM's job is to choose categories
    and spatial *intents* referring to ids; it never emits coordinates
    (Decision 4), so it does not need vertices -- and feeding it thousands of
    numbers would cost tokens and invite it to reason about geometry it is
    not the authority on.
    """
    walls: list[dict[str, Any]] = []
    for wall in analysis.walls:
        runs = analysis.runs_for(wall.id)
        has = [f"{o.id}:{o.type}" for o in analysis.openings if o.wall_id == wall.id]
        walls.append(
            {
                "id": wall.id,
                "kind": wall.kind,
                "length_mm": int(round(wall.length_mm)),
                "free_runs_mm": [int(round(run.length_mm)) for run in runs],
                "has": has,
            }
        )

    longest = max((run for run in analysis.free_runs), key=lambda r: r.length_mm, default=None)
    focal: list[str] = []
    window_walls = {o.wall_id for o in analysis.openings if o.type == "window"}
    focal.extend(f"{wall_id} (window wall)" for wall_id in sorted(window_walls))
    if longest is not None:
        focal.append(f"{longest.wall_id} (longest free run {longest.length_mm / 1000:.1f}m)")

    return {
        "room_type_guess": room_type_guess,
        "area_m2": round(analysis.area_m2, 1),
        "ceiling_mm": int(round(analysis.ceiling_height_mm)),
        "shape": _shape_of(analysis.floor),
        "walls": walls,
        "openings": [
            {
                "id": o.id,
                "type": o.type,
                "wall": o.wall_id,
                "width_mm": int(round(o.width_mm)),
            }
            for o in analysis.openings
        ],
        "fixed": [{"id": o.id, "label": o.label} for o in analysis.obstacles],
        "focal_candidates": focal,
    }


def _shape_of(floor: Polygon) -> str:
    corners = len(floor.exterior.coords) - 1
    if corners <= 4:
        return "rectangular"
    if corners <= 6:
        return "L"
    return "irregular"
