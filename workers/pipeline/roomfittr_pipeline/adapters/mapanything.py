"""Candidate A: MapAnything (3.4).

The Apache-2.0 checkpoint, which `docs/model-licenses.md` clears for shipping
and which -- unlike VGGT's -- is not gated.

What makes it candidate A is that it predicts **metric** pointmaps, so one
model gives geometry *and* scale. That does not mean it bypasses S7. The
contract in `backends.py` is explicit: a backend that predicts metric depth
reports it as `metric_scale` rather than silently emitting millimetres, and
S7 fuses it with MoGe-2, the door prior and any user measurement as one
`ScaleSource` among several. E2 is the experiment that decides whether this
model's own scale is good enough to lean on, and hard-coding trust in it here
would answer that question by assumption instead of by measurement.

3.4's concern for this candidate is memory at N around 100, which is exactly
what E7 profiles; `memory_efficient` exists so that sweep can be run without
editing this file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..backends import Reconstruction
from ..errors import PipelineError, Stage

CHECKPOINT = "facebook/map-anything"


@dataclass(slots=True)
class MapAnythingBackend:
    """S3 via MapAnything."""

    name: str = "mapanything"
    checkpoint: str = CHECKPOINT
    device: str = "cuda"
    # 3.4: trades throughput for peak VRAM. E7 sweeps both settings.
    memory_efficient: bool = False
    _model: Any = field(default=None, repr=False)

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from mapanything.models import MapAnything
        except ImportError as exc:  # pragma: no cover - GPU image only
            raise PipelineError(
                "INTERNAL",
                Stage.RECONSTRUCT,
                f"MapAnything is not installed in this image: {exc}",
            ) from exc

        model = MapAnything.from_pretrained(self.checkpoint).to(self.device)
        model.eval()
        self._model = model
        return model

    def reconstruct(self, frame_paths: list[Path]) -> Reconstruction:
        import torch
        from mapanything.utils.image import load_images

        if len(frame_paths) < 2:
            raise PipelineError(
                "RECON_FAILED",
                Stage.RECONSTRUCT,
                f"{len(frame_paths)} frames is not enough to reconstruct from",
            )

        model = self._load()
        views = load_images([str(p) for p in frame_paths])

        with torch.no_grad():
            predictions = model.infer(views, memory_efficient_inference=self.memory_efficient)

        poses = _stack(predictions, "camera_poses", np.float64)
        intrinsics = _stack(predictions, "intrinsics", np.float64)
        depth = _stack(predictions, "depth_z", np.float32)
        confidence = _stack(predictions, "mask", np.float32)

        if depth.ndim == 4:
            depth = np.squeeze(depth, axis=-1)
        if confidence.ndim == 4:
            confidence = np.squeeze(confidence, axis=-1)

        return Reconstruction(
            poses=_to_4x4(poses),
            intrinsics=intrinsics,
            depth=depth,
            # MapAnything's `mask` is a validity flag, not a graded score. It
            # is reported as 0 or 1 rather than dressed up as a confidence,
            # so `confidence_floor`'s percentile does the only thing it
            # honestly can here: keep the valid pixels.
            confidence=np.clip(confidence, 0.0, 1.0).astype(np.float32),
            backend=self.name,
            # The model is metric, so its unit *is* the metre. S7 still
            # decides; this is one vote (see the module docstring).
            metric_scale=1000.0,
        )


def _stack(predictions: Any, key: str, dtype: type) -> np.ndarray:
    """Pull one field out of MapAnything's per-view dicts into one array.

    Accepts either a list of per-view dicts or an already-batched dict, since
    the two shapes differ between releases and a silent shape change here
    would surface as a geometry bug several stages later.
    """
    import torch

    def to_numpy(value: Any) -> np.ndarray:
        if isinstance(value, torch.Tensor):
            # torch is untyped here (it lives only in the GPU image), so the
            # result is Any until it is asserted back into an ndarray.
            return np.asarray(value.detach().float().cpu().numpy())
        return np.asarray(value)

    if isinstance(predictions, dict):
        if key not in predictions:
            raise PipelineError(
                "RECON_FAILED", Stage.RECONSTRUCT, f"MapAnything returned no {key!r}"
            )
        array = to_numpy(predictions[key])
        if array.ndim >= 1 and array.shape[0] == 1:
            array = array[0]
        return np.asarray(array, dtype=dtype)

    missing = [i for i, view in enumerate(predictions) if key not in view]
    if missing:
        raise PipelineError(
            "RECON_FAILED",
            Stage.RECONSTRUCT,
            f"MapAnything returned no {key!r} for view(s) {missing[:5]}",
        )
    stacked = np.stack([np.squeeze(to_numpy(view[key])) for view in predictions], axis=0)
    return np.asarray(stacked, dtype=dtype)


def _to_4x4(poses: np.ndarray) -> np.ndarray:
    """Pad (N, 3, 4) to (N, 4, 4) if needed; MapAnything's poses are already
    camera-to-world, unlike VGGT's."""
    if poses.ndim != 3:
        raise PipelineError(
            "RECON_FAILED", Stage.RECONSTRUCT, f"poses have unexpected shape {poses.shape}"
        )
    if poses.shape[1:] == (4, 4):
        return poses.astype(np.float64)
    if poses.shape[1:] == (3, 4):
        out = np.zeros((poses.shape[0], 4, 4), dtype=np.float64)
        out[:, :3, :4] = poses
        out[:, 3, 3] = 1.0
        return out
    raise PipelineError(
        "RECON_FAILED", Stage.RECONSTRUCT, f"poses have unexpected shape {poses.shape}"
    )
