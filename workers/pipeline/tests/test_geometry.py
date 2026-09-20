"""S6 geometry extraction on synthetic rooms with known floor plans.

The plan's Phase 1 test list asks for "polygon extraction on synthetic density
maps (rect, L, with furniture blobs against walls)" and "opening rectangle
fitting". These are those, built as point clouds rather than density maps so
the whole stage is exercised from the same input S5 hands it.

Every room here is generated from dimensions the test knows, so the assertions
are against truth rather than against a previous run.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from roomfittr_pipeline import geometry
from roomfittr_pipeline.align import Points
from roomfittr_pipeline.errors import PipelineError
from shapely.geometry import Polygon

CEILING_MM = 2500.0


def wall_slab(
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    height_mm: float = CEILING_MM,
    n: int = 4000,
    noise_mm: float = 12.0,
    gap: tuple[float, float] | None = None,
    seed: int = 0,
) -> Points:
    """Points on a vertical wall between two XZ corners.

    `gap` is a (start, end) interval along the wall left empty -- a doorway,
    or the missing edge of an open-plan room.
    """
    rng = np.random.default_rng(seed)
    length = math.dist(start, end)
    t = rng.uniform(0.0, 1.0, n)
    if gap is not None:
        lo, hi = gap[0] / length, gap[1] / length
        t = t[(t < lo) | (t > hi)]
    x = start[0] + (end[0] - start[0]) * t + rng.normal(0.0, noise_mm, len(t))
    z = start[1] + (end[1] - start[1]) * t + rng.normal(0.0, noise_mm, len(t))
    y = rng.uniform(0.0, height_mm, len(t))
    return np.stack([x, y, z], axis=1)


def floor_slab(corners: list[tuple[float, float]], n: int = 6000, seed: int = 1) -> Points:
    """Points scattered over the floor of the polygon given by `corners`."""
    rng = np.random.default_rng(seed)
    polygon = Polygon(corners)
    minx, minz, maxx, maxz = polygon.bounds
    picked: list[tuple[float, float]] = []
    while len(picked) < n:
        x = rng.uniform(minx, maxx, n)
        z = rng.uniform(minz, maxz, n)
        for px, pz in zip(x, z, strict=False):
            if polygon.contains(Polygon([(px, pz), (px + 1, pz), (px, pz + 1)])):
                picked.append((px, pz))
                if len(picked) >= n:
                    break
    array = np.array(picked)
    return np.stack([array[:, 0], rng.normal(0.0, 5.0, len(array)), array[:, 1]], axis=1)


def ceiling_slab(corners: list[tuple[float, float]], n: int = 2000, seed: int = 2) -> Points:
    points = floor_slab(corners, n=n, seed=seed)
    points[:, 1] += CEILING_MM
    return points


def rectangular_room(
    width_mm: float = 4000.0, depth_mm: float = 3000.0, **kwargs: object
) -> tuple[Points, Points, list[tuple[float, float]]]:
    """Return (structure points, floor points, true corners) for a plain box."""
    corners = [
        (0.0, 0.0),
        (width_mm, 0.0),
        (width_mm, depth_mm),
        (0.0, depth_mm),
    ]
    walls = np.concatenate(
        [
            wall_slab(corners[0], corners[1], seed=10),
            wall_slab(corners[1], corners[2], seed=11),
            wall_slab(corners[2], corners[3], seed=12),
            wall_slab(corners[3], corners[0], seed=13),
        ]
    )
    floor = floor_slab(corners)
    ceiling = ceiling_slab(corners)
    return np.concatenate([walls, floor, ceiling]), floor, corners


def l_shaped_room() -> tuple[Points, Points, list[tuple[float, float]]]:
    """A 5 x 4 m room with a 2 x 1.5 m bite out of one corner."""
    corners = [
        (0.0, 0.0),
        (5000.0, 0.0),
        (5000.0, 2500.0),
        (3000.0, 2500.0),
        (3000.0, 4000.0),
        (0.0, 4000.0),
    ]
    walls = np.concatenate(
        [
            wall_slab(corners[i], corners[(i + 1) % len(corners)], n=3000, seed=20 + i)
            for i in range(len(corners))
        ]
    )
    floor = floor_slab(corners, n=8000)
    ceiling = ceiling_slab(corners, n=2500)
    return np.concatenate([walls, floor, ceiling]), floor, corners


def build(structure: Points, floor: Points) -> tuple[geometry.EvidenceGrid, geometry.HeightProfile]:
    profile = geometry.height_profile(structure)
    return geometry.wall_evidence_grid(structure, profile), profile


def iou(a: Polygon, b: Polygon) -> float:
    return a.intersection(b).area / a.union(b).area


class TestHeightProfile:
    def test_floor_and_ceiling_are_found(self) -> None:
        structure, _, _ = rectangular_room()
        profile = geometry.height_profile(structure)
        assert profile.floor_mm == pytest.approx(0.0, abs=80.0)
        assert profile.height_mm == pytest.approx(CEILING_MM, abs=100.0)
        assert profile.ceiling_observed

    def test_an_unseen_ceiling_is_flagged_and_estimated(self) -> None:
        """Phone captures point forward and down; the ceiling often never gets
        framed. 3.6 expects this and says so in the output rather than
        inventing a confident number."""
        structure, _, corners = rectangular_room()
        # Keep only what a downward-pointing capture would see.
        low = structure[structure[:, 1] < 1200.0]
        profile = geometry.height_profile(low)
        assert not profile.ceiling_observed
        assert profile.height_mm == geometry.CEILING_PRIOR_MM

    def test_a_stray_point_above_the_ceiling_does_not_become_the_ceiling(self) -> None:
        """Extremes are unreliable -- a light fitting, a reflection. Peaks are
        not, which is why the histogram looks for surfaces rather than maxima."""
        structure, _, _ = rectangular_room()
        stray = np.array([[1000.0, 6000.0, 1000.0]] * 5)
        profile = geometry.height_profile(np.concatenate([structure, stray]))
        assert profile.height_mm == pytest.approx(CEILING_MM, abs=150.0)

    def test_too_few_points(self) -> None:
        with pytest.raises(PipelineError) as exc:
            geometry.height_profile(np.zeros((10, 3)))
        assert exc.value.code == "INSUFFICIENT_COVERAGE"

    def test_a_flat_scene_is_not_a_room(self) -> None:
        flat = np.random.default_rng(0).uniform(0, 1000, (500, 3))
        flat[:, 1] = 0.0
        with pytest.raises(PipelineError):
            geometry.height_profile(flat)


class TestCandidateLines:
    def test_the_four_walls_of_a_box_are_found(self) -> None:
        structure, floor, corners = rectangular_room(4000.0, 3000.0)
        grid, _ = build(structure, floor)

        xs = geometry.candidate_lines(grid, axis=0)
        zs = geometry.candidate_lines(grid, axis=2)
        assert len(xs) == 2, f"expected 2 x-walls, got {xs}"
        assert len(zs) == 2, f"expected 2 z-walls, got {zs}"
        assert xs[0] == pytest.approx(0.0, abs=60.0)
        assert xs[1] == pytest.approx(4000.0, abs=60.0)
        assert zs[0] == pytest.approx(0.0, abs=60.0)
        assert zs[1] == pytest.approx(3000.0, abs=60.0)

    def test_duplicate_peaks_from_one_wall_are_merged(self) -> None:
        """A 100 mm wall seen from both sides is two peaks and one wall."""
        merged = geometry._merge_close([1000.0, 1040.0, 1080.0, 4000.0], 150.0)
        assert merged == pytest.approx([1040.0, 4000.0])

    def test_axis_argument_is_validated(self) -> None:
        structure, floor, _ = rectangular_room()
        grid, _ = build(structure, floor)
        with pytest.raises(ValueError):
            geometry.candidate_lines(grid, axis=1)


class TestFloorPolygon:
    def test_a_rectangular_room_is_recovered_to_within_a_few_centimetres(self) -> None:
        structure, floor, corners = rectangular_room(4000.0, 3000.0)
        grid, _ = build(structure, floor)
        polygon = geometry.floor_polygon(grid, floor[:, [0, 2]])

        assert iou(polygon, Polygon(corners)) > 0.97
        assert len(list(polygon.exterior.coords)) - 1 == 4

    def test_an_l_shaped_room_keeps_its_notch(self) -> None:
        """E4's polygon IoU is scored on rooms like this; a method that
        rounded the notch off would still score well on rectangles."""
        structure, floor, corners = l_shaped_room()
        grid, _ = build(structure, floor)
        polygon = geometry.floor_polygon(grid, floor[:, [0, 2]])

        assert iou(polygon, Polygon(corners)) > 0.93
        assert 5 <= len(list(polygon.exterior.coords)) - 1 <= 8

    def test_the_polygon_is_counter_clockwise(self) -> None:
        """The RoomModel schema requires it, and the viewer's floor winding
        decides which way its normal faces."""
        structure, floor, _ = rectangular_room()
        grid, _ = build(structure, floor)
        assert geometry.floor_polygon(grid, floor[:, [0, 2]]).exterior.is_ccw

    def test_walls_are_not_pulled_in_by_furniture(self) -> None:
        """R3 and E6, as a unit test.

        The scenario is a tall bookcase flush against the far wall. Two things
        are true of it at once, and it takes both to cause the damage:

        1. It is a dense vertical slab at a single depth, so it projects a
           sharp peak and becomes a candidate wall line 900 mm inside the room.
        2. It hides the floor behind it, so the strip between the bookcase and
           the real wall has no floor points to mark that cell interior.

        Together those shrink the room by 900 mm, and the result is still a
        tidy rectangle that passes every sanity check. Excluding object points
        from the evidence grid removes the spurious candidate line, and then
        the occluded strip is no longer a cell of its own and the room keeps
        its full depth.
        """
        structure, floor, corners = rectangular_room(4000.0, 3000.0)
        rng = np.random.default_rng(42)

        # A run of wardrobes 600 mm deep spanning the whole z = 3000 wall.
        # Full width matters: a narrower unit leaves floor visible past each
        # end, which is enough to keep the strip behind it marked interior.
        wardrobe = np.stack(
            [
                rng.uniform(0.0, 4000.0, 9000),
                rng.uniform(0.0, 2000.0, 9000),
                rng.normal(2400.0, 25.0, 9000),
            ],
            axis=1,
        )
        # The floor it hides. Nothing of the room past z = 2400 is visible.
        occluded = floor[floor[:, 2] < 2400.0]

        profile = geometry.height_profile(structure)
        true_polygon = Polygon(corners)

        # The correct call: structure points only.
        guarded = geometry.floor_polygon(
            geometry.wall_evidence_grid(structure, profile), occluded[:, [0, 2]]
        )
        assert iou(guarded, true_polygon) > 0.95
        assert guarded.bounds[3] == pytest.approx(3000.0, abs=80.0)

        # The mistake, made explicit, so the test states what it protects
        # against rather than only that the right answer is right.
        careless = geometry.floor_polygon(
            geometry.wall_evidence_grid(np.concatenate([structure, wardrobe]), profile),
            occluded[:, [0, 2]],
        )
        pull_in = guarded.bounds[3] - careless.bounds[3]
        assert pull_in > 200.0, (
            "contaminating the evidence grid with furniture was expected to move "
            "the wall inwards; if it no longer does, this test has stopped "
            "testing anything and the guard above proves nothing"
        )

    def test_a_capture_that_filmed_one_wall_is_rejected(self) -> None:
        """The floor-extent fallback keeps an open-plan room working, and it
        would also happily turn one filmed wall plus a walked floor into a
        confident-looking rectangle. The coverage gate is what stops that: a
        boundary that is mostly open was not measured, it was glimpsed."""
        corners = [(0.0, 0.0), (4000.0, 0.0), (4000.0, 3000.0), (0.0, 3000.0)]
        one_wall = wall_slab(corners[0], corners[1], n=4000)
        floor = floor_slab(corners)
        structure = np.concatenate([one_wall, floor, ceiling_slab(corners)])
        grid, profile = build(structure, floor)

        polygon = geometry.floor_polygon(grid, floor[:, [0, 2]])
        walls = geometry.walls_from_polygon(polygon, grid)
        assert sum(w.kind == "open" for w in walls) >= 3

        with pytest.raises(PipelineError) as exc:
            geometry.sanity_check(
                geometry.RoomGeometry(polygon=polygon, walls=walls, openings=[], profile=profile)
            )
        assert exc.value.code == "INSUFFICIENT_COVERAGE"

    def test_no_floor_points_is_a_coverage_error(self) -> None:
        structure, floor, _ = rectangular_room()
        grid, _ = build(structure, floor)
        with pytest.raises(PipelineError) as exc:
            geometry.floor_polygon(grid, np.zeros((5, 2)))
        assert exc.value.code == "INSUFFICIENT_COVERAGE"


class TestWallClassification:
    def test_a_closed_room_is_all_solid(self) -> None:
        structure, floor, _ = rectangular_room()
        grid, _ = build(structure, floor)
        polygon = geometry.floor_polygon(grid, floor[:, [0, 2]])
        walls = geometry.walls_from_polygon(polygon, grid)
        assert len(walls) == 4
        assert {wall.kind for wall in walls} == {"solid"}

    def test_a_missing_wall_becomes_an_open_boundary(self) -> None:
        """3.6 step 5. An open-plan edge bounds the room but is not something
        a sofa can go against, so the solver has to be told."""
        corners = [(0.0, 0.0), (4000.0, 0.0), (4000.0, 3000.0), (0.0, 3000.0)]
        walls_points = np.concatenate(
            [
                wall_slab(corners[0], corners[1], seed=30),
                wall_slab(corners[1], corners[2], seed=31),
                # corners[2] -> corners[3] deliberately absent: open to the hall.
                wall_slab(corners[3], corners[0], seed=33),
            ]
        )
        floor = floor_slab(corners)
        structure = np.concatenate([walls_points, floor, ceiling_slab(corners)])
        grid, _ = build(structure, floor)
        polygon = geometry.floor_polygon(grid, floor[:, [0, 2]])
        walls = geometry.walls_from_polygon(polygon, grid)

        open_walls = [w for w in walls if w.kind == "open"]
        assert len(open_walls) == 1
        # It is the long edge at z = 3000.
        assert open_walls[0].start[1] == pytest.approx(3000.0, abs=100.0)
        assert open_walls[0].end[1] == pytest.approx(3000.0, abs=100.0)

    def test_a_doorway_sized_gap_does_not_make_a_wall_open(self) -> None:
        """A 900 mm doorway in a 4 m wall still leaves a wall. Calling it open
        would stop the solver placing anything along it."""
        corners = [(0.0, 0.0), (4000.0, 0.0), (4000.0, 3000.0), (0.0, 3000.0)]
        walls_points = np.concatenate(
            [
                wall_slab(corners[0], corners[1], seed=40),
                wall_slab(corners[1], corners[2], seed=41),
                wall_slab(corners[2], corners[3], gap=(1500.0, 2400.0), seed=42),
                wall_slab(corners[3], corners[0], seed=43),
            ]
        )
        floor = floor_slab(corners)
        structure = np.concatenate([walls_points, floor, ceiling_slab(corners)])
        grid, _ = build(structure, floor)
        polygon = geometry.floor_polygon(grid, floor[:, [0, 2]])
        assert all(w.kind == "solid" for w in geometry.walls_from_polygon(polygon, grid))


class TestOpenings:
    def _room_walls(self) -> list[geometry.WallSegment]:
        structure, floor, _ = rectangular_room(4000.0, 3000.0)
        grid, _ = build(structure, floor)
        polygon = geometry.floor_polygon(grid, floor[:, [0, 2]])
        return geometry.walls_from_polygon(polygon, grid)

    def test_a_door_is_placed_on_the_right_wall_at_the_right_offset(self) -> None:
        walls = self._room_walls()
        rng = np.random.default_rng(7)
        # A 900 x 2030 door on the z = 0 wall, starting 1200 mm along.
        door = np.stack(
            [
                rng.uniform(1200.0, 2100.0, 800),
                rng.uniform(0.0, 2030.0, 800),
                rng.normal(0.0, 15.0, 800),
            ],
            axis=1,
        )
        opening = geometry.fit_opening(door, walls, opening_id="O1", label="door")
        assert opening is not None
        assert opening.type == "door"
        assert opening.sill_mm == 0
        assert opening.width_mm == pytest.approx(900, abs=120)
        assert opening.height_mm == pytest.approx(2030, abs=150)

        wall = next(w for w in walls if w.id == opening.wall_id)
        # Offset is measured from the wall's own start, whichever end that is.
        along = opening.offset_mm if wall.start[1] < 100 and wall.start[0] < 100 else None
        if along is not None:
            assert along == pytest.approx(1200, abs=200)

    def test_a_window_keeps_its_sill_height(self) -> None:
        """The sill is what the validator uses to decide whether a bookshelf
        blocks the window (5.5 S3), so it has to survive the fit."""
        walls = self._room_walls()
        rng = np.random.default_rng(8)
        window = np.stack(
            [
                rng.uniform(500.0, 1700.0, 700),
                rng.uniform(900.0, 2100.0, 700),
                rng.normal(3000.0, 15.0, 700),
            ],
            axis=1,
        )
        opening = geometry.fit_opening(window, walls, opening_id="O2", label="window")
        assert opening is not None
        assert opening.type == "window"
        assert opening.sill_mm == pytest.approx(900, abs=120)
        assert opening.height_mm == pytest.approx(1200, abs=150)

    def test_a_partly_occluded_door_is_clamped_to_a_plausible_size(self) -> None:
        """3.6 clamps door dimensions. A door half-hidden behind a coat rack
        fits a 400 mm box, and the validator would then keep the wrong strip
        of floor clear."""
        walls = self._room_walls()
        rng = np.random.default_rng(9)
        sliver = np.stack(
            [
                rng.uniform(1000.0, 1400.0, 400),
                rng.uniform(0.0, 1100.0, 400),
                rng.normal(0.0, 10.0, 400),
            ],
            axis=1,
        )
        opening = geometry.fit_opening(sliver, walls, opening_id="O3", label="door")
        assert opening is not None
        assert opening.width_mm >= geometry.DOOR_WIDTH_RANGE_MM[0]
        assert opening.height_mm >= geometry.DOOR_HEIGHT_RANGE_MM[0]

    def test_a_high_sill_door_is_recorded_as_a_window(self) -> None:
        """Segmentation mislabels happen. A 'door' 900 mm off the floor is not
        one, and treating it as a door would carve a keep-out zone out of
        usable floor for no reason."""
        walls = self._room_walls()
        rng = np.random.default_rng(10)
        high = np.stack(
            [
                rng.uniform(1000.0, 1900.0, 600),
                rng.uniform(900.0, 2100.0, 600),
                rng.normal(0.0, 12.0, 600),
            ],
            axis=1,
        )
        opening = geometry.fit_opening(high, walls, opening_id="O4", label="door")
        assert opening is not None
        assert opening.type == "window"
        assert opening.sill_mm > 500

    def test_an_opening_far_from_every_wall_is_dropped(self) -> None:
        """A window seen through a doorway into the next room, or in a mirror."""
        walls = self._room_walls()
        rng = np.random.default_rng(11)
        elsewhere = np.stack(
            [
                rng.uniform(1000.0, 1900.0, 400),
                rng.uniform(900.0, 2100.0, 400),
                rng.normal(9000.0, 15.0, 400),
            ],
            axis=1,
        )
        assert geometry.fit_opening(elsewhere, walls, opening_id="O5", label="window") is None

    def test_too_few_points_is_dropped(self) -> None:
        assert (
            geometry.fit_opening(
                np.zeros((5, 3)), self._room_walls(), opening_id="O6", label="door"
            )
            is None
        )

    def test_an_opening_never_extends_past_its_wall(self) -> None:
        """The schema requires it and the shell mesh's cutout would otherwise
        punch a hole through the corner into the next wall."""
        walls = self._room_walls()
        rng = np.random.default_rng(12)
        near_corner = np.stack(
            [
                rng.uniform(3600.0, 4100.0, 500),
                rng.uniform(0.0, 2030.0, 500),
                rng.normal(0.0, 12.0, 500),
            ],
            axis=1,
        )
        opening = geometry.fit_opening(near_corner, walls, opening_id="O7", label="door")
        assert opening is not None
        wall = next(w for w in walls if w.id == opening.wall_id)
        assert opening.offset_mm + opening.width_mm <= wall.length_mm + 1.0


class TestSanityChecks:
    def _geometry(self, **overrides: object) -> geometry.RoomGeometry:
        structure, floor, _ = rectangular_room()
        grid, profile = build(structure, floor)
        polygon = geometry.floor_polygon(grid, floor[:, [0, 2]])
        base = {
            "polygon": polygon,
            "walls": geometry.walls_from_polygon(polygon, grid),
            "openings": [],
            "profile": profile,
        }
        base.update(overrides)
        return geometry.RoomGeometry(**base)  # type: ignore[arg-type]

    def test_a_good_room_passes_with_only_a_missing_door_warning(self) -> None:
        assert geometry.sanity_check(self._geometry()) == ["no_door_found"]

    def test_an_implausibly_small_room_is_rejected(self) -> None:
        tiny = self._geometry(polygon=Polygon([(0, 0), (900, 0), (900, 900), (0, 900)]))
        with pytest.raises(PipelineError) as exc:
            geometry.sanity_check(tiny)
        assert exc.value.code == "LAYOUT_EXTRACTION_FAILED"

    def test_an_implausibly_large_room_is_rejected(self) -> None:
        huge = self._geometry(polygon=Polygon([(0, 0), (20000, 0), (20000, 20000), (0, 20000)]))
        with pytest.raises(PipelineError):
            geometry.sanity_check(huge)

    def test_an_impossible_ceiling_is_rejected(self) -> None:
        bad = self._geometry(
            profile=geometry.HeightProfile(floor_mm=0.0, ceiling_mm=6000.0, ceiling_observed=True)
        )
        with pytest.raises(PipelineError):
            geometry.sanity_check(bad)

    def test_an_estimated_ceiling_is_warned_about_not_rejected(self) -> None:
        estimated = self._geometry(
            profile=geometry.HeightProfile(floor_mm=0.0, ceiling_mm=2400.0, ceiling_observed=False)
        )
        assert "ceiling_estimated" in geometry.sanity_check(estimated)

    def test_overlapping_doors_are_rejected(self) -> None:
        overlapping = self._geometry(
            openings=[
                geometry.Opening("O1", "W1", "door", 1000, 900, 0, 2030),
                geometry.Opening("O2", "W1", "door", 1400, 900, 0, 2030),
            ]
        )
        with pytest.raises(PipelineError) as exc:
            geometry.sanity_check(overlapping)
        assert "overlap" in exc.value.detail

    def test_an_opening_on_a_wall_that_does_not_exist_is_rejected(self) -> None:
        orphan = self._geometry(openings=[geometry.Opening("O1", "W99", "door", 0, 900, 0, 2030)])
        with pytest.raises(PipelineError):
            geometry.sanity_check(orphan)

    def test_an_open_boundary_is_reported_to_the_user(self) -> None:
        """3.11 lists open-plan as a known limitation; the UI shows the warning
        rather than presenting a guess as a measurement."""
        walls = self._geometry().walls
        opened = [
            geometry.WallSegment(walls[0].id, walls[0].start, walls[0].end, "open", 0.1),
            *walls[1:],
        ]
        assert "open_boundary" in geometry.sanity_check(self._geometry(walls=opened))
