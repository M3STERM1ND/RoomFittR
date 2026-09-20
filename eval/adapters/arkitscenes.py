"""ARKitScenes scene -> `eval/ground_truth/gt-NNN.yaml`.

    uv run python -m eval.adapters.arkitscenes --scene /data/arkitscenes/raw/Training/41069021 \\
        --room-id gt-001 --room-type living --traits cluttered

Tier A of the evaluation set (implementation-plan.md 3.10). ARKitScenes was
chosen over ScanNet++, ZInD and Matterport3D because it is the only public
dataset pairing *real* commodity captures with laser ground truth that does not
gate access behind academic credentials: it is a plain download script, no
account and no affiliation check.

    https://github.com/apple-aiml-research/ARKitScenes   (moved from apple/ARKitScenes)

Files this reads, per the dataset documentation:

    <scene>/<id>_frames/highres_depth/*.png   FARO laser depth, uint16 mm, 1920x1440
    <scene>/<id>_frames/lowres_wide.traj      camera poses: ts + axis-angle + metres
    <scene>/<id>_frames/*.pincam              "w h fx fy cx cy", one line
    <scene>/<id>_3dod_annotation.json         furniture OBBs -> E6 ground truth

The layout is derived by back-projecting the laser depth through the poses into
a room-scoped cloud, then splitting floor and ceiling by height histogram
(`geometry.split_horizontal_surfaces`) and tracing the floor outline.

**No openings.** The ARKitScenes taxonomy is 17 furniture classes and contains
neither `door` nor `window`, so E5 has no Tier A ground truth and is decided on
Tier B instead (3.10). `openings` is emitted empty and the note says so, rather
than being guessed at from holes in the geometry -- which would be solving E5 in
order to score E5.

**Verify on first use.** Paths and conventions below come from the published
documentation, not from a downloaded scene. Two things in particular are worth
confirming on scene one, and both are what `--inspect` exists for:

1. Whether `.traj` stores world-to-camera (assumed here, and inverted) or the
   other way round.
2. Whether the depth camera follows the OpenCV convention assumed by
   `backproject` (+x right, +y down, +z forward).

If either is wrong the fused cloud is nonsense, and `split_horizontal_surfaces`
raises rather than returning a plausible-looking wrong answer.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from eval.adapters.geometry import (
    DerivationError,
    ceiling_height,
    floor_polygon,
    polygon_to_walls,
    split_horizontal_surfaces,
)

EVAL_DIR = Path(__file__).resolve().parents[1]
GROUND_TRUTH_DIR = EVAL_DIR / "ground_truth"

METRES_TO_MM = 1000.0

# ARKitScenes depth is uint16 millimetres. 0 means "no return". Beyond about
# 8 m a laser-projected depth in a home is either a corridor beyond a doorway or
# noise, and either way it is not this room.
MIN_DEPTH_MM = 200.0
MAX_DEPTH_MM = 8000.0

# Fusing every frame of a two-minute capture is tens of millions of points for no
# extra accuracy -- consecutive frames see the same surfaces. Sampling frames and
# pixels keeps a scene to a few million points and a few seconds.
DEFAULT_FRAME_STRIDE = 10
DEFAULT_PIXEL_STRIDE = 8

# The up axis must be clearly the least-varying one. Above this ratio against
# the next axis the scene is refused instead of guessed at.
UP_AXIS_MARGIN = 0.8


class AdapterError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Reading                                                                      #
# --------------------------------------------------------------------------- #


def axis_angle_to_matrix(axis_angle: np.ndarray) -> np.ndarray:
    """Rodrigues' formula. Kept explicit so it can be tested on its own."""
    theta = float(np.linalg.norm(axis_angle))
    if theta < 1e-12:
        return np.eye(3)
    kx, ky, kz = (axis_angle / theta).astype(np.float64)
    K = np.array([[0.0, -kz, ky], [kz, 0.0, -kx], [-ky, kx, 0.0]], dtype=np.float64)
    rotation: np.ndarray = np.eye(3, dtype=np.float64) + math.sin(theta) * K
    rotation += (1.0 - math.cos(theta)) * (K @ K)
    return rotation


