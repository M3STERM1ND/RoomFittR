"""Modal functions wrapping the pipeline (implementation-plan.md 2.1, 3.9).

Thin by design, per 2.4's repo layout: every stage's logic lives in
`roomfittr_pipeline`, which imports no Modal at all, so the whole pipeline can
be run and debugged on a laptop or in CI. What is here is the part that is
genuinely about Modal -- which GPU, which image, which secret, which timeout,
and how a scan is split into functions.

**Why S3 and S4 are separate functions.** 3.9 budgets VRAM per stage, and E7
profiles them separately at N = 40/80/100/150. Running reconstruction and
segmentation in one container would make those numbers uninterpretable, and it
would hold an A100 while SAM 3 runs, which is not what the A100 is for.

Timeouts come from `SCAN_JOB_TIMEOUT_S` rather than a constant, because 7.4
wants a hung job to die inside Modal rather than be noticed by a human, and
1.3 wants that bound tightenable without a deploy.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import Any

import modal

from .images import (
    ARTEFACTS,
    ARTEFACTS_PATH,
    WEIGHTS,
    WEIGHTS_PATH,
    cpu_image,
    mapanything_image,
    sam3_image,
    vggt_image,
)

app = modal.App("roomfittr")

HF_SECRET = modal.Secret.from_name("roomfittr-hf")
# Optional until R2 exists; see `_secrets` for why its absence is decided
# from the environment rather than caught.
_R2_SECRET_NAME = "roomfittr-r2"

# Keys are typed as `str | PurePosixPath` by Modal; annotating the dict
# keeps that widening explicit rather than inferring the narrower str.
# `dict` is invariant, so the value type has to be spelled the way Modal
# spells it rather than narrowed to Volume.
_VOLUMES: dict[str | PurePosixPath, modal.Volume | modal.CloudBucketMount] = {
    PurePosixPath(WEIGHTS_PATH): WEIGHTS,
    PurePosixPath(ARTEFACTS_PATH): ARTEFACTS,
}


def _timeout() -> int:
    """7.4's hard per-job bound, enforced by Modal itself."""
    return int(os.environ.get("SCAN_JOB_TIMEOUT_S", "1800"))


def _secrets() -> list[modal.Secret]:
    """HF always; R2 only once it exists.

    `Secret.from_name` is lazy: it does not check anything at construction and
    fails at *deploy* time if the secret is absent, so a try/except around it
    catches nothing. The gate is therefore the deploying environment's own R2
    credentials, which the runbook creates in the same step as the Modal
    secret -- so "R2 is configured here" and "roomfittr-r2 exists there" are
    the same fact. Before that, the pipeline's storage layer falls back to a
    local store, and a GPU run that cannot upload is still a run worth having.
    """
    secrets = [HF_SECRET]
    if os.environ.get("R2_ACCESS_KEY_ID"):
        secrets.append(modal.Secret.from_name(_R2_SECRET_NAME))
    return secrets


@app.function(
    image=vggt_image,
    gpu="A100-80GB",
    volumes=_VOLUMES,
    secrets=_secrets(),
    timeout=_timeout(),
)
def reconstruct_vggt(frames: list[bytes], names: list[str]) -> dict[str, Any]:
    """S3, candidate B. Frames in as bytes, `Reconstruction` fields out.

    Frames cross the boundary as bytes rather than as a volume path so this
    function can be called from anywhere -- the eval harness on a laptop, the
    API, a notebook -- without first staging files where Modal can see them.
    """
    return _run_reconstruction("vggt", frames, names)


@app.function(
    image=mapanything_image,
    gpu="A100-80GB",
    volumes=_VOLUMES,
    secrets=_secrets(),
    timeout=_timeout(),
)
def reconstruct_mapanything(frames: list[bytes], names: list[str]) -> dict[str, Any]:
    """S3, candidate A."""
    return _run_reconstruction("mapanything", frames, names)


