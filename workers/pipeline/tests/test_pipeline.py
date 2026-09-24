"""The S1-S9 chain, driven by a synthetic reconstruction.

No GPU and no video. `frames.run` is the only stage that needs a real file, so
it is replaced; everything after it runs for real against a reconstruction of
a room whose dimensions are known exactly, which is what makes the assertions
meaningful rather than merely green.

The room is a 4 m x 3 m x 2.6 m box. Every number below is checked against
that, so a stage that silently drops a factor -- the class of bug the VGGT
pose inversion would have been -- fails here rather than in a bake-off.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
from roomfittr_pipeline import backends, frames, ingest, pipeline
from roomfittr_pipeline.backends import Reconstruction

ROOM_X_MM = 4000.0
ROOM_Z_MM = 3000.0
ROOM_Y_MM = 2600.0

# The synthetic reconstruction is emitted in metres, so a correct pipeline has
# to recover a factor of 1000 to get back to millimetres.
UNITS_PER_MM = 1.0 / 1000.0

# Down, as 3.2's motion sidecar reports it and as ARKitScenes' poses are
# already aligned to. Without it -- or a segmented floor -- no point set
# can say which way is up, and the pipeline refuses rather than guesses.
GRAVITY = np.array([0.0, -1.0, 0.0])


def _box_surface_points(rng: np.random.Generator, count: int = 900000) -> np.ndarray:
    """Points on the floor, ceiling and four walls of the box, in metres."""
    x_half, z_half = ROOM_X_MM / 2, ROOM_Z_MM / 2
    faces = []
    n = count // 6
    # Floor and ceiling.
    for y in (0.0, ROOM_Y_MM):
        xs = rng.uniform(-x_half, x_half, n)
        zs = rng.uniform(-z_half, z_half, n)
        faces.append(np.stack([xs, np.full(n, y), zs], axis=1))
    # Walls at +/- x and +/- z.
    for x in (-x_half, x_half):
        ys = rng.uniform(0.0, ROOM_Y_MM, n)
        zs = rng.uniform(-z_half, z_half, n)
        faces.append(np.stack([np.full(n, x), ys, zs], axis=1))
    for z in (-z_half, z_half):
        ys = rng.uniform(0.0, ROOM_Y_MM, n)
        xs = rng.uniform(-x_half, x_half, n)
        faces.append(np.stack([xs, ys, np.full(n, z)], axis=1))
    return np.concatenate(faces, axis=0) * UNITS_PER_MM


class SyntheticBackend:
    """A `ReconstructionBackend` that renders the known box.

    Depth is produced by projecting the box's surface points into each camera,
    so the pipeline's own back-projection has to invert exactly what this did.
    That round trip is the point: a reconstruction handed straight through as
    a point cloud would not exercise `backproject`, the intrinsics, or the
    pose convention at all.
    """

    name = "synthetic"

    # Enough points and enough pixels that the rendered depth maps are
    # *dense*. A sparse map lifts to a sparse cloud, S6 finds too little
    # evidence behind each boundary edge, and the room is rejected for
    # insufficient coverage -- which is S6 being right about a bad input
    # rather than a bug, but it is not what this test is trying to say.
    def __init__(self, frame_count: int = 12, size: int = 200) -> None:
        self.frame_count = frame_count
        self.size = size

    def reconstruct(self, frame_paths: list[Path]) -> Reconstruction:
        rng = np.random.default_rng(7)
        points = _box_surface_points(rng)
        n, s = self.frame_count, self.size

        focal = s * 0.8
        K = np.array([[focal, 0.0, s / 2], [0.0, focal, s / 2], [0.0, 0.0, 1.0]])

        poses = np.zeros((n, 4, 4))
        depth = np.zeros((n, s, s), dtype=np.float32)
        confidence = np.ones((n, s, s), dtype=np.float32)

        # Cameras on a circle at eye height, looking outward, so between them
        # they see all four walls plus floor and ceiling.
        radius = 0.6 * UNITS_PER_MM * min(ROOM_X_MM, ROOM_Z_MM) / 2
        for i in range(n):
            angle = 2 * np.pi * i / n
            centre = np.array(
                [radius * np.cos(angle), 1500.0 * UNITS_PER_MM, radius * np.sin(angle)]
            )
            forward = np.array([np.cos(angle), 0.0, np.sin(angle)])
            right = np.array([-np.sin(angle), 0.0, np.cos(angle)])
            down = np.cross(forward, right)
            # Camera-to-world, OpenCV axes: +x right, +y down, +z forward.
            rotation = np.stack([right, down, forward], axis=1)
            poses[i, :3, :3] = rotation
            poses[i, :3, 3] = centre
            poses[i, 3, 3] = 1.0

            camera = (points - centre) @ rotation
            in_front = camera[:, 2] > 1e-6
            cam = camera[in_front]
            u = (cam[:, 0] * focal / cam[:, 2] + s / 2).astype(int)
            v = (cam[:, 1] * focal / cam[:, 2] + s / 2).astype(int)
            keep = (u >= 0) & (u < s) & (v >= 0) & (v < s)
            u, v, z = u[keep], v[keep], cam[keep][:, 2]
            # Nearest surface wins, which is what a depth map is.
            buffer = np.full((s, s), np.inf)
            np.minimum.at(buffer, (v, u), z)
            buffer[~np.isfinite(buffer)] = 0.0
            depth[i] = buffer.astype(np.float32)

        return Reconstruction(
            poses=poses,
            intrinsics=np.repeat(K[None], n, axis=0),
            depth=depth,
            confidence=confidence,
            backend=self.name,
            metric_scale=1000.0,
        )


@pytest.fixture
def patched_frames(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Stand in for S1-S2, which need a real video file.

    S1 and S2 are covered by their own tests and by an integration test
    against real ffmpeg; what this file is about is everything after them.
    """

    def fake_probe(path: Path) -> Any:
        from roomfittr_pipeline.ingest import VideoMeta

        return VideoMeta(
            duration_s=40.0,
            coded_width=1920,
            coded_height=1440,
            rotation_deg=0,
            fps=60.0,
            codec="hevc",
            format_name="mov,mp4",
            size_bytes=1000,
            has_audio=False,
        )

    def fake_run(video: Path, meta: Any, work_dir: Path, target: int = 80, **kw: Any) -> Any:
        model_dir = work_dir / "keyframes" / "model"
        model_dir.mkdir(parents=True, exist_ok=True)
        for i in range(8):
            (model_dir / f"{i:04d}.jpg").write_bytes(b"")
        return None

    monkeypatch.setattr(ingest, "probe", fake_probe)
    monkeypatch.setattr(ingest, "validate", lambda m: m)
    monkeypatch.setattr(frames, "run", fake_run)