def read_traj(path: Path) -> dict[str, np.ndarray]:
    """Timestamp -> 4x4 camera-to-world pose.

    The file stores *world-to-camera* (this is what ARKitScenes' own loader
    calls `r_w_to_p` before inverting it), so each row is inverted here. Getting
    this backwards produces a cloud that is not a room at all, which
    `split_horizontal_surfaces` refuses rather than quietly accepting.
    """
    if not path.is_file():
        raise AdapterError(f"missing {path}")

    poses: dict[str, np.ndarray] = {}
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        tokens = line.split()
        if not tokens:
            continue
        if len(tokens) < 7:
            raise AdapterError(f"{path}:{lineno}: expected 7 columns, got {len(tokens)}")
        ts = tokens[0]
        try:
            values = [float(t) for t in tokens[1:7]]
        except ValueError as e:
            raise AdapterError(f"{path}:{lineno}: non-numeric pose") from e

        world_to_cam = np.eye(4)
        world_to_cam[:3, :3] = axis_angle_to_matrix(np.array(values[:3]))
        world_to_cam[:3, 3] = values[3:6]
        poses[ts] = np.linalg.inv(world_to_cam)
    if not poses:
        raise AdapterError(f"{path} contained no poses")
    return poses


def read_pincam(path: Path) -> tuple[int, int, float, float, float, float]:
    """`width height fx fy cx cy`, one line."""
    tokens = path.read_text(encoding="utf-8").split()
    if len(tokens) < 6:
        raise AdapterError(f"{path}: expected 6 values, got {len(tokens)}")
    w, h, fx, fy, cx, cy = (float(t) for t in tokens[:6])
    return int(w), int(h), fx, fy, cx, cy


def backproject(
    depth_mm: np.ndarray,
    intrinsics: tuple[int, int, float, float, float, float],
    cam_to_world: np.ndarray,
    pixel_stride: int = DEFAULT_PIXEL_STRIDE,
) -> np.ndarray:
    """Depth image -> (N, 3) world points in millimetres.

    Assumes the OpenCV pinhole convention: +x right, +y down, +z forward. The
    intrinsics are scaled if the depth image is not the resolution they were
    recorded at, which is the usual case -- ARKitScenes ships intrinsics per RGB
    stream and the laser depth at 1920x1440.
    """
    height, width = depth_mm.shape
    iw, ih, fx, fy, cx, cy = intrinsics
    if iw != width or ih != height:
        sx, sy = width / iw, height / ih
        fx, fy, cx, cy = fx * sx, fy * sy, cx * sx, cy * sy

    vs = np.arange(0, height, pixel_stride)
    us = np.arange(0, width, pixel_stride)
    grid_u, grid_v = np.meshgrid(us, vs)
    z = depth_mm[np.ix_(vs, us)].astype(float)

    valid = (z >= MIN_DEPTH_MM) & (z <= MAX_DEPTH_MM)
    if not valid.any():
        return np.empty((0, 3))

    z = z[valid]
    u = grid_u[valid].astype(float)
    v = grid_v[valid].astype(float)

    cam = np.stack([(u - cx) * z / fx, (v - cy) * z / fy, z], axis=1).astype(np.float64)
    pose = cam_to_world.astype(np.float64)
    world: np.ndarray = cam @ pose[:3, :3].T + pose[:3, 3] * METRES_TO_MM
    return world


def find_frames_dir(scene: Path) -> Path:
    for candidate in sorted(scene.glob("*_frames")):
        if candidate.is_dir():
            return candidate
    # Some downloads place the streams directly in the scene directory.
    if (scene / "highres_depth").is_dir():
        return scene
    raise AdapterError(f"no *_frames directory under {scene}")


