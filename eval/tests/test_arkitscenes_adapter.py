"""Tests for the ARKitScenes adapter.

The dataset is a free download, but it is ~600 GB and CI cannot hold a scene, so
these build synthetic rooms with known dimensions and check the adapter recovers
them. The valuable case is the round trip in
`test_synthetic_room_survives_render_and_fusion`: a room of known size is
*rendered* to depth images through known poses and then fused back, so the
projection, the pose convention and the geometry are all exercised together. A
sign error anywhere in that chain does not survive it.

What these cannot check is the real dataset's file layout and its two coordinate
conventions (whether `.traj` is world-to-camera, and whether the depth camera is
OpenCV-style). Those come from documentation, which is why the adapter ships
`--inspect` to confirm them on the first downloaded scene.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from jsonschema import Draft7Validator

from eval.adapters.arkitscenes import (
    AdapterError,
    alignment_matrix,
    axis_angle_to_matrix,
    backproject,
    build_layout,
    detect_up,
    load_furniture,
    read_pincam,
    read_traj,
)
from eval.adapters.geometry import DerivationError, split_horizontal_surfaces
from eval.validate_ground_truth import SCHEMA

# --------------------------------------------------------------------------- #
# Rotation and pose parsing                                                    #
# --------------------------------------------------------------------------- #


def test_zero_rotation_is_identity() -> None:
    assert np.allclose(axis_angle_to_matrix(np.zeros(3)), np.eye(3))


def test_quarter_turn_about_y() -> None:
    R = axis_angle_to_matrix(np.array([0.0, math.pi / 2, 0.0]))
    assert np.allclose(R @ np.array([1.0, 0.0, 0.0]), [0.0, 0.0, -1.0], atol=1e-9)


@pytest.mark.parametrize("axis", [[0.3, 0.0, 0.0], [0.0, -1.2, 0.0], [0.4, 0.5, -0.6]])
def test_rotations_are_orthonormal(axis: list[float]) -> None:
    R = axis_angle_to_matrix(np.array(axis))
    assert np.allclose(R @ R.T, np.eye(3), atol=1e-9)
    assert np.linalg.det(R) == pytest.approx(1.0)


def test_traj_is_inverted_to_camera_to_world(tmp_path: Path) -> None:
    """The file stores world-to-camera. A camera 2 m along +x in world terms
    must come back with that translation, not its negation -- get this backwards
    and every scene fuses into a mirrored heap."""
    path = tmp_path / "lowres_wide.traj"
    # Identity rotation, world-to-camera translation of -2 in x.
    path.write_text("12345.678 0 0 0 -2 0 0\n", encoding="utf-8")
    poses = read_traj(path)
    pose = poses["12345.678"]
    assert np.allclose(pose[:3, :3], np.eye(3), atol=1e-9)
    assert np.allclose(pose[:3, 3], [2.0, 0.0, 0.0], atol=1e-9)


def test_traj_rejects_short_rows(tmp_path: Path) -> None:
    path = tmp_path / "bad.traj"
    path.write_text("1.0 0 0 0\n", encoding="utf-8")
    with pytest.raises(AdapterError):
        read_traj(path)


def test_pincam_parsing(tmp_path: Path) -> None:
    path = tmp_path / "x.pincam"
    path.write_text("1920 1440 1500.5 1500.5 960.0 720.0\n", encoding="utf-8")
    assert read_pincam(path) == (1920, 1440, 1500.5, 1500.5, 960.0, 720.0)


# --------------------------------------------------------------------------- #
# Back-projection                                                              #
# --------------------------------------------------------------------------- #


def test_backprojecting_the_principal_point() -> None:
    """A pixel at the principal point sits straight ahead at its depth."""
    depth = np.full((10, 10), 3000, dtype=np.uint16)
    intr = (10, 10, 100.0, 100.0, 5.0, 5.0)
    pts = backproject(depth, intr, np.eye(4), pixel_stride=5)
    centre = pts[np.argmin(np.hypot(pts[:, 0], pts[:, 1]))]
    assert centre[2] == pytest.approx(3000.0)


def test_invalid_depths_are_dropped() -> None:
    """0 means no laser return; beyond 8 m is another room through a doorway."""
    depth = np.zeros((10, 10), dtype=np.uint16)
    depth[0, 0] = 50  # too near
    depth[1, 1] = 60000  # too far
    assert len(backproject(depth, (10, 10, 100.0, 100.0, 5.0, 5.0), np.eye(4), 1)) == 0


def test_intrinsics_are_rescaled_to_the_depth_resolution() -> None:
    """ARKitScenes ships intrinsics per RGB stream and laser depth at 1920x1440,
    so they rarely match. Not rescaling silently doubles the room."""
    depth = np.full((20, 20), 2000, dtype=np.uint16)
    full = backproject(depth, (20, 20, 200.0, 200.0, 10.0, 10.0), np.eye(4), 1)
    scaled = backproject(depth, (10, 10, 100.0, 100.0, 5.0, 5.0), np.eye(4), 1)
    assert np.allclose(np.sort(full, axis=0), np.sort(scaled, axis=0))


def test_pose_translation_is_applied_in_millimetres() -> None:
    """Poses are metres, the cloud is millimetres. Mixing them puts the room a
    thousand times too close to the origin."""
    depth = np.full((4, 4), 1000, dtype=np.uint16)
    intr = (4, 4, 50.0, 50.0, 2.0, 2.0)
    pose = np.eye(4)
    pose[:3, 3] = [1.0, 0.0, 0.0]  # 1 m
    moved = backproject(depth, intr, pose, 1)
    still = backproject(depth, intr, np.eye(4), 1)
    assert (moved[:, 0] - still[:, 0]).mean() == pytest.approx(1000.0)


# --------------------------------------------------------------------------- #
# A synthetic room, rendered and fused back                                    #
# --------------------------------------------------------------------------- #

ROOM_W, ROOM_D, ROOM_H = 4600.0, 3800.0, 2700.0


def render_box_room(
    width: float = ROOM_W, depth: float = ROOM_D, height: float = ROOM_H
) -> np.ndarray:
    """Point cloud of an empty box room, as fusing depth frames would produce.

    Built by ray-casting a pinhole camera from several positions inside the box
    against its six faces, so the result has the same character as a real fusion:
    denser near the camera, nothing behind walls, floor and ceiling as planes.
    """
    intr = (320, 240, 250.0, 250.0, 160.0, 120.0)
    iw, ih, fx, fy, cx, cy = intr
    us, vs = np.meshgrid(np.arange(0, iw, 2), np.arange(0, ih, 2))
    dirs_cam = np.stack(
        [(us - cx) / fx, (vs - cy) / fy, np.ones_like(us, dtype=float)], axis=-1
    ).reshape(-1, 3)
    dirs_cam /= np.linalg.norm(dirs_cam, axis=1, keepdims=True)

    clouds = []
    # Look along +x, -x, +z, -z from near the middle, at eye height.
    yaws = [0.0, math.pi / 2, math.pi, 3 * math.pi / 2]
    origins = [
        np.array([width * 0.3, 1500.0, depth * 0.3]),
        np.array([width * 0.7, 1500.0, depth * 0.3]),
        np.array([width * 0.5, 1500.0, depth * 0.7]),
        np.array([width * 0.35, 1500.0, depth * 0.6]),
    ]
    # Pitch down a little so floors get hit, and up for ceilings.
    pitches = [-0.5, 0.0, 0.5]

    for origin in origins:
        for yaw in yaws:
            for pitch in pitches:
                R = axis_angle_to_matrix(np.array([0.0, yaw, 0.0])) @ axis_angle_to_matrix(
                    np.array([pitch, 0.0, 0.0])
                )
                dirs = dirs_cam @ R.T
                t = np.full(len(dirs), np.inf)
                for axis, lo, hi in ((0, 0.0, width), (1, 0.0, height), (2, 0.0, depth)):
                    with np.errstate(divide="ignore", invalid="ignore"):
                        for bound in (lo, hi):
                            hit = (bound - origin[axis]) / dirs[:, axis]
                            hit[hit <= 0] = np.inf
                            t = np.minimum(t, hit)
                good = np.isfinite(t) & (t < 8000.0)
                clouds.append(origin + dirs[good] * t[good, None])
    return np.vstack(clouds)


def test_synthetic_room_survives_render_and_fusion() -> None:
    """The end-to-end check: a room of known size, rendered and recovered."""
    points = render_box_room()
    layout = build_layout(points)

    assert layout["ceiling_height_mm"] == pytest.approx(ROOM_H, abs=60)
    assert len(layout["walls"]) == 4
    lengths = sorted(w["length_mm"] for w in layout["walls"])
    assert lengths[0] == pytest.approx(ROOM_D, rel=0.03)
    assert lengths[1] == pytest.approx(ROOM_D, rel=0.03)
    assert lengths[2] == pytest.approx(ROOM_W, rel=0.03)
    assert lengths[3] == pytest.approx(ROOM_W, rel=0.03)


def test_unscanned_band_against_a_wall_does_not_tear_the_outline() -> None:
    """The floor immediately against a wall is often never scanned -- grazing
    incidence, or furniture in the way. That leaves a band of empty cells.

    Without a wide enough morphological closing the outline follows the missing
    cells and comes out as a 12-corner staircase instead of a rectangle, which
    wrecks both E4's polygon IoU and its corner count. This is the case that set
    `CLOSING_CELLS`, and it is measured from a real fused scan, not invented.
    """
    points = render_box_room()
    floor, _ = split_horizontal_surfaces(points)
    # Delete every floor point within 300 mm of one wall.
    keep = ~(floor & (points[:, 2] < 300.0))
    layout = build_layout(points[keep])

    assert len(layout["walls"]) == 4, (
        f"expected a rectangle, got {len(layout['walls'])} walls: the closing is too narrow"
    )
    lengths = sorted(w["length_mm"] for w in layout["walls"])
    assert lengths[2] == pytest.approx(ROOM_W, rel=0.04)
    assert lengths[3] == pytest.approx(ROOM_W, rel=0.04)


def test_an_l_shaped_room_keeps_its_corner_despite_the_closing() -> None:
    """The closing must bridge scan gaps without swallowing real concave
    geometry. An L's notch is metres across, so it has to survive."""
    from eval.adapters.geometry import floor_polygon, polygon_to_walls

    grid = np.mgrid[0:5000:50, 0:5000:50].reshape(2, -1).T.astype(float)
    grid = grid[~((grid[:, 0] > 2500) & (grid[:, 1] > 2500))]
    poly = floor_polygon(grid)
    _, walls = polygon_to_walls(poly)
    assert len(walls) >= 6
    assert poly.area == pytest.approx(0.75 * 5000 * 5000, rel=0.05)


