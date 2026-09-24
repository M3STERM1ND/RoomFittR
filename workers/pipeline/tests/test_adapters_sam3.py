"""SAM 3 adapter: the tracking, which is ours rather than the model's.

The model needs a gated checkpoint and a GPU, so inference runs on Modal. The
association step does not, and it is what decides whether a sofa seen from
four angles is one object or four -- which is exactly what E6's recall is
measured against.
"""

from __future__ import annotations

import numpy as np
from roomfittr_pipeline.adapters.sam3 import _iou, _track


def _box(shape: tuple[int, int], top: int, left: int, height: int, width: int) -> np.ndarray:
    mask = np.zeros(shape, dtype=bool)
    mask[top : top + height, left : left + width] = True
    return mask


def test_iou_of_disjoint_and_identical_masks() -> None:
    a = _box((10, 10), 0, 0, 4, 4)
    b = _box((10, 10), 6, 6, 4, 4)
    assert _iou(a, b) == 0.0
    assert _iou(a, a) == 1.0


def test_empty_masks_do_not_divide_by_zero() -> None:
    empty = np.zeros((4, 4), dtype=bool)
    assert _iou(empty, empty) == 0.0


def test_one_object_drifting_across_frames_is_a_single_track() -> None:
    # A camera panning past a sofa: the mask moves a little each frame but
    # overlaps heavily with where it just was.
    shape = (20, 20)
    detections = [
        (0, _box(shape, 5, 5, 8, 8), 0.9),
        (1, _box(shape, 5, 6, 8, 8), 0.9),
        (2, _box(shape, 5, 7, 8, 8), 0.8),
    ]
    tracks = _track({("sofa", "removable"): detections}, frame_count=3)

    assert len(tracks) == 1
    track = tracks[0]
    assert track.label == "sofa"
    assert track.label_group == "removable"
    # (N, H, W) aligned with the reconstruction's frames.
    assert track.masks.shape == (3, 20, 20)
    assert [bool(track.masks[i].any()) for i in range(3)] == [True, True, True]


def test_two_objects_of_the_same_label_stay_separate() -> None:
    # Two chairs side by side. Merging them would undercount furniture and,
    # worse, produce one oversized OBB spanning both -- which is the E6
    # failure that moves a wall.
    shape = (20, 20)
    detections = [
        (0, _box(shape, 2, 2, 5, 5), 0.9),
        (0, _box(shape, 2, 13, 5, 5), 0.9),
        (1, _box(shape, 2, 2, 5, 5), 0.9),
        (1, _box(shape, 2, 13, 5, 5), 0.9),
    ]
    tracks = _track({("chair", "removable"): detections}, frame_count=2)

    assert len(tracks) == 2
    assert all(t.masks.shape == (2, 20, 20) for t in tracks)
    assert {t.track_id for t in tracks} == {0, 1}


def test_labels_never_associate_with_each_other() -> None:
    # A table mask that overlaps a rug mask perfectly must still be two
    # instances: `label_group` is what decides removability downstream, and
    # merging across labels would lose it.
    shape = (10, 10)
    mask = _box(shape, 0, 0, 6, 6)
    tracks = _track(
        {
            ("table", "removable"): [(0, mask, 0.9)],
            ("rug", "removable"): [(0, mask, 0.9)],
        },
        frame_count=1,
    )

    assert len(tracks) == 2
    assert {t.label for t in tracks} == {"table", "rug"}


def test_track_ids_are_unique_across_labels() -> None:
    shape = (10, 10)
    tracks = _track(
        {
            ("sofa", "removable"): [(0, _box(shape, 0, 0, 3, 3), 0.9)],
            ("door", "structure"): [(0, _box(shape, 6, 6, 3, 3), 0.8)],
        },
        frame_count=1,
    )

    assert len({t.track_id for t in tracks}) == len(tracks)
    groups = {t.label: t.label_group for t in tracks}
    assert groups == {"sofa": "removable", "door": "structure"}


def test_a_gap_in_visibility_leaves_empty_frames_not_missing_ones() -> None:
    # `InstanceMask.masks` is documented as mostly empty. Frames where the
    # object is not visible must still exist in the stack, or the array stops
    # being aligned with the reconstruction's frames.
    shape = (10, 10)
    tracks = _track(
        {
            ("lamp", "removable"): [
                (0, _box(shape, 0, 0, 4, 4), 0.9),
                (3, _box(shape, 0, 0, 4, 4), 0.9),
            ]
        },
        frame_count=5,
    )

    assert len(tracks) == 1
    masks = tracks[0].masks
    assert masks.shape == (5, 10, 10)
    assert [bool(masks[i].any()) for i in range(5)] == [True, False, False, True, False]


def test_score_is_averaged_over_the_track() -> None:
    shape = (10, 10)
    mask = _box(shape, 0, 0, 5, 5)
    tracks = _track({("bed", "removable"): [(0, mask, 1.0), (1, mask, 0.6)]}, frame_count=2)
    assert len(tracks) == 1
    assert tracks[0].score == 0.8
