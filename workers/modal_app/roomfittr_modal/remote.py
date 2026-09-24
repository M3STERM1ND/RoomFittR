"""Backends that satisfy the S3/S4 protocols by calling deployed Modal functions.

`pipeline.run` takes backends as arguments rather than importing them (3.4's
adapter boundary). These are the implementations that put the GPU stages on
Modal while S1, S2 and S5-S9 stay on the caller's machine -- which is the
split 3.9 describes and the one E7 profiles.

They are deliberately *not* in `roomfittr_pipeline`: that package imports no
Modal, so the pipeline keeps running locally, in CI, and on any GPU host.

**Nothing here loads a model or does geometry.** It moves bytes to a function
and rebuilds the dataclass from what comes back, so a bug in this file shows
up as a transport error rather than as a quietly wrong room.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import modal
import numpy as np
from roomfittr_pipeline.backends import InstanceMask, Reconstruction

APP_NAME = "roomfittr"


@dataclass(slots=True)
class RemoteReconstruction:
    """S3 on a Modal GPU.

    `function_name` picks the card: `reconstruct_vggt_l40s` and
    `reconstruct_vggt` are the same model on different GPUs, which is what E7
    compares. It is a plain string so the bake-off can sweep them without
    importing anything from the app.
    """

    function_name: str = "reconstruct_vggt_l40s"
    environment: str = "dev"
    name: str = "remote"
    # Filled from the reply, so E7's numbers come from the container that did
    # the work rather than from a wall-clock guess on this side.
    last_elapsed_s: float | None = field(default=None, init=False)
    last_peak_vram_bytes: int | None = field(default=None, init=False)

    def reconstruct(self, frame_paths: list[Path]) -> Reconstruction:
        fn = modal.Function.from_name(
            APP_NAME, self.function_name, environment_name=self.environment
        )
        payload = [p.read_bytes() for p in frame_paths]
        names = [p.name for p in frame_paths]
        result: dict[str, Any] = fn.remote(payload, names)

        self.last_elapsed_s = float(result["elapsed_s"])
        peak = result.get("peak_vram_bytes")
        self.last_peak_vram_bytes = int(peak) if peak else None

        return Reconstruction(
            poses=np.asarray(result["poses"], dtype=np.float64),
            intrinsics=np.asarray(result["intrinsics"], dtype=np.float64),
            depth=np.asarray(result["depth"], dtype=np.float32),
            confidence=np.asarray(result["confidence"], dtype=np.float32),
            backend=str(result["backend"]),
            metric_scale=result["metric_scale"],
        )


@dataclass(slots=True)
class RemoteSegmentation:
    """S4 on a Modal GPU.

    Masks come back bit-packed: they are boolean, mostly empty, and one
    instance over 80 frames at 392x518 is 16 MB unpacked against 2 MB packed.
    `np.packbits` flattens, so the shape travels alongside and is restored
    here rather than inferred.
    """

    function_name: str = "segment_sam3"
    environment: str = "dev"
    name: str = "remote-sam3"

    def segment(self, frame_paths: list[Path], prompts: dict[str, list[str]]) -> list[InstanceMask]:
        fn = modal.Function.from_name(
            APP_NAME, self.function_name, environment_name=self.environment
        )
        payload = [p.read_bytes() for p in frame_paths]
        names = [p.name for p in frame_paths]
        raw: list[dict[str, Any]] = fn.remote(payload, names)

        instances: list[InstanceMask] = []
        for item in raw:
            shape = tuple(int(x) for x in item["masks_shape"])
            count = int(np.prod(shape))
            bits = np.unpackbits(np.frombuffer(item["masks_packed"], dtype=np.uint8))
            masks = bits[:count].astype(bool).reshape(shape)
            instances.append(
                InstanceMask(
                    track_id=int(item["track_id"]),
                    label=str(item["label"]),
                    label_group=str(item["label_group"]),
                    score=float(item["score"]),
                    masks=masks,
                )
            )
        return instances