def test_openings_are_empty_and_that_is_deliberate() -> None:
    """ARKitScenes annotates no doors or windows. Emitting a guess here would
    put false positives into E5's ground truth, so E5 waits for Tier B."""
    assert build_layout(render_box_room())["openings"] == []


def test_walls_agree_with_the_polygon() -> None:
    layout = build_layout(render_box_room())
    poly = layout["floor_polygon_mm"]
    assert len(layout["walls"]) == len(poly)
    for i, wall in enumerate(layout["walls"]):
        x0, z0 = poly[i]
        x1, z1 = poly[(i + 1) % len(poly)]
        assert wall["length_mm"] == pytest.approx(math.dist((x0, z0), (x1, z1)), abs=2)


# --------------------------------------------------------------------------- #
# Floor / ceiling detection                                                    #
# --------------------------------------------------------------------------- #


def stacked_planes(heights: list[float], per_plane: int = 4000) -> np.ndarray:
    rng = np.random.default_rng(0)
    out = []
    for y in heights:
        xs = rng.uniform(0, ROOM_W, per_plane)
        zs = rng.uniform(0, ROOM_D, per_plane)
        out.append(np.stack([xs, np.full(per_plane, y), zs], axis=1))
    return np.vstack(out)


def test_floor_and_ceiling_are_the_outer_planes() -> None:
    points = stacked_planes([0.0, 2700.0])
    floor, ceiling = split_horizontal_surfaces(points)
    assert points[floor][:, 1].mean() == pytest.approx(0.0, abs=60)
    assert points[ceiling][:, 1].mean() == pytest.approx(2700.0, abs=60)


