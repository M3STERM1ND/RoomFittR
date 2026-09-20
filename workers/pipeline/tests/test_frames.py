"""S2: frame scoring, quality filtering and keyframe selection.

Everything except the last class runs on hand-built arrays. That is the point
of splitting S2 the way it is split -- these thresholds decide whether a real
user's scan succeeds, and they need to be testable without a video file.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest
from numpy.typing import NDArray
from roomfittr_pipeline import frames
from roomfittr_pipeline.errors import PipelineError
from roomfittr_pipeline.frames import FrameScore
from roomfittr_pipeline.ingest import VideoMeta, probe


def _noise(seed: int = 0, size: int = 128) -> NDArray[np.uint8]:
    """A textured grey frame. Noise has broadband detail, so it scores sharp."""
    rng = np.random.default_rng(seed)
    return rng.integers(60, 200, size=(size, size), dtype=np.uint8)


def _score(
    index: int,
    *,
    sharpness: float = 100.0,
    luminance: float = 128.0,
    clipped: float = 0.0,
    crushed: float = 0.0,
    flow: float | None = 0.0,
) -> FrameScore:
    return FrameScore(
        index=index,
        timestamp_s=index / frames.DECODE_FPS,
        sharpness=sharpness,
        mean_luminance=luminance,
        clipped_highlight_frac=clipped,
        crushed_shadow_frac=crushed,
        flow_px=flow,
    )


class TestSharpness:
    def test_blur_lowers_the_score(self) -> None:
        sharp = _noise()
        blurred = cv2.GaussianBlur(sharp, (9, 9), 3.0)
        assert frames.sharpness(sharp) > frames.sharpness(blurred) * 5

    def test_a_flat_frame_scores_near_zero(self) -> None:
        """A blank white wall genuinely has no detail; this is why the filter
        is a percentile over the clip rather than an absolute threshold."""
        assert frames.sharpness(np.full((128, 128), 128, dtype=np.uint8)) < 1.0


class TestExposure:
    def test_mid_grey_is_clean(self) -> None:
        mean, clipped, crushed = frames.exposure(np.full((100, 100), 128, dtype=np.uint8))
        assert mean == pytest.approx(128.0)
        assert clipped == 0.0 and crushed == 0.0

    def test_blown_highlights_are_counted(self) -> None:
        frame = np.full((100, 100), 128, dtype=np.uint8)
        frame[:40, :] = 255  # 40% of the frame, e.g. a window in shot
        _, clipped, _ = frames.exposure(frame)
        assert clipped == pytest.approx(0.40)

    def test_crushed_shadows_are_counted(self) -> None:
        frame = np.zeros((100, 100), dtype=np.uint8)
        _, _, crushed = frames.exposure(frame)
        assert crushed == pytest.approx(1.0)

    def test_empty_frame_is_reported_as_unusable_not_a_crash(self) -> None:
        mean, clipped, crushed = frames.exposure(np.zeros((0, 0), dtype=np.uint8))
        assert (mean, clipped, crushed) == (0.0, 0.0, 1.0)


class TestFlow:
    def test_a_known_shift_is_recovered(self) -> None:
        """The number matters, not just its ordering: the keyframe threshold is
        a fraction of the image diagonal, so a flow that reads half the true
        translation halves the number of keyframes."""
        first = _noise(seed=1, size=256)
        shifted = np.roll(first, 12, axis=1)
        assert frames.flow_magnitude(first, shifted) == pytest.approx(12.0, abs=1.5)

    def test_no_movement_reads_as_no_movement(self) -> None:
        first = _noise(seed=2, size=256)
        assert frames.flow_magnitude(first, first.copy()) == pytest.approx(0.0, abs=0.5)

    def test_a_textureless_pair_reports_zero_rather_than_noise(self) -> None:
        """A blank wall gives nothing to track. Reporting 0 means selection
        does not keep the frame, which is the safe direction."""
        flat = np.full((256, 256), 200, dtype=np.uint8)
        assert frames.flow_magnitude(flat, np.roll(flat, 5, axis=1)) == 0.0


class TestUsable:
    def test_the_blurriest_quarter_is_dropped(self) -> None:
        scores = [_score(i, sharpness=float(i)) for i in range(100)]
        kept = frames.usable(scores)
        assert len(kept) == 75
        assert min(s.sharpness for s in kept) >= 25.0

    def test_badly_exposed_frames_go_before_the_percentile_is_taken(self) -> None:
        """Otherwise a handful of blown frames drag the sharpness cutoff down
        and blurry frames survive in their place."""
        good = [_score(i, sharpness=100.0) for i in range(60)]
        blown = [_score(60 + i, sharpness=500.0, clipped=0.9) for i in range(10)]
        kept = frames.usable(good + blown)
        assert all(s.clipped_highlight_frac == 0.0 for s in kept)

    def test_a_dark_clip_is_reported_as_dark_not_blurry(self) -> None:
        """Both are true of a dark room. "Turn the lights on" is the fix."""
        scores = [_score(i, luminance=20.0) for i in range(100)]
        with pytest.raises(PipelineError) as exc:
            frames.usable(scores)
        assert exc.value.code == "VIDEO_TOO_DARK"

    def test_too_few_survivors(self) -> None:
        scores = [_score(i) for i in range(20)]
        with pytest.raises(PipelineError) as exc:
            frames.usable(scores)
        assert exc.value.code == "VIDEO_TOO_SHORT_OR_BLURRY"

    def test_no_frames_at_all(self) -> None:
        with pytest.raises(PipelineError):
            frames.usable([])


class TestKeyframeSelection:
    def test_frames_are_kept_once_the_camera_has_moved_far_enough(self) -> None:
        """diagonal 1000 -> threshold 100 px of accumulated flow, so at a
        steady 25 px/frame a keyframe falls every fourth frame.

        The candidate count and target are chosen so that the greedy pass
        lands on exactly `target` and neither thinning nor padding runs:
        400 candidates at one keyframe per 4 gives 100, which is the target.
        """
        scores = [_score(i, flow=None if i == 0 else 25.0) for i in range(400)]
        chosen = frames.select_keyframes(scores, diagonal_px=1000.0, target=100)
        assert len(chosen) == 100
        assert [s.index for s in chosen][:4] == [0, 4, 8, 12]

    def test_a_static_camera_yields_only_the_first_frame_before_padding(self) -> None:
        scores = [_score(i, flow=0.0) for i in range(60)]
        chosen = frames.select_keyframes(scores, diagonal_px=1000.0, target=40)
        # Selection found one frame; padding tops it back up so S3 is not starved.
        assert len(chosen) == 40
        assert chosen[0].index == 0

    def test_selection_is_deterministic(self) -> None:
        """E1 compares models on identical keyframes. Without this the bake-off
        compares two models *and* two frame sets, and answers neither question."""
        rng = np.random.default_rng(7)
        scores = [
            _score(i, sharpness=float(rng.integers(50, 500)), flow=None if i == 0 else 30.0)
            for i in range(200)
        ]
        first = frames.select_keyframes(scores, 900.0, 60)
        second = frames.select_keyframes(scores, 900.0, 60)
        assert [s.index for s in first] == [s.index for s in second]

    def test_thinning_spreads_over_time_rather_than_over_list_position(self) -> None:
        """A busy corner produces a burst of keyframes. Taking every Nth *item*
        keeps that burst and starves the far wall; spacing by *timestamp* does
        not.

        The capture here is deliberately lopsided: 200 candidates over the
        first third of the recording, 20 over the remaining two thirds. Every
        one clears the flow threshold, so the greedy pass keeps all 220 and
        thinning has to choose. Position-based thinning would retain
        40 x 200/220 = 36 early frames and 4 late ones; time-based thinning
        should get far closer to an even split of the timeline.
        """
        dense = [_score(i, flow=None if i == 0 else 500.0) for i in range(200)]
        sparse = [_score(200 + i, flow=500.0) for i in range(0, 400, 20)]
        chosen = frames.select_keyframes(dense + sparse, diagonal_px=1000.0, target=40)
        assert len(chosen) == 40

        midpoint = (chosen[0].timestamp_s + chosen[-1].timestamp_s) / 2
        late = [s for s in chosen if s.timestamp_s >= midpoint]
        assert len(late) >= 15, f"only {len(late)}/40 keyframes from the second half"

    def test_target_is_clamped_to_the_gpu_budget(self) -> None:
        scores = [_score(i, flow=None if i == 0 else 500.0) for i in range(400)]
        assert (
            len(frames.select_keyframes(scores, 1000.0, target=9_999)) == frames.MAX_TARGET_FRAMES
        )
        assert len(frames.select_keyframes(scores, 1000.0, target=1)) == frames.MIN_TARGET_FRAMES

    def test_output_is_ordered_in_time(self) -> None:
        """Padding appends by sharpness; S3 receives a video sequence and
        SAM 3's tracking in S4 depends on temporal order."""
        rng = np.random.default_rng(3)
        scores = [
            _score(i, sharpness=float(rng.integers(1, 999)), flow=None if i == 0 else 5.0)
            for i in range(120)
        ]
        chosen = frames.select_keyframes(scores, 1000.0, target=50)
        assert [s.index for s in chosen] == sorted(s.index for s in chosen)

    def test_no_frames_raises(self) -> None:
        with pytest.raises(PipelineError):
            frames.select_keyframes([], 1000.0)


