"""The geometric primitives, and the parity contract underneath them (5.5).

Two things are being protected here.

The first is the rotation convention. `rotation_deg` is right-handed about
+Y, matching 2.4, three.js and the pipeline's `align.yaw_rotation`, so a
positive rotation carries +X towards -Z. The other handedness -- the one the
familiar 2D rotation matrix gives -- produces layouts that pass every rule
with the furniture turned 90 degrees from where it belongs.

The second is that every predicate the TypeScript validator has to reproduce
is exact and dependency-free. Where the Python side keeps a vectorised twin
for speed, the two must agree: the scalar version is the definition the
browser mirrors, and the fast one is what makes the circulation grid
affordable.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from layout_helpers import load_room
from roomfittr_layout.geometry import (
    TOLERANCE_MM,
    Footprint,
    corners_of,
    distance_to_boundary,
    distance_to_footprint,
    gap_between,
    protrusion,
    rectangle_from_wall,
    rotate_xz,
    separating_axis_overlap,
)
from roomfittr_layout.room import (
    _distance_to_boundary_grid,
    _distance_to_footprint_grid,
    analyse,
)


def box(
    x: float, z: float, w: float, d: float, h: float = 800.0, rotation: float = 0.0
) -> Footprint:
    return Footprint(x, z, w, d, h, rotation_deg=rotation)


class TestRotationConvention:
    def test_positive_rotation_carries_x_towards_negative_z(self) -> None:
        x, z = rotate_xz(1.0, 0.0, 90.0)
        assert x == pytest.approx(0.0, abs=1e-9)
        assert z == pytest.approx(-1.0, abs=1e-9)

    def test_an_unrotated_front_points_along_negative_z(self) -> None:
        """2.4: an item's local front is -Z before rotation."""
        facing = box(0, 0, 1000, 500).front_normal()
        assert facing == pytest.approx((0.0, -1.0), abs=1e-9)

    @pytest.mark.parametrize("degrees", [0.0, 37.0, 90.0, 180.0, 271.0])
    def test_rotation_preserves_size(self, degrees: float) -> None:
        """A reflection would also 'rotate' correctly and would mirror every
        footprint; checking the dimensions survive rules it out."""
        ring = corners_of(box(100, -200, 1800, 700, rotation=degrees))
        side_a = math.dist(tuple(ring[0]), tuple(ring[1]))
        side_b = math.dist(tuple(ring[1]), tuple(ring[2]))
        assert sorted([side_a, side_b]) == pytest.approx([700.0, 1800.0], abs=1e-6)


class TestSeparatingAxis:
    def test_identical_rectangles_overlap_by_their_smaller_side(self) -> None:
        a = box(0, 0, 1000, 600)
        assert separating_axis_overlap(a, a) == pytest.approx(600.0, abs=1e-6)

    def test_separated_rectangles_report_zero(self) -> None:
        assert separating_axis_overlap(box(0, 0, 1000, 600), box(5000, 0, 1000, 600)) == 0.0

    def test_touching_rectangles_report_zero(self) -> None:
        """Flush is not overlapping. The solver places items flush against
        walls and each other, so a touch counted as a collision would reject
        the solver's own output."""
        assert separating_axis_overlap(box(0, 0, 1000, 600), box(1000, 0, 1000, 600)) == 0.0

    def test_the_depth_is_reported_not_just_a_boolean(self) -> None:
        """5.5 requires a measured value in every violation message."""
        overlap = separating_axis_overlap(box(0, 0, 1000, 600), box(960, 0, 1000, 600))
        assert overlap == pytest.approx(40.0, abs=1e-6)

    def test_a_rotated_pair_is_handled(self) -> None:
        """Axis-aligned bounding boxes would call these overlapping; they are
        not, and a solver that believed otherwise would refuse a legal
        corner placement."""
        a = box(0, 0, 2000, 300, rotation=45.0)
        b = box(1400, 1400, 2000, 300, rotation=45.0)
        assert separating_axis_overlap(a, b) == 0.0


class TestGapAndProtrusion:
    def test_the_gap_between_two_rectangles(self) -> None:
        assert gap_between(box(0, 0, 1000, 600), box(2000, 0, 1000, 600)) == pytest.approx(
            1000.0, abs=1e-6
        )

    def test_overlapping_rectangles_have_no_gap(self) -> None:
        assert gap_between(box(0, 0, 1000, 600), box(500, 0, 1000, 600)) == 0.0

    def test_an_item_inside_the_room_does_not_protrude(self) -> None:
        room = analyse(load_room("rectangular-living")).floor
        assert protrusion(box(0, 0, 1000, 600), room) == 0.0

    def test_protrusion_measures_the_worst_corner(self) -> None:
        room = analyse(load_room("rectangular-living")).floor
        # The room spans x in [-2300, 2300]; push 100 mm past the edge.
        assert protrusion(box(1900, 0, 1000, 600), room) == pytest.approx(100.0, abs=1.0)

    def test_a_flush_item_is_within_tolerance(self) -> None:
        """5.5's 5 mm tolerance exists because the solver places items flush
        and the trigonometry lands them fractions of a millimetre out."""
        room = analyse(load_room("rectangular-living")).floor
        assert protrusion(box(1800, 0, 1000, 600), room) <= TOLERANCE_MM