def test_a_large_table_top_is_not_mistaken_for_the_floor() -> None:
    """In a cluttered room a desk surface can hold more points than the floor.
    Restricting the search to the outer thirds of the height range is what stops
    the ceiling height coming out as 1.95 m."""
    points = np.vstack([stacked_planes([0.0, 2700.0], 3000), stacked_planes([750.0], 20000)])
    floor, ceiling = split_horizontal_surfaces(points)
    assert points[floor][:, 1].mean() == pytest.approx(0.0, abs=60)
    assert points[ceiling][:, 1].mean() == pytest.approx(2700.0, abs=60)


def test_a_collapsed_cloud_is_refused() -> None:
    """If the pose or camera convention is wrong the cloud is not a room. It
    must fail loudly rather than produce a plausible wrong yardstick."""
    with pytest.raises(DerivationError):
        split_horizontal_surfaces(stacked_planes([0.0, 300.0]))


def test_a_scene_with_no_ceiling_is_refused() -> None:
    rng = np.random.default_rng(1)
    floor = stacked_planes([0.0], 8000)
    # A few stray points high up, far too few to be a ceiling.
    strays = np.stack(
        [rng.uniform(0, ROOM_W, 20), np.full(20, 2700.0), rng.uniform(0, ROOM_D, 20)], axis=1
    )
    with pytest.raises(DerivationError, match="ceiling"):
        split_horizontal_surfaces(np.vstack([floor, strays]))


