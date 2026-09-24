"""The parts of the VGGT adapter that do not need a GPU.

Loading the model needs a gated 5 GB checkpoint and a CUDA device, so the
inference path is exercised on Modal, not here. What *is* testable on a laptop
is the conversion either side of it, and that is where the dangerous bugs
live: a world-to-camera pose used as camera-to-world produces a reconstruction
that passes `backends.validate` cleanly and puts every camera in the wrong
place, which no structural check downstream would catch.
"""

from __future__ import annotations

import numpy as np
import pytest
from roomfittr_pipeline.adapters.vggt import _normalise_confidence, _to_camera_to_world


def _rotation_z(radians: float) -> np.ndarray:
    c, s = np.cos(radians), np.sin(radians)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def test_inverting_world_to_camera_recovers_the_camera_centre() -> None:
    # A camera at (2, 0, 5) looking along a rotated axis. VGGT reports the
    # world-to-camera transform; the centre it implies is what the contract
    # calls the pose translation.
    rotation = _rotation_z(0.7)
    centre = np.array([2.0, 0.0, 5.0])
    extrinsic = np.zeros((1, 3, 4))
    extrinsic[0, :3, :3] = rotation
    extrinsic[0, :3, 3] = -rotation @ centre

    poses = _to_camera_to_world(extrinsic)

    assert poses.shape == (1, 4, 4)
    np.testing.assert_allclose(poses[0, :3, 3], centre, atol=1e-12)
    np.testing.assert_allclose(poses[0, :3, :3], rotation.T, atol=1e-12)
    np.testing.assert_allclose(poses[0, 3], [0.0, 0.0, 0.0, 1.0])


def test_inversion_round_trips_to_the_identity() -> None:
    rng = np.random.default_rng(0)
    extrinsic = np.zeros((4, 3, 4))
    for i in range(4):
        # An orthonormal rotation via QR, which is what a real pose is.
        q, _ = np.linalg.qr(rng.normal(size=(3, 3)))
        if np.linalg.det(q) < 0:
            q[:, 0] *= -1
        extrinsic[i, :3, :3] = q
        extrinsic[i, :3, 3] = rng.normal(size=3)

    poses = _to_camera_to_world(extrinsic)

    for i in range(4):
        world_to_camera = np.eye(4)
        world_to_camera[:3, :4] = extrinsic[i]
        np.testing.assert_allclose(world_to_camera @ poses[i], np.eye(4), atol=1e-10)


def test_confidence_is_mapped_into_the_unit_range() -> None:
    # VGGT's depth_conf is an unbounded positive score, not a probability.
    raw = np.array([[[0.0, 1.0, 5.0, 40.0]]], dtype=np.float32)
    out = _normalise_confidence(raw)

    assert out.dtype == np.float32
    assert out.min() >= 0.0
    assert out.max() <= 1.0


def test_a_single_speculative_pixel_does_not_flatten_the_map() -> None:
    # Scaling by the max would push the ordinary values to ~0.001 and make
    # every confidence threshold downstream meaningless. The 99th percentile
    # is why this holds.
    ordinary = np.full(1000, 4.0, dtype=np.float32)
    raw = np.concatenate([ordinary, np.array([4000.0], dtype=np.float32)]).reshape(1, 1, -1)

    out = _normalise_confidence(raw)

    assert out[0, 0, 0] > 0.9


@pytest.mark.parametrize("values", [np.array([[[np.nan, np.nan]]]), np.array([[[0.0, 0.0]]])])
def test_degenerate_confidence_becomes_zero_rather_than_nan(values: np.ndarray) -> None:
    out = _normalise_confidence(values.astype(np.float32))
    assert np.isfinite(out).all()
    assert float(out.max()) == 0.0