class TestWallRectangles:
    def test_a_keepout_extends_into_the_room(self) -> None:
        """5.2's keep-outs go inwards. The left normal of a counter-clockwise
        floor polygon points into the room; the right normal would put every
        door's keep-out outside the building."""
        analysis = analyse(load_room("rectangular-living"))
        wall = analysis.wall("W1")
        assert wall is not None
        rect = rectangle_from_wall(wall.start, wall.end, 1000.0, 900.0, 900.0)
        assert analysis.floor.contains(rect.polygon().centroid)

    def test_the_rectangle_has_the_requested_size(self) -> None:
        analysis = analyse(load_room("rectangular-living"))
        wall = analysis.wall("W1")
        assert wall is not None
        rect = rectangle_from_wall(wall.start, wall.end, 1000.0, 900.0, 600.0)
        assert rect.width_mm == pytest.approx(900.0)
        assert rect.depth_mm == pytest.approx(600.0)

    def test_a_degenerate_wall_yields_an_empty_rectangle(self) -> None:
        rect = rectangle_from_wall((0.0, 0.0), (0.0, 0.0), 0.0, 900.0, 900.0)
        assert rect.width_mm == 0.0


class TestDistancePredicates:
    """The functions the TypeScript validator mirrors line for line."""

    def test_a_point_inside_a_rectangle_is_zero_away(self) -> None:
        assert distance_to_footprint((0.0, 0.0), box(0, 0, 1000, 600)) == 0.0

    def test_a_point_beside_a_rectangle(self) -> None:
        assert distance_to_footprint((1000.0, 0.0), box(0, 0, 1000, 600)) == pytest.approx(
            500.0, abs=1e-9
        )

    def test_a_point_diagonal_from_a_corner(self) -> None:
        """The clamped form has to handle the corner case as a real 2D
        distance, not as the larger of the two axis distances."""
        measured = distance_to_footprint((800.0, 600.0), box(0, 0, 1000, 600))
        assert measured == pytest.approx(math.hypot(300.0, 300.0), abs=1e-9)

    def test_rotation_is_accounted_for(self) -> None:
        upright = distance_to_footprint((0.0, 1000.0), box(0, 0, 2000, 400))
        turned = distance_to_footprint((0.0, 1000.0), box(0, 0, 2000, 400, rotation=90.0))
        assert upright == pytest.approx(800.0, abs=1e-9)
        assert turned == pytest.approx(0.0, abs=1e-9)

    def test_distance_to_a_room_boundary(self) -> None:
        room = analyse(load_room("rectangular-living")).floor
        # The room spans z in [-1900, 1900]; the centre is 1900 from the edge.
        assert distance_to_boundary((0.0, 0.0), room) == pytest.approx(1900.0, abs=1.0)

    def test_a_point_on_the_boundary_is_zero_away(self) -> None:
        room = analyse(load_room("rectangular-living")).floor
        assert distance_to_boundary((-2300.0, 0.0), room) == pytest.approx(0.0, abs=1e-6)


class TestVectorisedTwinsAgree:
    """The fast paths must equal the definitions, or the browser and the
    server disagree about which cells a person can stand in.

    Not a micro-test: the circulation grid is the input to H6, the only hard
    rule whose answer is a flood fill rather than a comparison.
    """

    @pytest.mark.parametrize("room_name", ["rectangular-living", "l-shaped-living", "narrow-room"])
    def test_boundary_distance(self, room_name: str) -> None:
        analysis = analyse(load_room(room_name))
        grid_x, grid_z = analysis.base_grid.grid_x, analysis.base_grid.grid_z
        fast = _distance_to_boundary_grid(grid_x, grid_z, analysis.floor)
        slow = np.array(
            [
                [
                    distance_to_boundary((float(x), float(z)), analysis.floor)
                    for x, z in zip(row_x, row_z, strict=False)
                ]
                for row_x, row_z in zip(grid_x, grid_z, strict=False)
            ]
        )
        assert np.abs(fast - slow).max() < 1e-9

    @pytest.mark.parametrize("rotation", [0.0, 37.0, 90.0])
    def test_footprint_distance(self, rotation: float) -> None:
        analysis = analyse(load_room("rectangular-living"))
        grid_x, grid_z = analysis.base_grid.grid_x, analysis.base_grid.grid_z
        item = box(300, -200, 1800, 700, rotation=rotation)
        fast = _distance_to_footprint_grid(grid_x, grid_z, item)
        slow = np.array(
            [
                [
                    distance_to_footprint((float(x), float(z)), item)
                    for x, z in zip(row_x, row_z, strict=False)
                ]
                for row_x, row_z in zip(grid_x, grid_z, strict=False)
            ]
        )
        assert np.abs(fast - slow).max() < 1e-6


class TestFixtureRoomsAreWellFormed:
    """Phase 0 hand-authored these from real measurements; the layout engine
    assumes properties of them that nothing else checks."""

    ROOMS = [
        "rectangular-living",
        "small-bedroom",
        "l-shaped-living",
        "open-plan-boundary",
        "many-openings",
        "narrow-room",
    ]

    @pytest.mark.parametrize("room_name", ROOMS)
    def test_the_floor_polygon_is_counter_clockwise(self, room_name: str) -> None:
        """Every inward normal in the engine is a left normal, which is only
        inward for a counter-clockwise ring. A clockwise fixture would put
        every door keep-out outside the building."""
        assert analyse(load_room(room_name)).floor.exterior.is_ccw

    @pytest.mark.parametrize("room_name", ROOMS)
    def test_every_door_keepout_lands_inside_the_room(self, room_name: str) -> None:
        analysis = analyse(load_room(room_name))
        for opening_id, keepout in analysis.door_keepouts.items():
            overlap = analysis.floor.intersection(keepout.polygon()).area
            assert overlap > 0, f"{room_name}/{opening_id} keep-out is outside the room"

    @pytest.mark.parametrize("room_name", ROOMS)
    def test_every_window_zone_lands_inside_the_room(self, room_name: str) -> None:
        analysis = analyse(load_room(room_name))
        for opening_id, zone in analysis.window_zones.items():
            overlap = analysis.floor.intersection(zone.polygon()).area
            assert overlap > 0, f"{room_name}/{opening_id} window zone is outside the room"