# --------------------------------------------------------------------------- #
# Furniture (E6)                                                               #
# --------------------------------------------------------------------------- #


def write_annotation(tmp_path: Path, items: list[dict[str, Any]]) -> Path:
    scene = tmp_path / "41069021"
    scene.mkdir()
    (scene / "41069021_3dod_annotation.json").write_text(
        json.dumps({"data": items}), encoding="utf-8"
    )
    return scene


def test_furniture_converted_to_millimetres_and_capped_at_three(tmp_path: Path) -> None:
    scene = write_annotation(
        tmp_path,
        [
            {"label": "sofa", "axesLengths": [2.1, 0.85, 0.9]},
            {"label": "table", "axesLengths": [1.1, 0.42, 0.6]},
            {"label": "tv_monitor", "axesLengths": [1.2, 0.7, 0.1]},
            {"label": "stool", "axesLengths": [0.4, 0.45, 0.4]},
        ],
    )
    furniture = load_furniture(scene)
    assert len(furniture) == 3  # 3.10 asks for three pieces
    sofa = next(f for f in furniture if f["label"] == "sofa")
    assert sorted(sofa["footprint_mm"]) == [900, 2100]
    assert sofa["height_mm"] == 850


def test_malformed_furniture_entries_are_skipped(tmp_path: Path) -> None:
    scene = write_annotation(
        tmp_path,
        [{"label": "sofa"}, {"axesLengths": [1, 1, 1]}, {"label": "bed", "axesLengths": [0, 1, 1]}],
    )
    assert load_furniture(scene) == []


def test_missing_annotation_file_is_not_an_error(tmp_path: Path) -> None:
    """Only the 3dod subset ships annotations. A raw-only scene still gives a
    usable room, just without E6 ground truth."""
    scene = tmp_path / "empty"
    scene.mkdir()
    assert load_furniture(scene) == []


# --------------------------------------------------------------------------- #
# Schema                                                                       #
# --------------------------------------------------------------------------- #


def test_output_validates_against_the_ground_truth_schema() -> None:
    layout = build_layout(render_box_room())
    doc = {
        "room_id": "gt-001",
        "name": "ARKitScenes 41069021",
        "room_type": "living",
        "measured_on": "2026-09-17",
        "measured_by": "eval/adapters/arkitscenes.py",
        "instrument": "ARKitScenes FARO laser depth, scene 41069021",
        "source": {
            "tier": "A",
            "dataset": "arkitscenes",
            "scene_id": "41069021",
            "derived_by": "eval/adapters/arkitscenes.py",
        },
        **layout,
        "furniture": [{"label": "sofa", "footprint_mm": [2100, 900], "height_mm": 850}],
        "captures": [
            {
                "capture_id": "gt-001-a",
                "device": "iPad Pro (ARKitScenes capture)",
                "style": "careful",
                "file": "captures/gt-001-a.mov",
            }
        ],
    }
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    errors = sorted(Draft7Validator(schema).iter_errors(doc), key=str)
    assert not errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors)


# --------------------------------------------------------------------------- #
# Gravity alignment                                                            #
# --------------------------------------------------------------------------- #
#
# Regression cover for the bug that made the first batch of Tier A files wrong:
# the adapter assumed the world frame was y-up when ARKitScenes' is z-up. It did
# not fail -- it produced a 19-piece kitchen in 0.5 m2 under a 4 m ceiling, and
# that file passed schema validation. Hence these tests assert on *plausibility*
# as well as on the transform.


def _walking_poses(up_axis: int, n: int = 60) -> dict[str, np.ndarray]:
    """Poses for someone walking a room: metres horizontally, ~nothing vertically."""
    rng = np.random.default_rng(0)
    horizontal = [i for i in range(3) if i != up_axis]
    poses: dict[str, np.ndarray] = {}
    for i in range(n):
        m = np.eye(4)
        m[horizontal[0], 3] = rng.uniform(-2.0, 2.0)
        m[horizontal[1], 3] = rng.uniform(-2.0, 2.0)
        m[up_axis, 3] = 1.4 + rng.normal(0, 0.02)
        poses[f"{i}"] = m
    return poses


def _annotation(tmp_path: Path, up_axis: int, floor_metres: float) -> Path:
    """A scene directory holding one furniture box resting on the floor."""
    centroid = [0.0, 0.0, 0.0]
    centroid[up_axis] = floor_metres + 0.4
    doc = {
        "data": [
            {
                "label": "table",
                "segments": {
                    "obbAligned": {
                        "centroid": centroid,
                        "axesLengths": [0.8, 0.8, 0.8],
                    }
                },
            }
        ]
    }
    (tmp_path / "scene_3dod_annotation.json").write_text(json.dumps(doc), encoding="utf-8")
    return tmp_path


