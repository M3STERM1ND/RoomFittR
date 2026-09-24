"""Concrete S3/S4 backends behind the `backends.py` protocols (3.4, 3.5).

Split from `backends.py` on purpose. That module is the contract and imports
nothing heavier than numpy, so the whole pipeline -- and CI -- can import it
on a laptop. These modules load multi-gigabyte checkpoints and need a GPU, so
every one of them imports torch *inside* its methods rather than at module
scope. Importing this package costs nothing and tells you what exists;
constructing a backend is what pulls the weights in.

`resolve` is the only way E1 selects a candidate, so the bake-off names a
model with a string and cannot accidentally compare two differently-wired
versions of the same one (3.4's reason for the adapter boundary, and R15's
mitigation).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..backends import ReconstructionBackend, SegmentationBackend

# 3.4's candidate letters, so `eval/reports/phase1.md` and the code agree on
# what "candidate B" means.
RECONSTRUCTION_CANDIDATES = {
    "A": "mapanything",
    "B": "vggt",
    "C": "depth-anything-3",
    "D": "colmap",
}


def resolve(name: str, **kwargs: object) -> ReconstructionBackend:
    """Build a reconstruction backend by name or by 3.4 candidate letter."""
    key = RECONSTRUCTION_CANDIDATES.get(name.upper(), name).lower()
    if key == "vggt":
        from .vggt import VGGTBackend

        return VGGTBackend(**kwargs)  # type: ignore[arg-type]
    if key == "mapanything":
        from .mapanything import MapAnythingBackend

        return MapAnythingBackend(**kwargs)  # type: ignore[arg-type]
    raise ValueError(
        f"unknown reconstruction backend {name!r}; "
        f"available: vggt, mapanything (candidates {sorted(RECONSTRUCTION_CANDIDATES)})"
    )


def resolve_segmentation(name: str = "sam3", **kwargs: object) -> SegmentationBackend:
    """Build a segmentation backend. 3.5 names SAM 3; nothing else is wired."""
    if name.lower() in {"sam3", "sam-3"}:
        from .sam3 import SAM3Backend

        return SAM3Backend(**kwargs)  # type: ignore[arg-type]
    raise ValueError(f"unknown segmentation backend {name!r}")


__all__ = ["RECONSTRUCTION_CANDIDATES", "resolve", "resolve_segmentation"]