@app.function(
    image=vggt_image,
    gpu="L40S",
    volumes=_VOLUMES,
    secrets=_secrets(),
    timeout=_timeout(),
)
def reconstruct_vggt_l40s(frames: list[bytes], names: list[str]) -> dict[str, Any]:
    """S3, candidate B, on the cheaper card. E7 profiles both (3.9)."""
    return _run_reconstruction("vggt", frames, names)


@app.function(
    image=sam3_image,
    gpu="L40S",
    volumes=_VOLUMES,
    secrets=_secrets(),
    timeout=_timeout(),
)
def segment_sam3(frames: list[bytes], names: list[str]) -> list[dict[str, Any]]:
    """S4. Returns instances as plain dicts; masks are packed, see below."""
    import numpy as np
    from roomfittr_pipeline.adapters import resolve_segmentation
    from roomfittr_pipeline.backends import PROMPTS

    paths = _materialise(frames, names)
    backend = resolve_segmentation("sam3")
    instances = backend.segment(paths, PROMPTS)

    out: list[dict[str, Any]] = []
    for instance in instances:
        # Masks are boolean and mostly empty. `packbits` is ~8x smaller over
        # the wire, and the shape is carried alongside so the caller can
        # unpack without guessing.
        out.append(
            {
                "track_id": instance.track_id,
                "label": instance.label,
                "label_group": instance.label_group,
                "score": instance.score,
                "masks_packed": np.packbits(instance.masks).tobytes(),
                "masks_shape": list(instance.masks.shape),
            }
        )
    return out


@app.function(image=cpu_image, volumes=_VOLUMES, secrets=_secrets(), timeout=_timeout())
def probe_environment() -> dict[str, Any]:
    """What the worker actually is. Used by the infrastructure check, not by a scan."""
    import platform
    import shutil

    return {
        "python": platform.python_version(),
        "ffmpeg": shutil.which("ffmpeg"),
        "hf_home": os.environ.get("HF_HOME"),
        "r2_configured": bool(os.environ.get("R2_ACCESS_KEY_ID")),
    }


def _materialise(frames: list[bytes], names: list[str]) -> list[Path]:
    """Write the incoming frame bytes to a temp dir and return their paths."""
    import tempfile

    if len(frames) != len(names):
        raise ValueError(f"{len(frames)} frames but {len(names)} names")
    directory = Path(tempfile.mkdtemp(prefix="roomfittr-frames-"))
    paths: list[Path] = []
    for data, name in zip(frames, names, strict=True):
        target = directory / Path(name).name
        target.write_bytes(data)
        paths.append(target)
    return paths


def _run_reconstruction(backend_name: str, frames: list[bytes], names: list[str]) -> dict[str, Any]:
    """Shared body for the S3 functions.

    Returns plain arrays rather than a `Reconstruction`, because the dataclass
    would have to be importable on both sides of the boundary with identical
    definitions; the caller rebuilds it. `validate` still runs *here*, so a
    reconstruction that cannot support a room fails inside the GPU container
    with 3.4's reason attached rather than three stages later.
    """
    import time

    from roomfittr_pipeline import backends
    from roomfittr_pipeline.adapters import resolve

    paths = _materialise(frames, names)
    backend = resolve(backend_name)

    started = time.perf_counter()
    reconstruction = backend.reconstruct(paths)
    elapsed = time.perf_counter() - started

    backends.validate(reconstruction)

    return {
        "poses": reconstruction.poses,
        "intrinsics": reconstruction.intrinsics,
        "depth": reconstruction.depth,
        "confidence": reconstruction.confidence,
        "backend": reconstruction.backend,
        "metric_scale": reconstruction.metric_scale,
        # E7's raw material (3.9): per-stage seconds and peak VRAM.
        "elapsed_s": elapsed,
        "peak_vram_bytes": _peak_vram(),
    }


def _peak_vram() -> int | None:
    try:
        import torch

        if torch.cuda.is_available():
            return int(torch.cuda.max_memory_allocated())
    except Exception:  # noqa: BLE001 - profiling must never fail a scan
        return None
    return None