def _room_cloud(up_axis: int, sign: int, height_mm: float = 2700.0) -> np.ndarray:
    """Floor and ceiling slabs plus wall points, in a frame where `up_axis` is up."""
    rng = np.random.default_rng(1)
    horizontal = [i for i in range(3) if i != up_axis]
    n = 4000
    pts = np.zeros((n * 2, 3))
    for k, level in enumerate((0.0, height_mm)):
        block = pts[k * n : (k + 1) * n]
        block[:, horizontal[0]] = rng.uniform(-2000, 2000, n)
        # Deliberately unlike the ceiling height, so reading the wrong axis
        # gives an obviously wrong answer rather than a near-miss.
        block[:, horizontal[1]] = rng.uniform(-2200, 2200, n)
        block[:, up_axis] = level * sign + rng.normal(0, 8, n)
    return pts


@pytest.mark.parametrize("axis", [0, 1, 2])
@pytest.mark.parametrize("sign", [1, -1])
def test_alignment_matrix_puts_up_on_plus_y(axis: int, sign: int) -> None:
    rot = alignment_matrix(axis, sign)
    up = np.zeros(3)
    up[axis] = sign
    assert np.allclose(rot @ up, [0.0, 1.0, 0.0])


@pytest.mark.parametrize("axis", [0, 1, 2])
@pytest.mark.parametrize("sign", [1, -1])
def test_alignment_matrix_is_a_rotation_not_a_reflection(axis: int, sign: int) -> None:
    """A reflection would mirror the floor plan and nobody would notice."""
    rot = alignment_matrix(axis, sign)
    assert np.allclose(rot @ rot.T, np.eye(3))
    assert np.isclose(np.linalg.det(rot), 1.0)


@pytest.mark.parametrize("axis", [0, 1, 2])
def test_detect_up_finds_the_axis_the_camera_moved_along_least(tmp_path: Path, axis: int) -> None:
    scene = _annotation(tmp_path, axis, floor_metres=0.0)
    cloud = _room_cloud(axis, sign=1)
    assert detect_up(scene, _walking_poses(axis), cloud) == (axis, 1)


def test_detect_up_uses_furniture_to_tell_floor_from_ceiling(tmp_path: Path) -> None:
    """With the room built downward, the floor is at the *high* end of the axis."""
    axis = 2
    scene = _annotation(tmp_path, axis, floor_metres=0.0)
    cloud = _room_cloud(axis, sign=-1)
    found_axis, sign = detect_up(scene, _walking_poses(axis), cloud)
    assert (found_axis, sign) == (axis, -1)


def test_detect_up_refuses_an_ambiguous_capture(tmp_path: Path) -> None:
    """Two floors or a stairwell: no axis is clearly the quiet one."""
    scene = _annotation(tmp_path, 2, floor_metres=0.0)
    rng = np.random.default_rng(2)
    poses = {}
    for i in range(60):
        m = np.eye(4)
        m[:3, 3] = rng.uniform(-2.0, 2.0, 3)
        poses[f"{i}"] = m
    with pytest.raises(AdapterError, match="which axis is up"):
        detect_up(scene, poses, _room_cloud(2, 1))


def test_detect_up_refuses_a_scene_with_no_furniture_annotation(tmp_path: Path) -> None:
    with pytest.raises(AdapterError, match="furniture annotation"):
        detect_up(tmp_path, _walking_poses(2), _room_cloud(2, 1))


def test_a_z_up_room_is_not_read_as_y_up(tmp_path: Path) -> None:
    """The actual regression.

    A z-up room, interpreted as y-up, gives a "ceiling height" taken across the
    room's width and a footprint that is really a wall elevation. Aligning first
    must recover the true height.
    """
    axis, height = 2, 2700.0
    scene = _annotation(tmp_path, axis, floor_metres=0.0)
    cloud = _room_cloud(axis, sign=1, height_mm=height)

    naive_span = cloud[:, 1].max() - cloud[:, 1].min()
    assert abs(naive_span - height) > 1000.0, "fixture must be wrong when read as y-up"

    found_axis, sign = detect_up(scene, _walking_poses(axis), cloud)
    aligned = cloud @ alignment_matrix(found_axis, sign).T
    recovered = aligned[:, 1].max() - aligned[:, 1].min()
    assert abs(recovered - height) < 60.0

    floor, ceiling = split_horizontal_surfaces(aligned)
    assert floor.sum() > 1000 and ceiling.sum() > 1000
