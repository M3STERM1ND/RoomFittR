"""S4: open-vocabulary segmentation via SAM 3 (3.5).

`facebook/sam3` is gated, so the worker image needs `HF_TOKEN` from the
`roomfittr-hf` Modal secret.

3.5 asks for prompt-driven segmentation so no detector has to be trained, and
for the result to arrive as **tracked instances** rather than per-frame
detections. The tracking is what makes S5 able to accumulate one object's
points across the frames it appears in; without it, a sofa seen from four
angles is four objects and E6's recall is measured against the wrong thing.

The `label_group` a mask carries -- not its label -- is what decides
removability downstream, which is why it is threaded through from `PROMPTS`
rather than re-derived later.

**Association is by mask IoU across adjacent frames**, which is the cheap
option and is stated here because it is a real limitation: an object that
leaves the view and returns becomes two tracks. 3.5's `MERGE_CONTAINMENT`
step in `objects.py` is what recovers those in 3D, and E6 is what measures
whether the pair is good enough.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from ..backends import InstanceMask
from ..errors import PipelineError, Stage

CHECKPOINT = "facebook/sam3"

# Below this, a detection is noise rather than an object. 3.5 does not fix a
# number; this is the usual SAM 3 default and is a knob E6 can move.
DEFAULT_SCORE_THRESHOLD = 0.4

# Two masks in adjacent frames are the same instance above this IoU. Low
# enough to survive a moving camera, high enough that two chairs side by side
# do not merge.
TRACK_IOU_THRESHOLD = 0.5

# A mask smaller than this fraction of the frame is not a piece of furniture
# at 40 cm (E6's floor); it is a speck that costs a track slot.
MIN_MASK_AREA_FRACTION = 1e-4


@dataclass(slots=True)
class SAM3Backend:
    """S4 via SAM 3."""

    name: str = "sam3"
    checkpoint: str = CHECKPOINT
    device: str = "cuda"
    score_threshold: float = DEFAULT_SCORE_THRESHOLD
    _model: Any = field(default=None, repr=False)
    _processor: Any = field(default=None, repr=False)

    def _load(self) -> tuple[Any, Any]:
        if self._model is not None and self._processor is not None:
            return self._model, self._processor
        try:
            import torch
            from transformers import Sam3Model, Sam3Processor
        except ImportError as exc:  # pragma: no cover - GPU image only
            raise PipelineError(
                "INTERNAL",
                Stage.SEGMENT,
                f"SAM 3 is not installed in this image: {exc}",
            ) from exc

        processor = Sam3Processor.from_pretrained(self.checkpoint)
        model = Sam3Model.from_pretrained(self.checkpoint, dtype=torch.bfloat16)
        model = model.to(self.device)
        model.eval()
        self._model, self._processor = model, processor
        return model, processor

    def segment(self, frame_paths: list[Path], prompts: dict[str, list[str]]) -> list[InstanceMask]:
        import torch
        from PIL import Image

        model, processor = self._load()
        frame_count = len(frame_paths)
        if frame_count == 0:
            return []

        # (label, group) pairs, flattened once so the per-frame loop is a
        # straight pass over prompts rather than nested bookkeeping.
        targets = [(label, group) for group, labels in prompts.items() for label in labels]

        # label -> list of (frame_index, mask)
        per_label: dict[tuple[str, str], list[tuple[int, NDArray[np.bool_], float]]] = {
            t: [] for t in targets
        }

        for frame_index, path in enumerate(frame_paths):
            image = Image.open(path).convert("RGB")
            for label, group in targets:
                inputs = processor(images=image, text=label, return_tensors="pt").to(self.device)
                with torch.no_grad():
                    outputs = model(**inputs)
                results = processor.post_process_instance_segmentation(
                    outputs,
                    threshold=self.score_threshold,
                    target_sizes=[(image.height, image.width)],
                )[0]

                masks = results.get("masks")
                scores = results.get("scores")
                if masks is None or len(masks) == 0:
                    continue
                for mask, score in zip(masks, scores, strict=False):
                    array = np.asarray(
                        mask.cpu().numpy() if hasattr(mask, "cpu") else mask, dtype=bool
                    )
                    if array.mean() < MIN_MASK_AREA_FRACTION:
                        continue
                    per_label[(label, group)].append((frame_index, array, float(score)))

        return _track(per_label, frame_count)


def _iou(a: NDArray[np.bool_], b: NDArray[np.bool_]) -> float:
    union = np.logical_or(a, b).sum()
    if union == 0:
        return 0.0
    return float(np.logical_and(a, b).sum() / union)


def _track(
    per_label: dict[tuple[str, str], list[tuple[int, NDArray[np.bool_], float]]],
    frame_count: int,
) -> list[InstanceMask]:
    """Chain per-frame detections of one label into instances.

    Greedy nearest-IoU association against the most recent frame each track
    was seen in. Tracks are kept per label, so a chair is never associated
    with a table however much their masks overlap.
    """
    instances: list[InstanceMask] = []
    next_track_id = 0

    for (label, group), detections in per_label.items():
        if not detections:
            continue
        # frame index -> list of (mask, score) for this label
        by_frame: dict[int, list[tuple[NDArray[np.bool_], float]]] = {}
        for frame_index, mask, score in detections:
            by_frame.setdefault(frame_index, []).append((mask, score))

        # Each open track: (last_frame, last_mask, frames, scores)
        open_tracks: list[dict[str, Any]] = []
        for frame_index in sorted(by_frame):
            unmatched = list(by_frame[frame_index])
            for track in open_tracks:
                if not unmatched:
                    break
                scored = [(_iou(track["last_mask"], m), i) for i, (m, _) in enumerate(unmatched)]
                best_iou, best_i = max(scored, default=(0.0, -1))
                if best_iou >= TRACK_IOU_THRESHOLD and best_i >= 0:
                    mask, score = unmatched.pop(best_i)
                    track["last_mask"] = mask
                    track["last_frame"] = frame_index
                    track["frames"][frame_index] = mask
                    track["scores"].append(score)
            for mask, score in unmatched:
                open_tracks.append(
                    {
                        "last_frame": frame_index,
                        "last_mask": mask,
                        "frames": {frame_index: mask},
                        "scores": [score],
                    }
                )

        for track in open_tracks:
            frames: dict[int, NDArray[np.bool_]] = track["frames"]
            shape = next(iter(frames.values())).shape
            # (N, H, W) aligned with the reconstruction's frames. Mostly
            # empty by design: `InstanceMask` documents that an instance is
            # visible in a handful of keyframes.
            stacked = np.zeros((frame_count, *shape), dtype=bool)
            for frame_index, mask in frames.items():
                stacked[frame_index] = mask
            instances.append(
                InstanceMask(
                    track_id=next_track_id,
                    label=label,
                    label_group=group,
                    score=float(np.mean(track["scores"])),
                    masks=stacked,
                )
            )
            next_track_id += 1

    return instances
