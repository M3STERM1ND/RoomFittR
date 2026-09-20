"""S7 scale fusion.

E2 ("is uncalibrated scale good enough?") and E3 ("does one user measurement
fix it?") are answered on real captures, which need a GPU and a dataset. What
can be pinned here is the arithmetic underneath them and, more importantly,
the honesty of the confidence it reports: 3.11 requires an uncalibrated scale
to reach the user as a best guess, and that only works if `confidence` means
something.
"""

from __future__ import annotations

import numpy as np
import pytest
from roomfittr_pipeline import scale
from roomfittr_pipeline.errors import PipelineError
from roomfittr_pipeline.scale import Confidence, ScaleSource


def sources(**named: float) -> list[ScaleSource]:
    return [ScaleSource(name=name, factor=factor) for name, factor in named.items()]


class TestScaleSource:
    def test_a_negative_factor_is_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError):
            ScaleSource(name="model", factor=-1.0)

    def test_a_nan_factor_is_rejected(self) -> None:
        """A model that fails quietly returns NaN, and NaN propagates through
        a median without raising anything."""
        with pytest.raises(ValueError):
            ScaleSource(name="mono", factor=float("nan"))

    def test_default_weights_follow_the_plan_ordering(self) -> None:
        assert (
            ScaleSource("user", 1.0).effective_weight > ScaleSource("model", 1.0).effective_weight
        )
        assert (
            ScaleSource("model", 1.0).effective_weight
            > ScaleSource("ceiling", 1.0).effective_weight
        )

    def test_an_unknown_source_gets_a_small_weight_rather_than_crashing(self) -> None:
        assert ScaleSource("something_new", 1.0).effective_weight == pytest.approx(0.1)


class TestWeightedMedian:
    def test_matches_the_plain_median_with_equal_weights(self) -> None:
        values = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        assert scale.weighted_median(values, np.ones(5)) == 3.0

    def test_weight_moves_the_answer(self) -> None:
        values = np.array([1.0, 10.0])
        assert scale.weighted_median(values, np.array([9.0, 1.0])) == 1.0
        assert scale.weighted_median(values, np.array([1.0, 9.0])) == 10.0

    def test_an_empty_set_raises(self) -> None:
        with pytest.raises(ValueError):
            scale.weighted_median(np.array([]), np.array([]))


class TestFusion:
    def test_agreeing_sources_give_a_high_confidence_answer(self) -> None:
        result = scale.fuse(sources(model=1000.0, mono=1010.0, door=995.0))
        assert result.factor == pytest.approx(1000.0, rel=0.02)
        assert result.confidence is Confidence.HIGH
        assert not result.user_calibrated

    def test_mild_disagreement_reads_as_medium(self) -> None:
        result = scale.fuse(sources(model=1000.0, mono=1050.0, door=1020.0))
        assert result.confidence is Confidence.MEDIUM

    def test_wide_disagreement_reads_as_low(self) -> None:
        """This is the case 3.11 cares about: the pipeline still produces a
        room, and the UI has to say the measurement is a guess."""
        result = scale.fuse(sources(model=1000.0, mono=1200.0, door=1350.0))
        assert result.confidence is Confidence.LOW

    def test_a_user_measurement_wins_outright(self) -> None:
        """3.7 calls it authoritative. It is the only source measured with a
        tape rather than inferred from pixels."""
        result = scale.fuse(sources(model=1000.0, mono=1200.0, user=1234.0))
        assert result.factor == 1234.0
        assert result.confidence is Confidence.HIGH
        assert result.user_calibrated

    def test_the_other_sources_survive_a_user_measurement(self) -> None:
        """E2 needs to know what the pipeline would have said unaided, and a
        calibrated scan is the only place with ground truth to compare to."""
        result = scale.fuse(sources(model=1000.0, user=1234.0))
        assert {s.name for s in result.sources} == {"model", "user"}

    def test_one_bad_source_does_not_drag_the_answer(self) -> None:
        """A door prior that measured a cupboard is wrong by a factor. A mean
        would carry a third of that error; the median should barely move."""
        clean = scale.fuse(sources(model=1000.0, mono=1010.0))
        polluted = scale.fuse(sources(model=1000.0, mono=1010.0, door=3000.0))
        assert polluted.factor == pytest.approx(clean.factor, rel=0.05)
        assert "door" in polluted.excluded

    def test_a_lone_source_is_never_high_confidence(self) -> None:
        """Nothing disagrees with it, so its spread is zero -- which would read
        as certainty. One unchecked estimate is precisely what the confidence
        field exists to flag."""
        result = scale.fuse(sources(model=1000.0))
        assert result.confidence is Confidence.MEDIUM

    def test_a_lone_user_measurement_is_high_confidence(self) -> None:
        assert scale.fuse(sources(user=1000.0)).confidence is Confidence.HIGH

    def test_two_sources_that_disagree_wildly_keep_both(self) -> None:
        """With no majority there is nothing to appeal to. Discarding one
        would be arbitrary; reporting low confidence is honest."""
        result = scale.fuse(sources(model=1000.0, mono=4000.0))
        assert result.excluded == []
        assert result.confidence is Confidence.LOW

    def test_no_sources_at_all_is_a_pipeline_error(self) -> None:
        with pytest.raises(PipelineError) as exc:
            scale.fuse([])
        assert exc.value.code == "INSUFFICIENT_COVERAGE"

    def test_the_json_form_matches_the_room_model_schema(self) -> None:
        payload = scale.fuse(sources(model=1000.0, mono=1010.0)).as_json()
        assert set(payload) == {"factor", "sources", "confidence", "user_calibrated"}
        assert payload["confidence"] in {"high", "medium", "low"}
        first = payload["sources"][0]  # type: ignore[index]
        assert set(first) == {"name", "factor", "weight"}


