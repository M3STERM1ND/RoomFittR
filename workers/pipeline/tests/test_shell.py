"""S8 shell mesh and glTF export.

The plan's Phase 2 test list asks for "mesh validity (watertight walls,
cutouts inside walls, correct normals); GLB size budget test". Those are
checkable now, on geometry built by hand, and they are worth having before
the GPU stages exist: a shell with its ceiling wound the wrong way looks
fine in a unit test and like an empty box in the viewer.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from roomfittr_pipeline import shell
from roomfittr_pipeline.geometry import HeightProfile, Opening, RoomGeometry, WallSegment
from shapely.geometry import Polygon

CEILING_MM = 2500


def rectangular(
    width: float = 4000.0,
    depth: float = 3000.0,
    *,
    openings: list[Opening] | None = None,
    open_wall: str | None = None,
) -> RoomGeometry:
    corners = [(0.0, 0.0), (width, 0.0), (width, depth), (0.0, depth)]
    polygon = Polygon(corners)
    walls = [
        WallSegment(
            id=f"W{i + 1}",
            start=corners[i],
            end=corners[(i + 1) % 4],
            kind="open" if f"W{i + 1}" == open_wall else "solid",
            evidence_coverage=0.1 if f"W{i + 1}" == open_wall else 0.9,
        )
        for i in range(4)
    ]
    return RoomGeometry(
        polygon=polygon,
        walls=walls,
        openings=openings or [],
        profile=HeightProfile(floor_mm=0.0, ceiling_mm=float(CEILING_MM), ceiling_observed=True),
    )


def l_shaped() -> RoomGeometry:
    corners = [
        (0.0, 0.0),
        (5000.0, 0.0),
        (5000.0, 2500.0),
        (3000.0, 2500.0),
        (3000.0, 4000.0),
        (0.0, 4000.0),
    ]
    walls = [
        WallSegment(
            id=f"W{i + 1}",
            start=corners[i],
            end=corners[(i + 1) % len(corners)],
            kind="solid",
            evidence_coverage=0.9,
        )
        for i in range(len(corners))
    ]
    return RoomGeometry(
        polygon=Polygon(corners),
        walls=walls,
        openings=[],
        profile=HeightProfile(floor_mm=0.0, ceiling_mm=float(CEILING_MM), ceiling_observed=True),
    )


class TestShellAssembly:
    def test_a_plain_room_builds_floor_ceiling_and_four_walls(self) -> None:
        built = shell.build_shell(rectangular())
        assert built.wall_count == 4
        assert built.open_segment_count == 0
        assert built.triangle_count > 0

    def test_the_mesh_spans_the_room(self) -> None:
        built = shell.build_shell(rectangular(4000.0, 3000.0))
        lo, hi = built.mesh.bounds
        thickness = shell.WALL_THICKNESS_MM
        # Walls sit outside the floor plan, so the shell's outer bounds run a
        # thickness past it in each direction.
        assert lo[0] == pytest.approx(-thickness, abs=1.0)
        assert hi[0] == pytest.approx(4000.0 + thickness, abs=1.0)
        assert lo[1] == pytest.approx(0.0, abs=1.0)
        assert hi[1] == pytest.approx(CEILING_MM, abs=1.0)

    def test_the_room_measures_what_was_measured(self) -> None:
        """The clear span between opposite wall faces must equal the floor
        plan. Building the walls inwards instead leaves a 4 m room rendering
        as 3.8 m -- an error the size of the accuracy target in E2, added
        after all the measuring is finished, and invisible in any test that
        only checks the mesh exists.
        """
        room = rectangular(4000.0, 3000.0)
        floor = shell._horizontal_surface(room.polygon, 0.0, upward=True)

        # The floor is the measured plan...
        assert floor.bounds[1][0] - floor.bounds[0][0] == pytest.approx(4000.0, abs=1.0)
        # ...and no wall intrudes on it.
        for wall_id in range(4):
            wall = shell._wall_face(
                room.walls[wall_id],
                [],
                float(CEILING_MM),
                thickness_mm=shell.WALL_THICKNESS_MM,
            )
            assert wall is not None
            probe = np.array([[2000.0, 1200.0, 1500.0]])  # the middle of the room
            assert not wall.contains(probe)[0], f"wall {wall_id} intrudes into the room"

    def test_an_l_shaped_room_does_not_fill_its_notch(self) -> None:
        """An unconstrained (Delaunay) triangulation covers the convex hull,
        so a concave room comes back with triangles across the missing corner.
        A filled notch is a floor the camera can walk into and a patch the
        solver will happily place a sofa on, inside a wall."""
        room = l_shaped()
        floor = shell._horizontal_surface(room.polygon, 0.0, upward=True)
        area = sum(_triangle_area_xz(floor.vertices[face]) for face in floor.faces)
        assert area == pytest.approx(room.polygon.area, rel=0.01)

    def test_an_open_segment_gets_no_wall(self) -> None:
        """3.6: an open boundary bounds the room without being a surface.
        Building a wall there would show the user a wall that is not in
        their home."""
        built = shell.build_shell(rectangular(open_wall="W2"))
        assert built.wall_count == 3
        assert built.open_segment_count == 1

    def test_the_ceiling_faces_down_and_the_floor_faces_up(self) -> None:
        """Winding is not cosmetic here. A ceiling facing up is invisible from
        inside the room, and the viewer shows an open-topped box.

        Tested on the two surfaces directly rather than on the assembled
        shell: wall undersides also sit at y = 0 and legitimately face down,
        so filtering the whole mesh by height would compare floor triangles
        against wall triangles.
        """
        polygon = rectangular().polygon
        floor = shell._horizontal_surface(polygon, 0.0, upward=True)
        ceiling = shell._horizontal_surface(polygon, float(CEILING_MM), upward=False)

        assert len(floor.faces) and len(ceiling.faces)
        assert all(n[1] > 0.9 for n in floor.face_normals), "floor does not face up"
        assert all(n[1] < -0.9 for n in ceiling.face_normals), "ceiling does not face down"


class TestOpenings:
    def _door(self) -> Opening:
        return Opening(
            id="O1",
            wall_id="W1",
            type="door",
            offset_mm=1200,
            width_mm=900,
            sill_mm=0,
            height_mm=2030,
        )

    def _window(self) -> Opening:
        return Opening(
            id="O2",
            wall_id="W3",
            type="window",
            offset_mm=1000,
            width_mm=1200,
            sill_mm=900,
            height_mm=1200,
        )

    def test_a_door_removes_material_from_its_wall(self) -> None:
        without = shell.build_shell(rectangular())
        with_door = shell.build_shell(rectangular(openings=[self._door()]))
        assert with_door.mesh.volume < without.mesh.volume

    def test_the_hole_is_where_the_door_is(self) -> None:
        """A cutout in the wrong place is still a cutout, so a volume
        comparison proves nothing. This checks the doorway is actually empty
        and the wall beside it is not.

        Point-in-mesh runs on the single wall solid, which is watertight. The
        assembled shell is not -- floor, ceiling and walls are separate
        overlapping solids -- and `contains` on a non-watertight mesh answers
        by ray parity, which is arbitrary.
        """
        room = rectangular(openings=[self._door()])
        wall = shell._wall_face(
            room.walls[0],
            [self._door()],
            float(CEILING_MM),
            thickness_mm=shell.WALL_THICKNESS_MM,
        )
        assert wall is not None
        probes = np.array(
            [
                _wall_local(room.walls[0], u=1650.0, v=1000.0),  # in the doorway
                _wall_local(room.walls[0], u=400.0, v=1000.0),  # in the wall beside it
                _wall_local(room.walls[0], u=1650.0, v=2300.0),  # above the door head
            ]
        )
        contains = wall.contains(probes)
        assert not contains[0], "the doorway is still solid"
        assert contains[1], "the wall beside the doorway went missing"
        assert contains[2], "the wall above the door went missing"

    def test_a_window_keeps_wall_below_its_sill(self) -> None:
        """A window cut to the floor is a door. The sill is what the validator
        uses to decide whether a bookshelf blocks the view (5.5 S3)."""
        room = rectangular(openings=[self._window()])
        wall = shell._wall_face(
            room.walls[2],
            [self._window()],
            float(CEILING_MM),
            thickness_mm=shell.WALL_THICKNESS_MM,
        )
        assert wall is not None
        # The window spans v = 900..2100 at u = 1000..2200.
        below = _wall_local(room.walls[2], u=1600.0, v=400.0)
        inside = _wall_local(room.walls[2], u=1600.0, v=1500.0)
        assert wall.contains(np.array([below]))[0], "the wall under the window is missing"
        assert not wall.contains(np.array([inside]))[0], "the window is still solid"

    def test_a_window_gets_glass_and_a_door_does_not(self) -> None:
        with_window = shell.build_shell(rectangular(openings=[self._window()]))
        with_door = shell.build_shell(rectangular(openings=[self._door()]))
        # Glass adds geometry back; a bare door hole only removes it.
        assert with_window.triangle_count > with_door.triangle_count

    def test_several_openings_on_one_wall(self) -> None:
        pair = [
            self._door(),
            Opening("O3", "W1", "window", 2500, 1000, 900, 1100),
        ]
        built = shell.build_shell(rectangular(openings=pair))
        assert built.opening_count == 2
        assert built.wall_count == 4

    def test_an_opening_on_an_open_segment_is_simply_absent(self) -> None:
        """There is no wall to cut. It must not crash or leave a floating
        door frame in mid air."""
        built = shell.build_shell(rectangular(open_wall="W1", openings=[self._door()]))
        assert built.wall_count == 3


class TestExport:
    def test_the_glb_is_written_in_metres(self, tmp_path: object) -> None:
        """2.4: the pipeline is millimetres, glTF is metres. This is the one
        place the conversion happens, and a shell exported in millimetres
        would load as a 4 km room."""
        import trimesh

        built = shell.build_shell(rectangular(4000.0, 3000.0))
        destination = Path(str(tmp_path)) / "room_shell.glb"
        shell.export_glb(built, destination)

        loaded = trimesh.load(str(destination), force="mesh")
        lo, hi = loaded.bounds
        assert (hi[0] - lo[0]) == pytest.approx(4.0, abs=0.3)
        assert (hi[1] - lo[1]) == pytest.approx(2.5, abs=0.05)

    def test_export_does_not_mutate_the_mesh_it_was_given(self) -> None:
        built = shell.build_shell(rectangular())
        before = built.mesh.bounds.copy()
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            shell.export_glb(built, Path(directory) / "s.glb")
        assert np.allclose(built.mesh.bounds, before)

    def test_a_tier_zero_shell_is_far_inside_its_budget(self, tmp_path: object) -> None:
        """9.2 allows 3 MB. A flat-coloured shell should not come close, and
        the assertion is deliberately much tighter than the budget so a
        regression shows up here rather than on a phone."""
        built = shell.build_shell(
            rectangular(openings=[Opening("O1", "W1", "door", 1200, 900, 0, 2030)])
        )
        size = shell.export_glb(built, Path(str(tmp_path)) / "room_shell.glb")
        shell.check_budget(size)
        assert size < 256 * 1024, f"{size} bytes is larger than a flat shell should be"

    def test_the_budget_check_actually_fails(self) -> None:
        with pytest.raises(ValueError, match="budget"):
            shell.check_budget(shell.TIER0_GLB_BUDGET_BYTES + 1)


class TestAppearance:
    def test_only_solid_walls_get_an_appearance_entry(self) -> None:
        payload = shell.appearance(rectangular(open_wall="W2"))
        assert set(payload["walls"]) == {"W1", "W3", "W4"}

    def test_colours_are_hex_strings(self) -> None:
        payload = shell.appearance(rectangular())
        assert payload["floor"]["color"].startswith("#")
        assert len(payload["floor"]["color"]) == 7

    def test_tier_zero_is_declared(self) -> None:
        assert shell.appearance(rectangular())["tier"] == 0


def _wall_local(wall: WallSegment, *, u: float, v: float) -> list[float]:
    """A world point `u` along a wall, `v` up it, halfway through its thickness.

    Worth a helper rather than hand-computed coordinates: a wall's local frame
    depends on which way round its segment runs, and getting that wrong gives
    a probe outside the mesh and a test that passes for the wrong reason.
    """
    dx, dz = wall.direction()
    half = shell.WALL_THICKNESS_MM / 2.0
    # Walls are built on the far side of the polygon edge, so the room's
    # inner surface is exactly the measured floor plan (see `_wall_face`).
    # A probe therefore sits *outside* the polygon by half a thickness.
    return [
        wall.start[0] + dx * u + dz * half,
        v,
        wall.start[1] + dz * u - dx * half,
    ]


def _triangle_area_xz(corners: np.ndarray) -> float:
    """Area of a triangle projected onto the floor plane.

    The 2D cross product is written out rather than called: numpy 2 removed
    `np.cross` for 2-vectors.
    """
    (ax, az), (bx, bz), (cx, cz) = corners[:, [0, 2]]
    return float(abs((bx - ax) * (cz - az) - (bz - az) * (cx - ax)) / 2.0)