def find_intrinsics(frames: Path) -> Path:
    for pattern in ("wide_intrinsics/*.pincam", "lowres_wide_intrinsics/*.pincam", "*.pincam"):
        matches = sorted(frames.glob(pattern))
        if matches:
            return matches[0]
    raise AdapterError(f"no .pincam intrinsics under {frames}")


def camera_centres(poses: dict[str, np.ndarray]) -> np.ndarray:
    """(N, 3) camera positions in millimetres."""
    return np.array([m[:3, 3] for m in poses.values()]) * METRES_TO_MM


def furniture_bottoms(scene: Path, axis: int) -> list[float]:
    """Lowest extent of each annotated box along `axis`, in millimetres.

    Furniture rests on the floor, so these cluster at the floor plane. That is
    what tells floor from ceiling -- and it comes from Apple's annotations, not
    from our own geometry, so it is an independent check rather than a
    restatement of an assumption.
    """
    out: list[float] = []
    for path in sorted(scene.glob("*annotation*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for obj in doc.get("data", []):
            seg = obj.get("segments", {}).get("obbAligned", {})
            centroid, lengths = seg.get("centroid"), seg.get("axesLengths")
            if centroid and lengths and len(centroid) == 3:
                out.append((centroid[axis] - max(lengths) / 2.0) * METRES_TO_MM)
    return out


def detect_up(scene: Path, poses: dict[str, np.ndarray], points_mm: np.ndarray) -> tuple[int, int]:
    """Which world axis is up, and which way. Returns (axis, sign).

    ARKitScenes' world frame is **z-up**, not the y-up this file originally
    assumed -- an error that produced rooms like a 19-piece kitchen in 0.5 m2
    under a 4 m ceiling. Rather than swap one hard-coded axis for another, the
    axis is measured per scene from two independent signals:

    * **Which axis:** the spread of camera centres. Somebody walking a room
      moves metres horizontally and centimetres vertically, so the up axis is
      the one they moved along least. The camera's own down-vector is *not*
      usable here: when the device is held sideways (ARKitScenes'
      `sky_direction` of `Left`, the majority of the dataset) image-down is not
      world-down, though the pose accounts for it correctly.
    * **Which way:** the annotated furniture, which sits on the floor.

    Raises rather than guessing. A wrong up-axis does not fail loudly downstream
    -- it yields a plausible-looking room of the wrong size, which is the one
    outcome a yardstick must never have.
    """
    centres = camera_centres(poses)
    if len(centres) < 8:
        raise AdapterError(f"only {len(centres)} poses; cannot establish which way is up")

    spread = centres.std(axis=0)
    order = np.argsort(spread)
    axis = int(order[0])
    if spread[axis] > UP_AXIS_MARGIN * spread[order[1]]:
        raise AdapterError(
            "cannot tell which axis is up: camera spread is "
            f"{spread[0]:.0f}/{spread[1]:.0f}/{spread[2]:.0f} mm on x/y/z, with no axis "
            "clearly smallest. The capture may be a stairwell or span two floors."
        )

    bottoms = furniture_bottoms(scene, axis)
    if not bottoms:
        raise AdapterError(
            f"no usable furniture annotation under {scene}; without it, floor cannot be "
            "told from ceiling. Re-download this scene with the `annotation` asset."
        )

    v = points_mm[:, axis]
    low, high = float(np.percentile(v, 0.5)), float(np.percentile(v, 99.5))
    median_bottom = float(np.median(bottoms))
    sign = 1 if abs(median_bottom - low) < abs(median_bottom - high) else -1
    return axis, sign


def alignment_matrix(axis: int, sign: int) -> np.ndarray:
    """Rotation mapping the detected up-direction onto +y.

    Downstream geometry (`geometry.split_horizontal_surfaces`, `floor_polygon`)
    is written for a y-up cloud. Rotating once here keeps that code, and its
    tests, untouched.
    """
    up = np.zeros(3)
    up[axis] = float(sign)
    other = [i for i in range(3) if i != axis]
    x = np.zeros(3)
    x[other[0]] = 1.0
    z = np.cross(x, up)
    rot = np.stack([x, up, z])
    if np.linalg.det(rot) < 0:
        rot[2] = -rot[2]
    return rot


def fuse_scene(
    scene: Path,
    frame_stride: int = DEFAULT_FRAME_STRIDE,
    pixel_stride: int = DEFAULT_PIXEL_STRIDE,
) -> np.ndarray:
    """Every sampled laser depth frame, back-projected into one world cloud (mm)."""
    try:
        from PIL import Image
    except ImportError as e:  # pragma: no cover - dependency is declared
        raise AdapterError("pillow is required: uv sync") from e

    frames = find_frames_dir(scene)
    depth_dir = frames / "highres_depth"
    if not depth_dir.is_dir():
        raise AdapterError(f"missing {depth_dir}")

    depth_files = sorted(depth_dir.glob("*.png"))
    if not depth_files:
        raise AdapterError(f"no depth PNGs in {depth_dir}")

    traj = next(iter(sorted(frames.glob("*.traj"))), None)
    if traj is None:
        raise AdapterError(f"no .traj file under {frames}")
    poses = read_traj(traj)
    intrinsics = read_pincam(find_intrinsics(frames))

    chunks: list[np.ndarray] = []
    matched = 0
    for path in depth_files[::frame_stride]:
        # Files are named "<video_id>_<timestamp>.png"; the pose table is keyed
        # by that timestamp. Rounding differs between streams, so the nearest
        # pose within a frame time is taken rather than requiring an exact hit.
        stamp = path.stem.split("_")[-1]
        # Explicit None check: `a or b` on a numpy array raises, because an
        # array has no single truth value.
        pose = poses.get(stamp)
        if pose is None:
            pose = nearest_pose(poses, stamp)
        if pose is None:
            continue
        depth = np.asarray(Image.open(path)).astype(np.uint16)
        pts = backproject(depth, intrinsics, pose, pixel_stride)
        if len(pts):
            chunks.append(pts)
            matched += 1

    if not chunks:
        raise AdapterError(
            f"no depth frame could be matched to a pose in {traj.name}; "
            "timestamps may not line up between streams"
        )
    points = np.vstack(chunks)

    # Rotate into the y-up frame the geometry helpers expect. See detect_up.
    axis, sign = detect_up(scene, poses, points)
    aligned: np.ndarray = points.astype(np.float64) @ alignment_matrix(axis, sign).T
    return aligned


def nearest_pose(
    poses: dict[str, np.ndarray], stamp: str, tol_s: float = 0.05
) -> np.ndarray | None:
    try:
        target = float(stamp)
    except ValueError:
        return None
    best_key, best_gap = None, tol_s
    for key in poses:
        try:
            gap = abs(float(key) - target)
        except ValueError:
            continue
        if gap <= best_gap:
            best_key, best_gap = key, gap
    return poses[best_key] if best_key is not None else None


def load_furniture(scene: Path) -> list[dict[str, Any]]:
    """Furniture footprints from the 3DOD annotation, for E6.

    ScanNet++ gave us openings and no furniture boxes; ARKitScenes is the other
    way round. 3.10 asks for three pieces per room, so the three largest are
    kept.
    """
    matches = sorted(scene.glob("*_3dod_annotation.json"))
    if not matches:
        return []
    doc = json.loads(matches[0].read_text(encoding="utf-8"))

    out: list[dict[str, Any]] = []
    for item in doc.get("data", []):
        label = str(item.get("label", "")).strip().lower().replace("_", " ")
        extent = item.get("segments", {}).get("obbAligned", {}).get("axesLengths") or item.get(
            "axesLengths"
        )
        if not label or not extent or len(extent) < 3:
            continue
        dims_mm = sorted(float(v) * METRES_TO_MM for v in extent[:3])
        # axesLengths has no fixed axis order, so the tallest dimension is taken
        # as height and the other two as the footprint.
        footprint = [int(round(dims_mm[2])), int(round(dims_mm[1]))]
        height = int(round(dims_mm[0]))
        if min(*footprint, height) <= 0:
            continue
        out.append({"label": label, "footprint_mm": footprint, "height_mm": height})

    out.sort(key=lambda f: f["footprint_mm"][0] * f["footprint_mm"][1], reverse=True)
    return out[:3]


# --------------------------------------------------------------------------- #
# Converting                                                                   #
# --------------------------------------------------------------------------- #


def build_layout(points_mm: np.ndarray) -> dict[str, Any]:
    """Cloud in ARKit world axes (Y up, gravity-aligned, metres->mm) -> layout.

    No axis conversion: ARKit's world frame is already right-handed Y-up, which
    is what 2.4 specifies. ScanNet++ needed one because its clouds are Z-up.
    """
    floor_mask, ceiling_mask = split_horizontal_surfaces(points_mm)
    floor_pts = points_mm[floor_mask]

    poly = floor_polygon(floor_pts[:, [0, 2]])
    polygon_mm, walls = polygon_to_walls(poly)
    height = ceiling_height(floor_pts[:, 1], points_mm[ceiling_mask][:, 1])

    return {
        "ceiling_height_mm": height,
        "floor_polygon_mm": polygon_mm,
        "walls": [{"id": w.id, "length_mm": int(round(w.length_mm))} for w in walls],
        # E5 is decided on Tier B: ARKitScenes annotates no doors or windows.
        "openings": [],
    }


def build_document(
    scene: Path,
    room_id: str,
    room_type: str,
    traits: list[str],
    name: str | None,
    frame_stride: int,
    pixel_stride: int,
) -> dict[str, Any]:
    points = fuse_scene(scene, frame_stride, pixel_stride)
    layout = build_layout(points)
    furniture = load_furniture(scene)

    video = next(iter(sorted(scene.glob("*.mov"))), None)
    doc: dict[str, Any] = {
        "room_id": room_id,
        "name": name or f"ARKitScenes {scene.name}",
        "room_type": room_type,
        "measured_on": "2026-09-17",
        "measured_by": "eval/adapters/arkitscenes.py",
        "instrument": f"ARKitScenes FARO laser depth, scene {scene.name}",
        "source": {
            "tier": "A",
            "dataset": "arkitscenes",
            "scene_id": scene.name,
            "derived_by": "eval/adapters/arkitscenes.py",
        },
        **layout,
        "captures": [
            {
                "capture_id": f"{room_id}-a",
                "device": "iPad Pro (ARKitScenes capture)",
                "style": "careful",
                "file": (Path("captures") / f"{room_id}-a.mov").as_posix(),
                "note": (
                    f"copy or symlink from {video.as_posix()}"
                    if video
                    else "download the 'mov' asset for this scene"
                ),
            }
        ],
        "notes": (
            "Tier A ground truth: derived from ARKitScenes' FARO laser depth, not measured "
            "by hand. openings is empty on purpose -- ARKitScenes annotates 17 furniture "
            "classes and no doors or windows, so E5 is decided on Tier B (3.10). "
            "Spot-check the floor polygon against the capture before trusting E4 on it."
        ),
    }
    if traits:
        doc["traits"] = traits
    if furniture:
        doc["furniture"] = furniture
    return doc


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #


def inspect(scene: Path, frame_stride: int, pixel_stride: int) -> int:
    """Report what is present and whether the conventions hold, writing nothing.

    Run this on the very first scene. The path layout and the two coordinate
    conventions come from documentation rather than from data, and this is the
    cheapest place to find out that one of them is wrong.
    """
    print(f"scene: {scene}")
    try:
        frames = find_frames_dir(scene)
        print(f"  [found  ] frames dir: {frames.name}")
    except AdapterError as e:
        print(f"  [MISSING] {e}")
        return 1

    ok = True
    depth_files = sorted((frames / "highres_depth").glob("*.png"))
    print(f"  [{'found  ' if depth_files else 'MISSING'}] highres_depth: {len(depth_files)} frames")
    ok &= bool(depth_files)

    traj = next(iter(sorted(frames.glob("*.traj"))), None)
    if traj:
        try:
            poses = read_traj(traj)
            print(f"  [found  ] {traj.name}: {len(poses)} poses")
        except AdapterError as e:
            print(f"  [BAD    ] {traj.name}: {e}")
            ok = False
    else:
        print("  [MISSING] no .traj")
        ok = False

    try:
        intr = find_intrinsics(frames)
        print(f"  [found  ] intrinsics: {intr.name} -> {read_pincam(intr)}")
    except AdapterError as e:
        print(f"  [MISSING] {e}")
        ok = False

    furniture = load_furniture(scene)
    print(f"  [{'found  ' if furniture else 'none   '}] furniture boxes: {len(furniture)}")

    if not ok:
        print("\nnot usable as-is -- see above")
        return 1

    print("\n  fusing a sample to check the coordinate conventions...")
    try:
        points = fuse_scene(scene, max(frame_stride * 4, 1), pixel_stride * 2)
    except (AdapterError, DerivationError) as e:
        print(f"  fusion failed: {e}")
        return 1

    lo, hi = points.min(axis=0), points.max(axis=0)
    print(f"  {len(points):,} points")
    print(
        f"  extent mm: x {lo[0]:.0f}..{hi[0]:.0f}  "
        f"y {lo[1]:.0f}..{hi[1]:.0f}  z {lo[2]:.0f}..{hi[2]:.0f}"
    )
    print(f"  vertical span: {hi[1] - lo[1]:.0f} mm  (expect roughly a room height)")

    try:
        floor, ceiling = split_horizontal_surfaces(points)
        print(
            f"  floor plane: {floor.sum():,} pts at y~{points[floor][:, 1].mean():.0f} mm; "
            f"ceiling: {ceiling.sum():,} pts at y~{points[ceiling][:, 1].mean():.0f} mm"
        )
        implied = ceiling_height(points[floor][:, 1], points[ceiling][:, 1])
        print(f"  implied ceiling height: {implied} mm")
    except DerivationError as e:
        print(f"  surfaces: {e}")
        print("  -> most likely the .traj or camera convention assumed in this file is wrong.")
        return 1

    print("\nlooks usable")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", required=True, type=Path, help="An ARKitScenes scene directory.")
    parser.add_argument("--room-id", help="Target id, e.g. gt-001. Also the filename stem.")
    parser.add_argument(
        "--room-type",
        default="other",
        choices=["living", "bedroom", "office", "kitchen_dining", "open_plan", "other"],
    )
    parser.add_argument("--traits", nargs="*", default=[], help="Why this scene is in the set.")
    parser.add_argument("--name", help="Human-readable name. Defaults to the scene id.")
    parser.add_argument("--frame-stride", type=int, default=DEFAULT_FRAME_STRIDE)
    parser.add_argument("--pixel-stride", type=int, default=DEFAULT_PIXEL_STRIDE)
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="Report what the scene contains, check conventions, and exit.",
    )
    parser.add_argument("--stdout", action="store_true", help="Print instead of writing the file.")
    args = parser.parse_args(argv)

    if args.inspect:
        return inspect(args.scene, args.frame_stride, args.pixel_stride)
    if not args.room_id:
        parser.error("--room-id is required unless --inspect is given")

    try:
        doc = build_document(
            args.scene,
            args.room_id,
            args.room_type,
            args.traits,
            args.name,
            args.frame_stride,
            args.pixel_stride,
        )
    except (AdapterError, DerivationError) as e:
        print(f"{args.scene.name}: {e}", file=sys.stderr)
        return 1

    text = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=88)
    if args.stdout:
        print(text)
    else:
        out = GROUND_TRUTH_DIR / f"{args.room_id}.yaml"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}")

    print(
        f"  {len(doc['walls'])} walls, ceiling {doc['ceiling_height_mm']} mm, "
        f"{len(doc.get('furniture', []))} furniture pieces, 0 openings (E5 is Tier B)"
    )
    print("  now run: uv run python -m eval.validate_ground_truth")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
