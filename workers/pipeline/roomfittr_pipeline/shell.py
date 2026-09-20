"""S8: build the room shell mesh and export it as glTF (implementation-plan.md 3.7-3.8).

This is where "existing furniture removal" actually happens, and the point
worth holding on to is that no removal takes place. Decision 1 of the plan:
the shell is *generated* from measured geometry, so the furniture is absent
because it was never built, not because it was erased. That turns the hardest
research problem in V1 into mesh construction.

What gets built:

- **Floor**: the room polygon, triangulated.
- **Ceiling**: the same polygon at ceiling height, wound the other way so the
  camera can look down into the room from outside.
- **Walls**: one box per segment, 100 mm thick, with door and window
  rectangles subtracted in wall-local 2D before triangulation.
- **Glass**: a thin pane in each window, so a window reads as a window.

Units change here, and it is the one place in the pipeline where they do.
Everything upstream is integer millimetres (2.4); glTF is metres by
specification, so `MM_TO_M` is applied at export and nowhere else.

`open` wall segments get no geometry at all. They are a boundary the solver
respects, not a surface -- building a wall there would tell the user their
open-plan living room has a wall in the middle of it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import trimesh
from numpy.typing import NDArray
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry

from .geometry import Opening, RoomGeometry, WallSegment

# glTF is metres (2.4). Applied once, at export.
MM_TO_M = 0.001

# 3.7 S8: wall thickness for the shell.
WALL_THICKNESS_MM = 100.0

# Glass sits just inside the window opening so it does not z-fight the reveal.
GLASS_THICKNESS_MM = 8.0

# 3.7 S8 / 9.2: the Tier 0 budget. Checked rather than assumed, because a
# shell over budget is a 3D viewer that stalls on a phone.
TIER0_GLB_BUDGET_BYTES = 3 * 1024 * 1024

# Flat Tier 0 defaults, used when appearance sampling has nothing to say.
DEFAULT_WALL_COLOR = (0.92, 0.91, 0.89)
DEFAULT_FLOOR_COLOR = (0.76, 0.67, 0.55)
DEFAULT_CEILING_COLOR = (0.97, 0.97, 0.97)
GLASS_COLOR = (0.75, 0.85, 0.92, 0.25)


@dataclass(frozen=True, slots=True)
class ShellMesh:
    """The assembled shell, before export."""

    mesh: trimesh.Trimesh
    wall_count: int
    opening_count: int
    open_segment_count: int

    @property
    def triangle_count(self) -> int:
        return int(len(self.mesh.faces))


def _triangulate_polygon(polygon: Polygon) -> tuple[NDArray[np.float64], NDArray[np.int64]]:
    """Constrained triangulation of a simple polygon, as (vertices, triangles).

    Constrained matters: a plain Delaunay covers the convex hull, so an
    L-shaped room comes back with its notch filled in -- a floor the camera
    can walk into and the solver will place furniture on. earcut respects the
    boundary.
    """
    flat, triangles = trimesh.creation.triangulate_polygon(polygon, engine="earcut")
    return (
        np.asarray(flat, dtype=np.float64),
        np.asarray(triangles, dtype=np.int64),
    )


def _horizontal_surface(polygon: Polygon, y_mm: float, *, upward: bool) -> trimesh.Trimesh:
    """A flat slab at `y_mm`, wound so its normal points up or down.

    Winding is not cosmetic. The ceiling faces down into the room, and if it
    faced up the user orbiting above the room would see through the floor
    and into an apparently empty box.
    """
    flat, triangles = _triangulate_polygon(polygon)
    vertices = np.column_stack([flat[:, 0], np.full(len(flat), y_mm), flat[:, 1]])

    # Shapely triangles come out counter-clockwise in XZ, which is *clockwise*
    # seen from +Y, so the raw winding faces down.
    if upward:
        triangles = triangles[:, ::-1]
    return trimesh.Trimesh(vertices=vertices, faces=triangles, process=False)


def _wall_face(
    wall: WallSegment, openings: list[Opening], height_mm: float, *, thickness_mm: float
) -> trimesh.Trimesh | None:
    """One wall as a slab with its openings punched out.

    The cut is done in wall-local 2D -- u along the wall, v up from the floor
    -- where an opening is an axis-aligned rectangle and the subtraction is a
    plain polygon difference. Doing it in world space would need a 3D boolean,
    which is slower and far more fragile.
    """
    length = wall.length_mm
    if length < 1.0 or height_mm < 1.0:
        return None

    # Typed as the general geometry because a difference can return a
    # MultiPolygon (two openings that leave a wall in three pieces) or an
    # empty geometry (a doorway the width of the wall).
    face: BaseGeometry = Polygon([(0.0, 0.0), (length, 0.0), (length, height_mm), (0.0, height_mm)])
    for opening in openings:
        hole = Polygon(
            [
                (opening.offset_mm, opening.sill_mm),
                (opening.offset_mm + opening.width_mm, opening.sill_mm),
                (opening.offset_mm + opening.width_mm, opening.sill_mm + opening.height_mm),
                (opening.offset_mm, opening.sill_mm + opening.height_mm),
            ]
        )
        face = face.difference(hole)

    if face.is_empty or not isinstance(face, Polygon | MultiPolygon):
        return None

    parts: list[Polygon] = (
        [face] if isinstance(face, Polygon) else [p for p in face.geoms if isinstance(p, Polygon)]
    )
    meshes: list[trimesh.Trimesh] = []
    for part in parts:
        if part.area < 1.0:
            continue
        # No broad except here. A failure to extrude is a real problem --
        # it was previously swallowed and produced a room with no walls at
        # all, which every downstream stage then happily accepted.
        meshes.append(trimesh.creation.extrude_polygon(part, height=thickness_mm, engine="earcut"))

    if not meshes:
        return None

    face_mesh = trimesh.util.concatenate(meshes)

    # `extrude_polygon` puts the wall-local (u, v) plane in XY and extrudes
    # along +Z, which is already the orientation wanted: u runs along +X, the
    # height v along +Y, and the thickness along +Z. No standing-up rotation
    # is needed, and adding one lays every wall flat on the floor -- which
    # still produces a mesh, still exports, and looks like a room with no
    # walls only once it is opened in a viewer.
    dx, dz = wall.direction()
    heading = math.atan2(dz, dx)
    face_mesh.apply_transform(trimesh.transformations.rotation_matrix(-heading, [0.0, 1.0, 0.0]))

    # The rotation sends the thickness axis to the segment's left normal,
    # which for a counter-clockwise floor polygon points *into* the room. Left
    # there, every wall eats 100 mm of the room and a 4 m span renders as
    # 3.8 m -- an error the same size as the 5% E2 is trying to hold the whole
    # pipeline to, introduced after the measuring is done.
    #
    # S6 fits its wall lines to points on the *visible face* of each wall, so
    # the floor polygon is the inner surface. The thickness therefore belongs
    # on the far side, and the shell's interior then matches what was measured.
    inward_x, inward_z = -dz, dx
    face_mesh.apply_translation(
        [
            wall.start[0] - inward_x * thickness_mm,
            0.0,
            wall.start[1] - inward_z * thickness_mm,
        ]
    )
    return face_mesh


def _window_glass(wall: WallSegment, opening: Opening) -> trimesh.Trimesh:
    """A thin pane filling a window opening."""
    pane = trimesh.creation.box(extents=[opening.width_mm, opening.height_mm, GLASS_THICKNESS_MM])
    dx, dz = wall.direction()
    heading = math.atan2(dz, dx)
    pane.apply_transform(trimesh.transformations.rotation_matrix(-heading, [0.0, 1.0, 0.0]))

    along = opening.offset_mm + opening.width_mm / 2.0
    pane.apply_translation(
        [
            wall.start[0] + dx * along,
            opening.sill_mm + opening.height_mm / 2.0,
            wall.start[1] + dz * along,
        ]
    )
    return pane


def build_shell(geometry: RoomGeometry, *, thickness_mm: float = WALL_THICKNESS_MM) -> ShellMesh:
    """Assemble floor, ceiling and walls into one mesh.

    Colours are per-vertex rather than per-material. Tier 0 is flat colour by
    definition (3.7), and vertex colours survive the glTF round trip without
    a texture, an image encoder or a material library -- which keeps the
    Tier 0 shell comfortably inside its 3 MB budget.
    """
    height = float(geometry.profile.height_mm)
    parts: list[trimesh.Trimesh] = []

    floor = _horizontal_surface(geometry.polygon, 0.0, upward=True)
    floor.visual.vertex_colors = _rgba(DEFAULT_FLOOR_COLOR)
    parts.append(floor)

    ceiling = _horizontal_surface(geometry.polygon, height, upward=False)
    ceiling.visual.vertex_colors = _rgba(DEFAULT_CEILING_COLOR)
    parts.append(ceiling)

    openings_by_wall: dict[str, list[Opening]] = {}
    for opening in geometry.openings:
        openings_by_wall.setdefault(opening.wall_id, []).append(opening)

    wall_count = 0
    open_segments = 0
    for wall in geometry.walls:
        if wall.kind == "open":
            # No geometry: an open boundary is a boundary, not a surface.
            open_segments += 1
            continue
        face = _wall_face(
            wall, openings_by_wall.get(wall.id, []), height, thickness_mm=thickness_mm
        )
        if face is None:
            continue
        face.visual.vertex_colors = _rgba(DEFAULT_WALL_COLOR)
        parts.append(face)
        wall_count += 1

        for opening in openings_by_wall.get(wall.id, []):
            if opening.type == "window":
                glass = _window_glass(wall, opening)
                glass.visual.vertex_colors = _rgba(GLASS_COLOR)
                parts.append(glass)

    mesh = trimesh.util.concatenate(parts)
    return ShellMesh(
        mesh=mesh,
        wall_count=wall_count,
        opening_count=len(geometry.openings),
        open_segment_count=open_segments,
    )


def _rgba(color: tuple[float, ...]) -> list[int]:
    channels = list(color) + [1.0] * (4 - len(color))
    return [int(round(c * 255)) for c in channels]


def export_glb(shell: ShellMesh, destination: Path) -> int:
    """Write the shell as a binary glTF, in metres. Returns bytes written.

    The millimetre-to-metre conversion happens here and only here, on a copy,
    so the mesh a caller still holds stays in the pipeline's units.

    3.7 also calls for `gltfpack -cc -tc` (meshopt geometry compression plus
    KTX2 textures). That is a separate binary belonging in the worker image,
    and it is a packaging step rather than a modelling one: a Tier 0 shell is
    a few thousand untextured triangles and already lands far inside the
    budget without it. `check_budget` is what proves that per scan instead of
    assuming it.
    """
    scaled = shell.mesh.copy()
    scaled.apply_scale(MM_TO_M)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(trimesh.exchange.gltf.export_glb(trimesh.Scene(scaled)))
    return destination.stat().st_size


def check_budget(size_bytes: int, *, budget: int = TIER0_GLB_BUDGET_BYTES) -> None:
    """9.2's asset budget, enforced rather than documented."""
    if size_bytes > budget:
        raise ValueError(
            f"room shell is {size_bytes / 1024 / 1024:.1f} MB, over the "
            f"{budget / 1024 / 1024:.0f} MB budget of 9.2"
        )


def appearance(geometry: RoomGeometry) -> dict[str, Any]:
    """The `appearance` block of a RoomModel, Tier 0.

    Sampling real wall and floor colours from the keyframes is 3.7's Tier 0
    description and needs the frames, which live in S2's output rather than
    here. This produces the neutral defaults the mesh is built with, so the
    JSON and the GLB always agree on what the room looks like.
    """
    return {
        "tier": 0,
        "walls": {
            wall.id: {"color": _hex(DEFAULT_WALL_COLOR), "material": "paint"}
            for wall in geometry.walls
            if wall.kind == "solid"
        },
        "floor": {"color": _hex(DEFAULT_FLOOR_COLOR), "material": "wood"},
        "ceiling": {"color": _hex(DEFAULT_CEILING_COLOR), "material": "paint"},
    }


def _hex(color: tuple[float, ...]) -> str:
    return "#" + "".join(f"{int(round(c * 255)):02x}" for c in color[:3])
