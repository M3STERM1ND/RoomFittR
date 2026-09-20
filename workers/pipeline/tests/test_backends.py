"""The S3/S4 adapter contract.

No model is loaded here -- the real backends need GPU weights this machine
does not have. What is testable, and worth testing before any of them lands,
is the contract they must satisfy: the failure detection of 3.4, the
back-projection every candidate feeds into S5, and the prompt vocabulary that
decides what counts as removable.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray
from roomfittr_pipeline import backends
from roomfittr_pipeline.backends import (
    InstanceMask,
    Reconstruction,
    ReconstructionBackend,
    SegmentationBackend,
)
from roomfittr_pipeline.errors import PipelineError


def look_at(
    eye: tuple[float, float, float], target: tuple[float, float, float]
) -> NDArray[np.float64]:
    """A camera-to-world matrix for a camera at `eye` looking at `target`.

    OpenCV convention: +x right, +y down, +z forward.
    """
    forward = np.array(target, dtype=np.float64) - np.array(eye, dtype=np.float64)
    forward /= np.linalg.norm(forward)
    world_down = np.array([0.0, -1.0, 0.0])
    right = np.cross(world_down, forward)
    right /= np.linalg.norm(right)
    down = np.cross(forward, right)

    pose = np.eye(4, dtype=np.float64)
    pose[:3, 0], pose[:3, 1], pose[:3, 2] = right, down, forward
    pose[:3, 3] = eye
    return pose


def walk_a_room(n: int = 8, radius: float = 1500.0, height: float = 1400.0) -> Reconstruction:
    """A reconstruction of someone walking a circle, facing outwards.

    The capture shape the plan's coach screen asks for (3.2: "walk slowly
    along the room's perimeter, facing inward"), which is also the shape that
    gives the widest baseline.
    """
    poses = []
    for i in range(n):
        angle = 2 * math.pi * i / n
        eye = (radius * math.cos(angle), height, radius * math.sin(angle))
        target = (2 * radius * math.cos(angle), height, 2 * radius * math.sin(angle))
        poses.append(look_at(eye, target))

    shape = (n, 24, 32)
    intrinsics = np.tile(
        np.array([[400.0, 0.0, 16.0], [0.0, 400.0, 12.0], [0.0, 0.0, 1.0]]), (n, 1, 1)
    )
    return Reconstruction(
        poses=np.stack(poses),
        intrinsics=intrinsics,
        depth=np.full(shape, 2000.0, dtype=np.float32),
        confidence=np.full(shape, 0.9, dtype=np.float32),
        backend="synthetic",
    )


class TestValidation:
    def test_a_normal_walk_passes(self) -> None:
        backends.validate(walk_a_room())

    def test_a_single_frame_is_not_a_reconstruction(self) -> None:
        one = walk_a_room(n=8)
        with pytest.raises(PipelineError) as exc:
            backends.validate(
                Reconstruction(
                    poses=one.poses[:1],
                    intrinsics=one.intrinsics[:1],
                    depth=one.depth[:1],
                    confidence=one.confidence[:1],
                    backend="synthetic",
                )
            )
        assert exc.value.code == "RECON_FAILED"

    def test_cameras_all_in_one_place_are_rejected(self) -> None:
        """A model that collapsed. Without this the room comes out as
        whatever the depth maps happened to say, with no triangulation."""
        stuck = walk_a_room()
        poses = np.tile(stuck.poses[0], (stuck.frame_count, 1, 1))
        with pytest.raises(PipelineError):
            backends.validate(
                Reconstruction(
                    poses=poses,
                    intrinsics=stuck.intrinsics,
                    depth=stuck.depth,
                    confidence=stuck.confidence,
                    backend="synthetic",
                )
            )

    def test_a_straight_line_walk_is_rejected(self) -> None:
        """3.4: "poses that collapse into a line". Someone who walked down a
        hallway without turning has no baseline across it, so depth in that
        direction is a guess -- and the room still comes out looking like a
        room, which is why this is checked rather than left to S6."""
        line = walk_a_room()
        poses = line.poses.copy()
        for index in range(len(poses)):
            poses[index][:3, 3] = [index * 300.0, 1400.0, 0.0]
        with pytest.raises(PipelineError) as exc:
            backends.validate(
                Reconstruction(
                    poses=poses,
                    intrinsics=line.intrinsics,
                    depth=line.depth,
                    confidence=line.confidence,
                    backend="synthetic",
                )
            )
        assert "straight line" in exc.value.detail

    def test_nan_poses_are_rejected(self) -> None:
        broken = walk_a_room()
        poses = broken.poses.copy()
        poses[2, 0, 3] = np.nan
        with pytest.raises(PipelineError):
            backends.validate(
                Reconstruction(
                    poses=poses,
                    intrinsics=broken.intrinsics,
                    depth=broken.depth,
                    confidence=broken.confidence,
                    backend="synthetic",
                )
            )

    def test_mismatched_depth_and_pose_counts_are_rejected(self) -> None:
        bad = walk_a_room()
        with pytest.raises(PipelineError):
            backends.validate(
                Reconstruction(
                    poses=bad.poses,
                    intrinsics=bad.intrinsics,
                    depth=bad.depth[:3],
                    confidence=bad.confidence[:3],
                    backend="synthetic",
                )
            )


class TestBackprojection:
    def test_points_land_at_the_expected_distance(self) -> None:
        """A camera at the origin looking down +z with depth 2000 must put its
        points 2000 away, or every length downstream is wrong."""
        recon = walk_a_room(n=2)
        points = backends.backproject(recon, 0)
        assert len(points) > 0
        centre = recon.camera_centres()[0]
        distances = np.linalg.norm(points - centre, axis=1)
        # The principal ray is exactly 2000; edge pixels are slightly further.
        assert distances.min() == pytest.approx(2000.0, rel=0.01)
        assert distances.max() < 2100.0

    def test_a_mask_restricts_the_lift(self) -> None:
        """S5 step 1 accumulates points per track this way."""
        recon = walk_a_room(n=2)
        mask = np.zeros(recon.depth.shape[1:], dtype=bool)
        mask[:4, :4] = True
        assert len(backends.backproject(recon, 0, pixel_mask=mask)) == 16

    def test_invalid_depth_is_skipped(self) -> None:
        recon = walk_a_room(n=2)
        depth = recon.depth.copy()
        depth[0, :12, :] = 0.0
        depth[0, 12:16, :] = np.nan
        sparse = Reconstruction(
            poses=recon.poses,
            intrinsics=recon.intrinsics,
            depth=depth,
            confidence=recon.confidence,
            backend="synthetic",
        )
        expected = (24 - 16) * 32
        assert len(backends.backproject(sparse, 0)) == expected

    def test_the_confidence_floor_drops_the_worst_thirty_percent(self) -> None:
        """3.4 step 4. The threshold is relative because absolute confidence
        is not comparable between models -- which is the whole point of the
        bake-off."""
        recon = walk_a_room(n=2)
        confidence = np.linspace(0.0, 1.0, recon.confidence.size, dtype=np.float32)
        graded = Reconstruction(
            poses=recon.poses,
            intrinsics=recon.intrinsics,
            depth=recon.depth,
            confidence=confidence.reshape(recon.confidence.shape),
            backend="synthetic",
        )
        floor = backends.confidence_floor(graded)
        assert floor == pytest.approx(0.3, abs=0.02)

        kept = backends.backproject(graded, 0, confidence_floor=floor)
        total = graded.depth[0].size
        assert len(kept) < total

    def test_a_mask_of_the_wrong_shape_is_a_programming_error(self) -> None:
        recon = walk_a_room(n=2)
        with pytest.raises(ValueError):
            backends.backproject(recon, 0, pixel_mask=np.zeros((4, 4), dtype=bool))


class TestPrompts:
    def test_the_three_groups_of_the_plan_are_present(self) -> None:
        assert set(backends.PROMPTS) == {"removable", "structure", "fixed"}

    def test_a_radiator_is_fixed_and_a_sofa_is_not(self) -> None:
        """The distinction the whole removability policy rests on, stated
        once here rather than re-decided downstream."""
        assert "radiator" in backends.PROMPTS["fixed"]
        assert "sofa" in backends.PROMPTS["removable"]

    def test_no_prompt_appears_in_two_groups(self) -> None:
        """An overlap would make removability depend on iteration order."""
        seen: set[str] = set()
        for prompts in backends.PROMPTS.values():
            overlap = seen & set(prompts)
            assert not overlap, f"{overlap} appears in more than one group"
            seen |= set(prompts)

    def test_mirrors_are_prompted_for_separately(self) -> None:
        """3.11: mirrors create phantom geometry. They are not furniture and
        not structure -- they are points to throw away."""
        assert "mirror" in backends.EXCLUSION_PROMPTS
        assert not any("mirror" in group for group in backends.PROMPTS.values())


class TestProtocols:
    def test_a_minimal_backend_satisfies_the_protocol(self) -> None:
        """The contract is structural, so a real adapter only has to match the
        shape -- no base class to inherit and no import of ours to add."""

        class Fake:
            name = "fake"

            def reconstruct(self, frame_paths: list[Path]) -> Reconstruction:
                return walk_a_room()

        assert isinstance(Fake(), ReconstructionBackend)

    def test_a_segmentation_backend_satisfies_its_protocol(self) -> None:
        class FakeSegmenter:
            name = "fake"

            def segment(
                self, frame_paths: list[Path], prompts: dict[str, list[str]]
            ) -> list[InstanceMask]:
                return []

        assert isinstance(FakeSegmenter(), SegmentationBackend)
