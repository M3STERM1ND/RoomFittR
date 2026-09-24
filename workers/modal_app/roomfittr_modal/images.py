"""Worker images and the weights volume (implementation-plan.md 3.4, 3.9).

Three images rather than one, because the CPU stages (S1, S2, S5-S8) are the
ones that run on every scan and a 12 GB CUDA image is a slow cold start for
ffmpeg. The GPU images are split by model for the same reason E1 exists at
all: VGGT and MapAnything pull different, large dependency trees, and one
image carrying both means a change to either invalidates the other's cache.

**Weights live in a Modal Volume, not in the image** (3.4 step 1). Baking a
5 GB checkpoint into a layer makes every rebuild re-push it, and the gated
ones cannot be fetched at build time without putting `HF_TOKEN` into the build
context. Downloading on first use into a shared volume means the second
container and every later one start warm.
"""

from __future__ import annotations

import modal

# 3.4 step 1: one volume, shared by every GPU function. HF_HOME points here so
# huggingface_hub's own cache layout does the deduplication for us.
WEIGHTS = modal.Volume.from_name("roomfittr-weights", create_if_missing=True)
WEIGHTS_PATH = "/weights"

# Artefacts a run wants to keep when there is no R2 yet. 2.4's key layout is
# reproduced inside it, so moving to R2 later is a change of backend rather
# than of key.
ARTEFACTS = modal.Volume.from_name("roomfittr-artefacts", create_if_missing=True)
ARTEFACTS_PATH = "/artefacts"

_ENV = {
    "HF_HOME": WEIGHTS_PATH,
    "HF_HUB_ENABLE_HF_TRANSFER": "1",
    # Without this a failed download leaves a half-file that looks cached.
    "HF_HUB_DISABLE_TELEMETRY": "1",
}

# Pinned. 3.9 budgets VRAM per candidate and R15 expects model churn monthly;
# an unpinned torch would make an E7 profile un-reproducible a week later.
_TORCH = "torch==2.8.0"
_TORCH_INDEX = "https://download.pytorch.org/whl/cu128"


def _pipeline_source(image: modal.Image) -> modal.Image:
    """Add the pipeline package itself.

    Copied rather than pip-installed from the repo so a code change does not
    reinstall the dependency tree above it.
    """
    return image.add_local_python_source("roomfittr_pipeline", "roomfittr_schemas")


# `apt_install("ffmpeg")` pulls Debian's *recommended* set with it, which is
# mesa, X11 and SDL2 -- a desktop media stack for a headless worker that only
# ever decodes to disk. It roughly triples the image and was slow enough to be
# killed mid-build. `--no-install-recommends` keeps the codecs and drops the
# display stack.
_APT_FFMPEG = (
    "apt-get update && "
    "DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ffmpeg && "
    "rm -rf /var/lib/apt/lists/*"
)

cpu_image = _pipeline_source(
    modal.Image.debian_slim(python_version="3.12")
    # 3.4 and 7.5: the pipeline decodes untrusted user video, so FFmpeg comes
    # from the distribution rather than an unpinned download.
    .run_commands(_APT_FFMPEG)
    .pip_install(
        "numpy>=2.0",
        "scipy>=1.14",
        "shapely>=2.0",
        "opencv-python-headless>=4.10",
        "trimesh>=4.5",
        "mapbox-earcut>=1.0",
        "pygltflib>=1.16",
        "pydantic>=2.9",
        "rtree>=1.4.1",
        "pyyaml>=6.0",
        "jsonschema>=4.23",
        "boto3>=1.35",
    )
    .env(_ENV)
)

_gpu_base = (
    modal.Image.debian_slim(python_version="3.12")
    .run_commands(
        "apt-get update && "
        "DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "
        "ffmpeg git ca-certificates && "
        "rm -rf /var/lib/apt/lists/*"
    )
    .pip_install(_TORCH, "torchvision", extra_index_url=_TORCH_INDEX)
    .pip_install(
        "numpy>=2.0",
        "scipy>=1.14",
        "shapely>=2.0",
        "opencv-python-headless>=4.10",
        "pillow>=10.0",
        "huggingface_hub>=0.35",
        "hf_transfer>=0.1.8",
        "boto3>=1.35",
        "pydantic>=2.9",
    )
    .env(_ENV)
)

# Candidate B. The commercial checkpoint is gated, so HF_TOKEN is needed at
# *run* time; nothing here needs it at build time.
vggt_image = _pipeline_source(
    _gpu_base.pip_install("git+https://github.com/facebookresearch/vggt.git")
)

# Candidate A.
mapanything_image = _pipeline_source(
    _gpu_base.pip_install("git+https://github.com/facebookresearch/map-anything.git")
)

# S4. SAM 3 ships through transformers.
sam3_image = _pipeline_source(_gpu_base.pip_install("transformers>=4.57", "accelerate>=1.0"))

__all__ = [
    "ARTEFACTS",
    "ARTEFACTS_PATH",
    "WEIGHTS",
    "WEIGHTS_PATH",
    "cpu_image",
    "mapanything_image",
    "sam3_image",
    "vggt_image",
]
