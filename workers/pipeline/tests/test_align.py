"""S5 alignment on synthetic point clouds with known answers.

The plan's Phase 1 test list asks for exactly this: "alignment on synthetic
point clouds (known rotation recovered within 1 deg)". Synthetic is the point
-- these tests know the true answer, which no real capture does.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from numpy.typing import NDArray
from roomfittr_pipeline import align
from roomfittr_pipeline.align import Points
from roomfittr_pipeline.errors import PipelineError


def make_floor(
    width_mm: float = 4000.0,
    depth_mm: float = 3000.0,
    n: int = 2000,
    noise_mm: float = 5.0,
    seed: int = 0,
) -> Points:
    """A flat floor at y = 0, with a little measurement noise."""
    rng = np.random.default_rng(seed)
    x = rng.uniform(-width_mm / 2, width_mm / 2, n)
    z = rng.uniform(-depth_mm / 2, depth_mm / 2, n)
    y = rng.normal(0.0, noise_mm, n)
    return np.stack([x, y, z], axis=1)


def make_wall_normals(
    yaw_deg: float, n_per_wall: int = 60, noise_deg: float = 2.0, seed: int = 1
) -> Points:
    """Normals of the four walls of a rectangular room rotated by `yaw_deg`."""
    rng = np.random.default_rng(seed)
    normals = []
    for base in (0.0, 90.0, 180.0, 270.0):
        angles = np.radians(base + yaw_deg + rng.normal(0.0, noise_deg, n_per_wall))
        normals.append(
            np.stack([np.cos(angles), rng.normal(0.0, 0.02, n_per_wall), np.sin(angles)], axis=1)
        )
    return np.concatenate(normals)


def make_interior(height_mm: float = 2400.0, n: int = 500, seed: int = 2) -> Points:
    """Points scattered through the room's volume, above the floor.

    Stands in for the wall and ceiling points S5 always has to hand. Their
    only job is to say which side of the floor plane the room is on.
    """
    rng = np.random.default_rng(seed)
    return np.stack(
        [
            rng.uniform(-2000.0, 2000.0, n),
            rng.uniform(200.0, height_mm, n),
            rng.uniform(-1500.0, 1500.0, n),
        ],
        axis=1,
    )


def rotate(points: Points, rotation: NDArray[np.float64]) -> Points:
    return np.asarray(points, dtype=np.float64) @ rotation.T


class TestFloorPlane:
    def test_a_level_floor_is_found(self) -> None:
        fit = align.fit_floor_plane(make_floor(), interior_points=make_interior())
        assert align.angle_between_deg(fit.normal, np.array([0.0, 1.0, 0.0])) < 1.0
        assert abs(fit.offset_mm) < 5.0
        assert fit.inlier_fraction > 0.95

    def test_a_tilted_floor_is_found_at_its_true_tilt(self) -> None:
        """The reconstruction frame has no reason to be level, so this is the
        normal case rather than an edge case."""
        tilt = align.rotation_onto_y(np.array([0.2, 1.0, -0.1]))
        fit = align.fit_floor_plane(
            rotate(make_floor(), tilt.T), interior_points=rotate(make_interior(), tilt.T)
        )
        recovered = align.angle_between_deg(fit.normal, tilt.T @ np.array([0.0, 1.0, 0.0]))
        assert recovered < 1.0

    def test_outliers_do_not_drag_the_plane(self) -> None:
        """Furniture legs, a rug edge and depth noise all sit above the floor.
        A least-squares fit over everything would tilt; RANSAC should not."""
        floor = make_floor(n=2000)
        rng = np.random.default_rng(5)
        clutter = np.stack(
            [
                rng.uniform(0.0, 2000.0, 400),
                rng.uniform(100.0, 800.0, 400),
                rng.uniform(0.0, 1500.0, 400),
            ],
            axis=1,
        )
        fit = align.fit_floor_plane(
            np.concatenate([floor, clutter]), interior_points=make_interior()
        )
        assert align.angle_between_deg(fit.normal, np.array([0.0, 1.0, 0.0])) < 1.5
        assert abs(fit.offset_mm) < 20.0

    def test_interior_points_decide_which_way_is_up(self) -> None:
        """Sign matters more than anything else here: an inverted normal turns
        the room upside down and every height becomes negative."""
        upright = align.fit_floor_plane(make_floor(), interior_points=make_interior())
        assert upright.normal[1] > 0

        # Same floor, same geometry, but the room hangs below it. The fit has
        # to follow the evidence rather than a preference for +Y.
        inverted = align.fit_floor_plane(
            make_floor(), interior_points=make_interior() * np.array([1.0, -1.0, 1.0])
        )
        assert inverted.normal[1] < 0

    def test_a_gravity_hint_decides_the_sign(self) -> None:
        fit = align.fit_floor_plane(make_floor(), gravity_hint=np.array([0.0, -1.0, 0.0]))
        assert fit.normal[1] > 0
        assert fit.source.endswith("imu")

    def test_gravity_outranks_the_interior_points(self) -> None:
        """When both are present the accelerometer wins: it measures gravity
        directly, where the interior points only infer it."""
        fit = align.fit_floor_plane(
            make_floor(),
            interior_points=make_interior() * np.array([1.0, -1.0, 1.0]),
            gravity_hint=np.array([0.0, -1.0, 0.0]),
        )
        assert fit.normal[1] > 0

    def test_refusing_to_guess_the_up_direction(self) -> None:
        """A floor-only point set is symmetric and carries no answer. Picking
        one silently is precisely how Phase 0's adapter shipped upside-down
        rooms that still validated."""
        with pytest.raises(ValueError, match="which way is up"):
            align.fit_floor_plane(make_floor())

    def test_too_few_points_is_a_coverage_error(self) -> None:
        with pytest.raises(PipelineError) as exc:
            align.fit_floor_plane(make_floor(n=10), interior_points=make_interior())
        assert exc.value.code == "INSUFFICIENT_COVERAGE"

    def test_wrong_shape_is_a_programming_error_not_a_pipeline_one(self) -> None:
        with pytest.raises(ValueError):
            align.fit_floor_plane(np.zeros((100, 2)), interior_points=make_interior())

    def test_is_deterministic(self) -> None:
        floor, interior = make_floor(), make_interior()
        first = align.fit_floor_plane(floor, interior_points=interior)
        second = align.fit_floor_plane(floor, interior_points=interior)
        assert first.normal.tolist() == second.normal.tolist()
        assert first.offset_mm == second.offset_mm


class TestGravityCrossCheck:
    def test_agreement_passes_and_reports_the_angle(self) -> None:
        fit = align.fit_floor_plane(make_floor(), interior_points=make_interior())
        assert align.check_against_gravity_hint(fit, np.array([0.0, -1.0, 0.0])) < 1.0

    def test_a_table_top_mistaken_for_the_floor_is_caught(self) -> None:
        """This is the Phase 0 failure mode, in pipeline form: a confident fit
        to a horizontal surface that is not the floor. Here the surface is
        vertical relative to true gravity, so the instruments disagree."""
        fit = align.fit_floor_plane(make_floor(), interior_points=make_interior())
        sideways_gravity = np.array([-1.0, 0.0, 0.0])
        with pytest.raises(PipelineError) as exc:
            align.check_against_gravity_hint(fit, sideways_gravity)
        assert exc.value.code == "INSUFFICIENT_COVERAGE"
        assert "gravity" in exc.value.detail


class TestRotationOntoY:
    @pytest.mark.parametrize(
        "up",
        [
            [0.0, 1.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0],
            [0.0, -1.0, 0.0],
            [0.3, 0.9, -0.2],
        ],
    )
    def test_maps_the_given_axis_onto_y(self, up: list[float]) -> None:
        rotation = align.rotation_onto_y(np.array(up))
        mapped = rotation @ (np.array(up) / np.linalg.norm(up))
        assert np.allclose(mapped, [0.0, 1.0, 0.0], atol=1e-9)

    @pytest.mark.parametrize(
        "up", [[0.0, 1.0, 0.0], [1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.3, 0.9, -0.2]]
    )
    def test_is_a_rotation_and_never_a_reflection(self, up: list[float]) -> None:
        """A reflection also maps the axis correctly and silently mirrors the
        floor plan. Phase 0's adapter test learned this the hard way; it is
        just as true here."""
        rotation = align.rotation_onto_y(np.array(up))
        assert np.allclose(rotation @ rotation.T, np.eye(3), atol=1e-9)
        assert float(np.linalg.det(rotation)) == pytest.approx(1.0, abs=1e-9)

    def test_z_up_is_recovered(self) -> None:
        """The exact case that broke the evaluation adapter."""
        z_up_floor = make_floor()[:, [0, 2, 1]]  # swap so the floor's normal is +Z
        fit = align.fit_floor_plane(z_up_floor, interior_points=make_interior()[:, [0, 2, 1]])
        rotation = align.rotation_onto_y(fit.normal)
        levelled = rotate(z_up_floor, rotation)
        assert float(np.std(levelled[:, 1])) < 10.0


class TestManhattan:
    @pytest.mark.parametrize("true_yaw", [0.0, 7.5, 23.0, 44.0, 61.0, 89.0])
    def test_recovers_the_room_yaw_within_a_degree(self, true_yaw: float) -> None:
        fit = align.dominant_wall_yaw(make_wall_normals(true_yaw))
        # The answer is only defined modulo 90: a square room is
        # indistinguishable from itself turned a quarter turn.
        error = (fit.yaw_deg - true_yaw + 45.0) % 90.0 - 45.0
        assert abs(error) < 1.0, f"yaw {fit.yaw_deg} vs true {true_yaw}"

    def test_a_peak_straddling_the_wraparound_is_not_split(self) -> None:
        """Nearly-axis-aligned rooms are the common case and put the peak right
        on the 0/90 boundary. A plain histogram argmax splits it and can land
        the answer 45 degrees out, producing a diamond-shaped floor plan."""
        fit = align.dominant_wall_yaw(make_wall_normals(0.2, noise_deg=3.0))
        error = (fit.yaw_deg - 0.2 + 45.0) % 90.0 - 45.0
        assert abs(error) < 1.5

    def test_floor_and_ceiling_normals_are_ignored(self) -> None:
        """They are perfectly horizontal in heading terms -- i.e. meaningless --
        and there are usually far more of them than there are wall points."""
        walls = make_wall_normals(30.0)
        flat = np.tile(np.array([0.0, 1.0, 0.0]), (2000, 1))
        fit = align.dominant_wall_yaw(np.concatenate([walls, flat]))
        error = (fit.yaw_deg - 30.0 + 45.0) % 90.0 - 45.0
        assert abs(error) < 1.0
        assert fit.normals_used == len(walls)

    def test_too_few_usable_normals_raises(self) -> None:
        with pytest.raises(PipelineError):
            align.dominant_wall_yaw(make_wall_normals(10.0, n_per_wall=2))

    def test_support_is_low_for_a_non_rectilinear_room(self) -> None:
        """A curved wall has no dominant direction. The number is what tells
        S6 to expect a messy polygon (3.11's curved-wall limitation)."""
        rng = np.random.default_rng(11)
        angles = rng.uniform(0, 2 * math.pi, 800)
        scattered = np.stack([np.cos(angles), np.zeros(800), np.sin(angles)], axis=1)
        assert align.dominant_wall_yaw(scattered).support < 0.15


class TestAlignEndToEnd:
    def test_a_rotated_room_comes_back_level_and_square(self) -> None:
        true_yaw = 33.0
        floor = make_floor()
        normals = make_wall_normals(true_yaw)

        # Throw the whole room into an arbitrary reconstruction frame.
        tilt = align.rotation_onto_y(np.array([0.25, 1.0, -0.4]))
        scan_floor = rotate(floor, tilt.T)
        scan_normals = rotate(normals, tilt.T)
        scan_interior = rotate(make_interior(), tilt.T)

        result = align.align(scan_floor, scan_normals, interior_points=scan_interior)

        levelled = result.apply(scan_floor)
        assert abs(float(np.median(levelled[:, 1]))) < 5.0, "floor is not at y = 0"
        assert float(np.std(levelled[:, 1])) < 15.0, "floor is not flat"

        squared = rotate(scan_normals, result.rotation)
        assert result.manhattan is not None
        residual = align.dominant_wall_yaw(squared).yaw_deg
        assert min(residual, 90.0 - residual) < 1.0, "walls are not axis-aligned"

    def test_alignment_survives_missing_wall_normals(self) -> None:
        """A partial scan still has to produce a room. Level but unsquared is
        a worse result, not a failed one."""
        result = align.align(make_floor(), wall_normals=None, interior_points=make_interior())
        assert result.manhattan is None
        assert abs(float(np.median(result.apply(make_floor())[:, 1]))) < 5.0

    def test_unusable_wall_normals_degrade_rather_than_raise(self) -> None:
        flat_only = np.tile(np.array([0.0, 1.0, 0.0]), (500, 1))
        result = align.align(make_floor(), flat_only, interior_points=make_interior())
        assert result.manhattan is None

    def test_floor_lands_at_zero_even_when_offset_from_the_origin(self) -> None:
        shift = np.array([1200.0, 3400.0, -800.0])
        floor = make_floor() + shift
        result = align.align(floor, make_wall_normals(0.0), interior_points=make_interior() + shift)
        assert abs(float(np.median(result.apply(floor)[:, 1]))) < 5.0


class TestYawSignConvention:
    """Pinning the sign of `yaw_rotation`.

    A sign error here produced a room rotated by twice its true yaw, which
    still validated against the schema and still looked like a room. These
    tests exist so that failure is loud next time.
    """

    def test_positive_yaw_reduces_measured_heading(self) -> None:
        """Right-handed about +Y carries +X towards -Z, so `atan2(z, x)` drops."""
        x_axis = np.array([[1.0, 0.0, 0.0]])
        rotated = rotate(x_axis, align.yaw_rotation(30.0))[0]
        heading = math.degrees(math.atan2(rotated[2], rotated[0]))
        assert heading == pytest.approx(-30.0, abs=1e-6)

    def test_rotating_by_the_measured_yaw_squares_the_room(self) -> None:
        """The cancellation the aligner depends on, isolated from everything else."""
        normals = make_wall_normals(37.0)
        measured = align.dominant_wall_yaw(normals).yaw_deg
        squared = align.dominant_wall_yaw(rotate(normals, align.yaw_rotation(measured))).yaw_deg
        assert min(squared, 90.0 - squared) < 0.5

    def test_negating_the_yaw_does_not_square_the_room(self) -> None:
        """The bug, asserted directly: the intuitive sign leaves the room at
        roughly twice its original yaw."""
        normals = make_wall_normals(20.0)
        measured = align.dominant_wall_yaw(normals).yaw_deg
        wrong = align.dominant_wall_yaw(rotate(normals, align.yaw_rotation(-measured))).yaw_deg
        assert min(wrong, 90.0 - wrong) > 5.0
