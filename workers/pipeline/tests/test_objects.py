"""S5 step 6: oriented bounding boxes for detected objects.

E6 ("furniture detection for removal") is scored on real captures against
ARKitScenes' annotated boxes. What is testable here is the fitting itself,
on clusters whose true box the test knows, plus the two failure modes that
would quietly corrupt E6's numbers: untrimmed mask bleed, and tracks of one
object counted as several.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from roomfittr_pipeline import objects
from roomfittr_pipeline.align import Points
from roomfittr_pipeline.objects import Instance, OrientedBox


def box_cloud(
    center: tuple[float, float, float],
    size: tuple[float, float, float],
    yaw_deg: float = 0.0,
    n: int = 2000,
    seed: int = 0,
) -> Points:
    """A solid cloud filling an oriented box, for fitting back."""
    rng = np.random.default_rng(seed)
    local = np.stack(
        [
            rng.uniform(-size[0] / 2, size[0] / 2, n),
            rng.uniform(-size[2] / 2, size[2] / 2, n),
            rng.uniform(-size[1] / 2, size[1] / 2, n),
        ],
        axis=1,
    )
    angle = math.radians(yaw_deg)
    cos, sin = math.cos(angle), math.sin(angle)
    x = local[:, 0] * cos - local[:, 2] * sin
    z = local[:, 0] * sin + local[:, 2] * cos
    return np.stack([x + center[0], local[:, 1] + center[1], z + center[2]], axis=1)


class TestFitBox:
    def test_an_axis_aligned_box_is_recovered(self) -> None:
        cloud = box_cloud((1000.0, 400.0, 500.0), (2000.0, 900.0, 800.0))
        box = objects.fit_box(cloud)
        assert box is not None
        assert box.center[0] == pytest.approx(1000.0, abs=60.0)
        assert box.center[2] == pytest.approx(500.0, abs=60.0)
        assert sorted(box.size[:2]) == pytest.approx([900.0, 2000.0], abs=120.0)
        assert box.size[2] == pytest.approx(800.0, abs=80.0)

    @pytest.mark.parametrize("yaw", [0.0, 15.0, 37.0, 72.0])
    def test_a_rotated_box_keeps_its_true_footprint(self, yaw: float) -> None:
        """A sofa at an angle must not be reported as the larger
        axis-aligned box that encloses it: the solver would then refuse to
        place anything in a corner that is actually free."""
        cloud = box_cloud((0.0, 400.0, 0.0), (1800.0, 800.0, 800.0), yaw_deg=yaw, n=4000)
        box = objects.fit_box(cloud)
        assert box is not None
        assert sorted(box.size[:2]) == pytest.approx([800.0, 1800.0], abs=150.0)

    def test_mask_bleed_is_trimmed_away(self) -> None:
        """3.5's 2nd-98th percentile trim, and the reason for it.

        A segmentation mask leaks onto whatever is behind the object. Here a
        few percent of the sofa's points land on the far wall, and untrimmed
        the box would stretch to meet them -- turning a 900 mm deep sofa into
        a 3 m one and blocking most of the room for the solver.
        """
        sofa = box_cloud((0.0, 400.0, 0.0), (2000.0, 900.0, 800.0), n=4000)
        rng = np.random.default_rng(3)
        bleed = np.stack(
            [
                rng.uniform(-1000.0, 1000.0, 60),
                rng.uniform(0.0, 800.0, 60),
                rng.uniform(2500.0, 3000.0, 60),
            ],
            axis=1,
        )
        box = objects.fit_box(np.concatenate([sofa, bleed]))
        assert box is not None
        assert min(box.size[:2]) < 1400.0, "the bleed stretched the box"

    def test_a_tiny_cluster_is_not_an_object(self) -> None:
        assert objects.fit_box(box_cloud((0.0, 0.0, 0.0), (50.0, 50.0, 50.0))) is None

    def test_too_few_points_is_not_an_object(self) -> None:
        assert objects.fit_box(np.zeros((5, 3))) is None

    def test_wrong_shape_raises(self) -> None:
        with pytest.raises(ValueError):
            objects.fit_box(np.zeros((100, 2)))

    def test_a_collinear_cluster_does_not_crash_the_hull(self) -> None:
        """Points along a single line have no convex hull. A wall sliver
        caught by a mask looks exactly like this."""
        line = np.stack(
            [np.linspace(0.0, 2000.0, 200), np.full(200, 500.0), np.full(200, 100.0)],
            axis=1,
        )
        box = objects.fit_box(line)
        assert box is None or box.size[0] > 0


class TestBoxOverlap:
    def test_a_box_fully_overlaps_itself(self) -> None:
        box = OrientedBox((0.0, 400.0, 0.0), (1000.0, 800.0, 800.0), 0.0)
        assert objects.box_iou_3d(box, box) == pytest.approx(1.0, abs=1e-6)

    def test_separated_boxes_do_not_overlap(self) -> None:
        a = OrientedBox((0.0, 400.0, 0.0), (1000.0, 800.0, 800.0), 0.0)
        b = OrientedBox((5000.0, 400.0, 0.0), (1000.0, 800.0, 800.0), 0.0)
        assert objects.box_iou_3d(a, b) == 0.0

    def test_boxes_at_different_heights_do_not_overlap(self) -> None:
        """A rug and a ceiling light share a footprint and nothing else."""
        rug = OrientedBox((0.0, 5.0, 0.0), (2000.0, 1500.0, 10.0), 0.0)
        light = OrientedBox((0.0, 2300.0, 0.0), (400.0, 400.0, 300.0), 0.0)
        assert objects.box_iou_3d(rug, light) == 0.0


class TestContainment:
    """Why IoU alone cannot decide whether two tracks are one object."""

    def test_a_partial_view_scores_low_on_iou_and_high_on_containment(self) -> None:
        sofa = OrientedBox((0.0, 400.0, 0.0), (2000.0, 900.0, 800.0), 0.0)
        glimpse = OrientedBox((0.0, 400.0, 0.0), (400.0, 400.0, 800.0), 0.0)
        assert objects.box_iou_3d(sofa, glimpse) < objects.MERGE_IOU
        assert objects.box_containment(sofa, glimpse) > objects.MERGE_CONTAINMENT
        assert objects.same_object(sofa, glimpse)

    def test_neighbouring_objects_are_not_contained(self) -> None:
        """The property that makes containment safe to merge on: a box beside
        another box is not inside it, however close."""
        bed = OrientedBox((0.0, 300.0, 0.0), (1500.0, 2000.0, 600.0), 0.0)
        nightstand = OrientedBox((1000.0, 300.0, -900.0), (450.0, 400.0, 600.0), 0.0)
        assert objects.box_containment(bed, nightstand) < objects.MERGE_CONTAINMENT
        assert not objects.same_object(bed, nightstand)

    def test_separated_boxes_have_no_containment(self) -> None:
        a = OrientedBox((0.0, 400.0, 0.0), (1000.0, 800.0, 800.0), 0.0)
        b = OrientedBox((5000.0, 400.0, 0.0), (1000.0, 800.0, 800.0), 0.0)
        assert objects.box_containment(a, b) == 0.0


class TestMerging:
    def _instance(
        self,
        track: int,
        center: tuple[float, float, float],
        size: tuple[float, float, float],
        *,
        label: str = "sofa",
        group: str = "removable",
        score: float = 0.9,
        n: int = 2000,
        seed: int = 0,
    ) -> Instance:
        return Instance(
            track_id=track,
            label=label,
            label_group=group,
            score=score,
            points=box_cloud(center, size, n=n, seed=seed),
        )

    def test_one_object_seen_as_two_tracks_becomes_one_object(self) -> None:
        """The user walked round the sofa and it left the frame in between.
        Counting it twice would put a phantom obstacle in the room."""
        merged = objects.merge_instances(
            [
                self._instance(1, (0.0, 400.0, 0.0), (2000.0, 900.0, 800.0), seed=1),
                self._instance(2, (60.0, 400.0, 30.0), (1900.0, 880.0, 800.0), seed=2),
            ]
        )
        assert len(merged) == 1
        assert sorted(merged[0].track_ids) == [1, 2]

    def test_two_real_objects_stay_separate(self) -> None:
        merged = objects.merge_instances(
            [
                self._instance(1, (0.0, 400.0, 0.0), (2000.0, 900.0, 800.0), seed=1),
                self._instance(2, (4000.0, 400.0, 2000.0), (1000.0, 1000.0, 500.0), seed=2),
            ]
        )
        assert len(merged) == 2

    def test_objects_in_different_groups_never_merge(self) -> None:
        """3.5 merges within a label group. A rug under a radiator overlaps
        it, and merging them would make the rug unremovable."""
        merged = objects.merge_instances(
            [
                self._instance(
                    1, (0.0, 200.0, 0.0), (1200.0, 600.0, 400.0), label="radiator", group="fixed"
                ),
                self._instance(
                    2, (0.0, 200.0, 0.0), (1200.0, 600.0, 400.0), label="rug", group="removable"
                ),
            ]
        )
        assert len(merged) == 2
        assert {o.label_group for o in merged} == {"fixed", "removable"}

    def test_the_larger_view_keeps_the_identity(self) -> None:
        """Merging in track order lets a sliver glimpsed first name the whole
        object. Ordering by footprint means the full view wins."""
        sliver = self._instance(
            1, (0.0, 400.0, 0.0), (400.0, 400.0, 800.0), label="box", score=0.4, seed=1
        )
        whole = self._instance(
            2, (0.0, 400.0, 0.0), (2000.0, 900.0, 800.0), label="sofa", score=0.95, seed=2
        )
        merged = objects.merge_instances([sliver, whole])
        assert len(merged) == 1
        assert merged[0].label == "sofa"
        assert merged[0].score == pytest.approx(0.95)

    def test_ids_are_stable_and_sequential(self) -> None:
        merged = objects.merge_instances(
            [
                self._instance(1, (0.0, 400.0, 0.0), (2000.0, 900.0, 800.0), seed=1),
                self._instance(2, (5000.0, 400.0, 0.0), (1000.0, 600.0, 700.0), seed=2),
            ]
        )
        assert [o.id for o in merged] == ["D1", "D2"]


class TestSerialisation:
    def _objects(self) -> list[objects.DetectedObject]:
        return objects.merge_instances(
            [
                Instance(
                    1,
                    "sofa",
                    "removable",
                    0.93,
                    box_cloud((0.0, 400.0, 0.0), (2000.0, 900.0, 800.0)),
                ),
                Instance(
                    2,
                    "radiator",
                    "fixed",
                    0.81,
                    box_cloud((3000.0, 300.0, 1500.0), (1200.0, 200.0, 600.0), seed=4),
                ),
            ]
        )

    def test_the_label_group_decides_removability(self) -> None:
        """Not the label. A radiator is furniture to a person and immovable
        to a solver, and S4's prompt groups are where that is settled."""
        by_label = {o.label: o for o in self._objects()}
        assert by_label["sofa"].removable
        assert not by_label["radiator"].removable

    def test_the_json_matches_the_objects_file_schema(self) -> None:
        payload = objects.objects_file(self._objects(), pipeline_version="0.1.0")
        assert set(payload) == {"schema_version", "pipeline_version", "units", "objects"}
        assert payload["units"] == "mm"

        entry = payload["objects"][0]
        assert {"id", "label", "label_group", "score", "obb", "removable"} <= set(entry)
        # The schema requires integer millimetres throughout the box.
        assert all(isinstance(v, int) for v in entry["obb"]["center"])
        assert all(isinstance(v, int) and v >= 1 for v in entry["obb"]["size"])
        assert 0.0 <= entry["score"] <= 1.0

    def test_fixed_objects_become_obstacles_the_solver_must_respect(self) -> None:
        """3.7: fixed-group detections default to "keep", whether or not the
        user ever opens the keep-or-remove UI."""
        obstacles = objects.fixed_obstacles(self._objects())
        assert len(obstacles) == 1
        assert obstacles[0]["label"] == "radiator"
        assert obstacles[0]["source"] == "detected"
        # RoomModel's fixed_obstacles carry an XZ centre, not a 3D one.
        assert len(obstacles[0]["center"]) == 2

    def test_removable_objects_are_not_obstacles(self) -> None:
        assert all(o["label"] != "sofa" for o in objects.fixed_obstacles(self._objects()))
