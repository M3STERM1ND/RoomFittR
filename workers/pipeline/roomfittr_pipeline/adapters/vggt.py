"""Candidate B: VGGT-1B, the commercial checkpoint (3.4).

`facebook/VGGT-1B-Commercial` rather than `facebook/VGGT-1B`: the plain
checkpoint is CC BY-NC and `docs/model-licenses.md` rules it out of anything
that ships. Both are gated on Hugging Face, so the worker image needs
`HF_TOKEN` from the `roomfittr-hf` Modal secret.

VGGT predicts geometry **up to scale**, so `metric_scale` stays None and S7
decides what the units mean (3.4's scale column, and the reason `Reconstruction`
carries no millimetres).

Two things about the conversion are easy to get wrong and are handled here
rather than left to the caller:

- VGGT returns **world-to-camera** extrinsics; `Reconstruction.poses` is
  **camera-to-world**. These are inverses, and confusing them produces a
  reconstruction that validates cleanly and puts every camera in the wrong
  place.
- Its preprocessing resizes frames, so the intrinsics it returns belong to
  the resized image. Depth maps come back at that same size, so depth,
  confidence and intrinsics stay mutually consistent and no rescaling is
  needed -- but the frames on disk are *not* the frames these intrinsics
  describe, which matters when S4's masks are lifted through them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..backends import Reconstruction
from ..errors import PipelineError, Stage

CHECKPOINT = "facebook/VGGT-1B-Commercial"

# 3.4 step 2: inference in bfloat16. Ampere and later support it natively;
# older cards fall back to fp16, which VGGT's own examples also do.
_BF16_MIN_CAPABILITY = 8


@dataclass(slots=True)
class VGGTBackend:
    """S3 via VGGT-1B."""

    name: str = "vggt-1b-commercial"
    checkpoint: str = CHECKPOINT
    device: str = "cuda"
    _model: Any = field(default=None, repr=False)

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from vggt.models.vggt import VGGT
        except ImportError as exc:  # pragma: no cover - GPU image only
            raise PipelineError(
                "INTERNAL",
                Stage.RECONSTRUCT,
                f"VGGT is not installed in this image: {exc}",
            ) from exc

        model = VGGT.from_pretrained(self.checkpoint)
        model = model.to(self.device)
        model.eval()
        self._model = model
        return model

    def _dtype(self) -> Any:
        import torch

        if self.device.startswith("cuda") and torch.cuda.is_available():
            major = torch.cuda.get_device_capability()[0]
            return torch.bfloat16 if major >= _BF16_MIN_CAPABILITY else torch.float16
        return torch.float32

    def reconstruct(self, frame_paths: list[Path]) -> Reconstruction:
        import torch
        from vggt.utils.load_fn import load_and_preprocess_images
        from vggt.utils.pose_enc import pose_encoding_to_extri_intri

        if len(frame_paths) < 2:
            raise PipelineError(
                "RECON_FAILED",
                Stage.RECONSTRUCT,
                f"{len(frame_paths)} frames is not enough to reconstruct from",
            )

        model = self._load()
        images = load_and_preprocess_images([str(p) for p in frame_paths]).to(self.device)

        with torch.no_grad(), torch.autocast(device_type="cuda", dtype=self._dtype()):
            # The batch dimension is the sequence: VGGT attends across all
            # frames at once, which is where its memory growth comes from
            # and what E7 profiles.
            predictions = model(images[None])

        extrinsic, intrinsic = pose_encoding_to_extri_intri(
            predictions["pose_enc"], images.shape[-2:]
        )

        # (1, N, ...) -> (N, ...), and out of bf16 before any maths.
        extrinsic_np = extrinsic[0].float().cpu().numpy().astype(np.float64)
        intrinsic_np = intrinsic[0].float().cpu().numpy().astype(np.float64)
        depth = predictions["depth"][0].float().cpu().numpy().astype(np.float32)
        confidence = predictions["depth_conf"][0].float().cpu().numpy().astype(np.float32)

        # VGGT emits depth as (N, H, W, 1); the contract is (N, H, W).
        depth = np.squeeze(depth, axis=-1) if depth.ndim == 4 else depth
        confidence = np.squeeze(confidence, axis=-1) if confidence.ndim == 4 else confidence

        poses = _to_camera_to_world(extrinsic_np)
        return Reconstruction(
            poses=poses,
            intrinsics=intrinsic_np,
            depth=depth,
            confidence=_normalise_confidence(confidence),
            backend=self.name,
            metric_scale=None,
        )


def _to_camera_to_world(extrinsic: np.ndarray) -> np.ndarray:
    """Invert VGGT's (N, 3, 4) world-to-camera into (N, 4, 4) camera-to-world.

    Inverted by transposing the rotation rather than with a general inverse:
    the rotation is orthonormal, so the transpose is exact and cannot pick up
    the conditioning problems a 4x4 solve can when a pose is near-degenerate.
    """
    count = extrinsic.shape[0]
    rotation = extrinsic[:, :3, :3]
    translation = extrinsic[:, :3, 3]

    poses = np.zeros((count, 4, 4), dtype=np.float64)
    r_t = np.transpose(rotation, (0, 2, 1))
    poses[:, :3, :3] = r_t
    poses[:, :3, 3] = -np.einsum("nij,nj->ni", r_t, translation)
    poses[:, 3, 3] = 1.0
    return poses


def _normalise_confidence(confidence: np.ndarray) -> np.ndarray:
    """Map VGGT's unbounded confidence onto the 0-1 the contract promises.

    VGGT's `depth_conf` is a positive score with no upper bound, not a
    probability. `backends.confidence_floor` takes a percentile so it would
    work either way, but anything reading `confidence` as 0-1 -- the schema,
    the debug viewer -- would be quietly wrong. Scaled by the 99th percentile
    rather than the max so one speculative pixel cannot compress the rest of
    the map into nothing.
    """
    finite = confidence[np.isfinite(confidence)]
    if finite.size == 0:
        return np.zeros_like(confidence, dtype=np.float32)
    high = float(np.percentile(finite, 99.0))
    if high <= 0:
        return np.zeros_like(confidence, dtype=np.float32)
    return np.clip(confidence / high, 0.0, 1.0).astype(np.float32)