def test_a_known_box_comes_back_with_the_right_dimensions(
    patched_frames: None, tmp_path: Path
) -> None:
    result = pipeline.run(
        tmp_path / "fake.mov",
        tmp_path / "work",
        reconstruct=SyntheticBackend(),
        gravity_hint=GRAVITY,
        pipeline_version="0.1.0-test",
    )

    model = result.room_model
    assert model["pipeline_version"] == "0.1.0-test"

    # The ceiling is the cleanest single check that scale survived the whole
    # chain: it is measured in S6 and multiplied in `roommodel.build`.
    assert model["ceiling_height_mm"] == pytest.approx(ROOM_Y_MM, rel=0.08)

    # Floor spans, within what a voxelised evidence grid plus S7's ceiling
    # prior allow. The prior is 2590 mm against this room's 2600, so the
    # recovered scale carries a few percent of that mismatch into every
    # dimension -- which is E2's whole question, measured here on a room
    # whose answer is known.
    polygon = np.array(model["floor_polygon"], dtype=float)
    x_span = polygon[:, 0].max() - polygon[:, 0].min()
    z_span = polygon[:, 1].max() - polygon[:, 1].min()
    spans = sorted([x_span, z_span])
    assert spans[0] == pytest.approx(ROOM_Z_MM, rel=0.12)
    assert spans[1] == pytest.approx(ROOM_X_MM, rel=0.12)


def test_the_result_is_a_schema_valid_room_model(patched_frames: None, tmp_path: Path) -> None:
    # A RoomModel that scores well but does not validate is not a deliverable:
    # 2.4 makes the schema the single definition, and the eval harness reads
    # exactly this document.
    import json

    from jsonschema import Draft7Validator
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT7

    # 2.4 makes `packages/schemas/src/*.schema.json` the single definition.
    # Resolved from this file rather than the working directory so the test
    # does not depend on where pytest was invoked from.
    repo_root = Path(__file__).resolve().parents[3]
    schema_dir = repo_root / "packages" / "schemas" / "src"
    schema_path = schema_dir / "room-model.schema.json"
    if not schema_path.exists():
        pytest.skip(f"schema not present at {schema_path}")

    result = pipeline.run(
        tmp_path / "fake.mov",
        tmp_path / "work",
        reconstruct=SyntheticBackend(),
        gravity_hint=GRAVITY,
        pipeline_version="0.1.0-test",
    )
    # Same registry the schema package's own suite builds: the schemas refer
    # to each other by both bare filename and canonical $id, and the $id is an
    # https URI that must never actually be fetched.
    resources = []
    for path in schema_dir.glob("*.schema.json"):
        doc = json.loads(path.read_text(encoding="utf-8"))
        resource = Resource.from_contents(doc, default_specification=DRAFT7)
        resources.append((path.name, resource))
        resources.append((doc["$id"], resource))
    validator = Draft7Validator(
        json.loads(schema_path.read_text(encoding="utf-8")),
        registry=Registry().with_resources(resources),
    )
    errors = sorted(validator.iter_errors(result.room_model), key=lambda e: list(e.path))
    assert not errors, "; ".join(f"{list(e.path)}: {e.message}" for e in errors)


def test_running_without_segmentation_says_so(patched_frames: None, tmp_path: Path) -> None:
    # The mode is legitimate but not gate-worthy, and the warning is how a
    # reader of the run knows which it was.
    result = pipeline.run(
        tmp_path / "fake.mov",
        tmp_path / "work",
        reconstruct=SyntheticBackend(),
        segment=None,
        gravity_hint=GRAVITY,
        pipeline_version="0.1.0-test",
    )
    assert any("segmentation skipped" in w for w in result.warnings)


def test_a_collapsed_reconstruction_fails_in_s3_not_later(tmp_path: Path) -> None:
    # 3.4's failure detection exists so the error names the real cause. If
    # this leaked through, the symptom would be a strange floor plan.
    class Collinear(SyntheticBackend):
        def reconstruct(self, frame_paths: list[Path]) -> Reconstruction:
            base = super().reconstruct(frame_paths)
            poses = base.poses.copy()
            poses[:, :3, 3] = np.linspace(0, 1, len(poses))[:, None] * np.array([1.0, 0.0, 0.0])
            return Reconstruction(
                poses=poses,
                intrinsics=base.intrinsics,
                depth=base.depth,
                confidence=base.confidence,
                backend="collinear",
            )

    from roomfittr_pipeline.errors import PipelineError

    with pytest.raises(PipelineError) as exc:
        backends.validate(Collinear().reconstruct([]))
    assert "straight line" in str(exc.value)