class TestAgainstARealVideo:
    def test_run_produces_both_resolutions(self, good_video: Path, tmp_path: Path) -> None:
        meta: VideoMeta = probe(good_video)
        result = frames.run(good_video, meta, tmp_path, target=40, model_long_side_px=518)

        assert len(result.keyframes) == 40
        model_files = sorted(result.model_dir.glob("*.jpg"))
        full_files = sorted(result.full_dir.glob("*.jpg"))
        assert len(model_files) == len(full_files) == 40

        # 3.3 step 5: the model copy is downscaled, the texturing copy is not.
        model_img = cv2.imread(str(model_files[0]))
        full_img = cv2.imread(str(full_files[0]))
        assert model_img is not None and full_img is not None
        assert max(model_img.shape[:2]) == 518
        assert full_img.shape[:2] == (720, 1280)

    def test_candidates_are_decoded_at_three_fps(self, good_video: Path) -> None:
        meta = probe(good_video)
        scores, decoded = frames.decode_candidates(good_video, meta)
        # 22 s at 3 fps, give or take the final partial second.
        assert 60 <= len(scores) <= 70
        assert len(decoded) == len(scores)
        assert scores[0].flow_px is None
        assert all(s.flow_px is not None for s in scores[1:])

    def test_decoded_frames_have_display_orientation(
        self, rotated_video: Path, tmp_path: Path
    ) -> None:
        """The reconstruction model has no way to tell a sideways room from a
        real one, so rotation has to be resolved before S3 ever sees a pixel."""
        meta = probe(rotated_video)
        assert meta.is_portrait
        _, decoded = frames.decode_candidates(rotated_video, meta)
        assert decoded[0].shape[:2] == (meta.height, meta.width) == (1280, 720)