class TestSourceConstructors:
    def test_a_door_of_known_height_gives_the_expected_factor(self) -> None:
        # A door measuring 2.03 units means one unit is one metre.
        assert scale.from_door_height(2.03).factor == pytest.approx(1000.0, rel=1e-6)

    def test_a_short_measured_door_scales_the_room_up(self) -> None:
        """Worth stating because it is the failure mode: a partly occluded
        door measures short and inflates the whole room."""
        assert scale.from_door_height(1.5).factor > scale.from_door_height(2.03).factor

    def test_ceiling_source_is_weighted_below_the_door(self) -> None:
        """3.7 calls the ceiling prior weak: real ceilings vary far more than
        real door heights."""
        assert (
            scale.from_ceiling_height(2.59).effective_weight
            < scale.from_door_height(2.03).effective_weight
        )

    def test_user_measurement_maps_units_to_the_known_length(self) -> None:
        # A wall measured 3.6 units that the user says is 3600 mm.
        assert scale.from_user_measurement(3.6, 3600.0).factor == pytest.approx(1000.0)

    def test_non_positive_measurements_are_rejected(self) -> None:
        with pytest.raises(ValueError):
            scale.from_door_height(0.0)
        with pytest.raises(ValueError):
            scale.from_ceiling_height(-1.0)
        with pytest.raises(ValueError):
            scale.from_user_measurement(1.0, 0.0)


class TestMonoDepth:
    def test_a_uniform_ratio_is_recovered(self) -> None:
        ours = np.full((32, 32), 2.0)
        assert scale.from_mono_depth(ours, ours * 1500.0).factor == pytest.approx(1500.0)

    def test_a_failed_region_does_not_move_the_median(self) -> None:
        """Depth models fail on windows and mirrors, and they fail by a lot.
        A mean over the ratio map would follow them."""
        ours = np.full((40, 40), 2.0)
        theirs = ours * 1500.0
        theirs[:8, :] = 60000.0  # a window the metric model read as far away
        assert scale.from_mono_depth(ours, theirs).factor == pytest.approx(1500.0, rel=0.02)

    def test_invalid_pixels_are_ignored(self) -> None:
        ours = np.full((32, 32), 2.0)
        theirs = ours * 1500.0
        ours[:4, :] = 0.0
        theirs[4:8, :] = np.nan
        assert scale.from_mono_depth(ours, theirs).factor == pytest.approx(1500.0)

    def test_mismatched_shapes_are_a_programming_error(self) -> None:
        with pytest.raises(ValueError):
            scale.from_mono_depth(np.ones((4, 4)), np.ones((5, 5)))

    def test_too_little_overlap_is_a_pipeline_error(self) -> None:
        ours = np.zeros((32, 32))
        ours[0, :5] = 1.0
        with pytest.raises(PipelineError):
            scale.from_mono_depth(ours, np.ones((32, 32)))


class TestCalibrationBehaviour:
    """E3's mechanism: one measurement should fix the whole room.

    The experiment itself needs real captures. What is checkable here is that
    a calibrated scale is applied uniformly -- every length scales by the same
    factor -- which is the property that makes one measurement able to fix the
    others at all.
    """

    def test_one_measurement_rescales_every_length_by_the_same_factor(self) -> None:
        uncalibrated = scale.fuse(sources(model=1000.0, mono=1100.0))
        walls_units = np.array([3.6, 2.8, 3.6, 2.8])

        # The user measures the first wall and it is really 3800 mm.
        calibrated = scale.fuse(
            sources(model=1000.0, mono=1100.0)
            + [scale.from_user_measurement(walls_units[0], 3800.0)]
        )

        before = walls_units * uncalibrated.factor
        after = walls_units * calibrated.factor
        assert after[0] == pytest.approx(3800.0)
        # The correction is a single ratio applied to everything.
        assert np.allclose(after / before, after[0] / before[0])
        assert calibrated.confidence is Confidence.HIGH
