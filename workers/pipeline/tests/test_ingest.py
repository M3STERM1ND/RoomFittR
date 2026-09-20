"""S1: probing, the 3.2 input contract, and metadata stripping."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from roomfittr_pipeline import ingest
from roomfittr_pipeline.errors import PipelineError
from roomfittr_pipeline.ingest import VideoMeta


def _meta(**overrides: object) -> VideoMeta:
    """A meta that passes every check, so each test can break exactly one thing."""
    base: dict[str, object] = {
        "duration_s": 60.0,
        "coded_width": 1920,
        "coded_height": 1080,
        "rotation_deg": 0,
        "fps": 30.0,
        "codec": "h264",
        "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
        "size_bytes": 50 * 1024 * 1024,
        "has_audio": True,
    }
    base.update(overrides)
    return VideoMeta(**base)  # type: ignore[arg-type]


class TestFpsParsing:
    def test_rational(self) -> None:
        assert ingest._parse_fps("30000/1001") == pytest.approx(29.97, abs=0.01)

    def test_plain_integer(self) -> None:
        assert ingest._parse_fps("30") == 30.0

    def test_ffprobe_unknown_is_zero_not_an_exception(self) -> None:
        """`0/0` means "cannot tell"; validation rejects it with the right message."""
        assert ingest._parse_fps("0/0") == 0.0

    def test_garbage_is_zero(self) -> None:
        assert ingest._parse_fps("N/A") == 0.0


class TestRotation:
    def test_display_matrix_sign_is_inverted(self) -> None:
        """A display matrix of -90 means the video is displayed rotated 90 clockwise."""
        stream = {"side_data_list": [{"rotation": -90}]}
        assert ingest._rotation_from(stream) == 90

    def test_legacy_rotate_tag(self) -> None:
        assert ingest._rotation_from({"tags": {"rotate": "270"}}) == 270

    def test_side_data_wins_over_the_tag(self) -> None:
        """Newer FFmpeg writes both; the side data is the authoritative one."""
        stream = {"side_data_list": [{"rotation": -90}], "tags": {"rotate": "180"}}
        assert ingest._rotation_from(stream) == 90

    def test_absent_is_zero(self) -> None:
        assert ingest._rotation_from({}) == 0


class TestDisplayedDimensions:
    def test_unrotated_passes_through(self) -> None:
        meta = _meta()
        assert (meta.width, meta.height) == (1920, 1080)
        assert not meta.is_portrait

    def test_ninety_degrees_swaps(self) -> None:
        """The whole point of the property: coded 1920x1080 is displayed 1080x1920."""
        meta = _meta(rotation_deg=90)
        assert (meta.width, meta.height) == (1080, 1920)
        assert meta.is_portrait
        assert meta.short_side == 1080

    def test_one_eighty_does_not_swap(self) -> None:
        meta = _meta(rotation_deg=180)
        assert (meta.width, meta.height) == (1920, 1080)


class TestValidation:
    def test_a_good_video_passes_unchanged(self) -> None:
        meta = _meta()
        assert ingest.validate(meta) is meta

    def test_too_short(self) -> None:
        with pytest.raises(PipelineError) as exc:
            ingest.validate(_meta(duration_s=12.0))
        assert exc.value.code == "VIDEO_TOO_SHORT_OR_BLURRY"

    def test_too_small_short_side(self) -> None:
        with pytest.raises(PipelineError) as exc:
            ingest.validate(_meta(coded_width=1280, coded_height=600))
        assert exc.value.code == "VIDEO_UNREADABLE"

    def test_a_portrait_video_is_measured_on_its_real_short_side(self) -> None:
        """Regression: reading `coded_width` would pass a 1080x1920 portrait clip
        as 1920-wide, and fail a legitimate 720x1280 one. Both are correct here."""
        assert ingest.validate(_meta(coded_width=1920, coded_height=1080, rotation_deg=90))
        with pytest.raises(PipelineError):
            ingest.validate(_meta(coded_width=1280, coded_height=600, rotation_deg=90))

    def test_too_few_fps(self) -> None:
        with pytest.raises(PipelineError) as exc:
            ingest.validate(_meta(fps=15.0))
        assert exc.value.code == "VIDEO_UNREADABLE"

    def test_oversize(self) -> None:
        with pytest.raises(PipelineError):
            ingest.validate(_meta(size_bytes=ingest.MAX_BYTES + 1))

    def test_unaccepted_container(self) -> None:
        with pytest.raises(PipelineError):
            ingest.validate(_meta(format_name="avi"))

    def test_webm_is_accepted(self) -> None:
        """MediaRecorder on Android Chrome produces WebM (3.2, E9)."""
        assert ingest.validate(_meta(format_name="matroska,webm"))

    def test_over_long_is_truncated_not_rejected(self) -> None:
        """3.2 says truncate past 3 min. Rejecting a 3:05 capture would be unkind."""
        result = ingest.validate(_meta(duration_s=240.0))
        assert result.truncated_to_s == ingest.MAX_DURATION_S

    def test_a_hair_over_the_limit_is_left_alone(self) -> None:
        """Re-encoding to shave 0.2 s off is pure cost for no benefit."""
        assert ingest.validate(_meta(duration_s=180.3)).truncated_to_s is None


class TestAgainstRealFiles:
    def test_probe_reads_a_real_clip(self, good_video: Path) -> None:
        meta = ingest.probe(good_video)
        assert meta.duration_s == pytest.approx(22.0, abs=0.5)
        assert (meta.width, meta.height) == (1280, 720)
        assert meta.fps == pytest.approx(30.0, abs=0.1)
        assert meta.codec == "h264"

    def test_probe_reads_the_rotation_tag(self, rotated_video: Path) -> None:
        meta = ingest.probe(rotated_video)
        assert meta.rotation_deg == 90
        assert meta.is_portrait

    def test_a_non_video_is_unreadable_rather_than_a_crash(self, tmp_path: Path) -> None:
        junk = tmp_path / "notavideo.mp4"
        junk.write_bytes(b"this is not an mp4" * 100)
        with pytest.raises(PipelineError) as exc:
            ingest.probe(junk)
        assert exc.value.code == "VIDEO_UNREADABLE"

    def test_short_clip_is_rejected_end_to_end(self, short_video: Path, tmp_path: Path) -> None:
        with pytest.raises(PipelineError) as exc:
            ingest.run(short_video, tmp_path)
        assert exc.value.code == "VIDEO_TOO_SHORT_OR_BLURRY"

    def test_low_resolution_is_rejected_end_to_end(
        self, low_res_video: Path, tmp_path: Path
    ) -> None:
        with pytest.raises(PipelineError):
            ingest.run(low_res_video, tmp_path)

    def test_run_strips_metadata_and_audio(self, good_video: Path, tmp_path: Path) -> None:
        """The privacy commitment of 3.3 step 2, checked against the output file."""
        # Give the source something to strip: a GPS tag and an audio track.
        with_extras = tmp_path / "with_extras.mp4"
        subprocess.run(
            [
                ingest._resolve("ffmpeg"),
                "-y",
                "-v",
                "error",
                "-i",
                str(good_video),
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=22",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-metadata",
                "location=+51.5074-000.1278/",
                "-metadata",
                "comment=shot at home",
                str(with_extras),
            ],
            check=True,
            capture_output=True,
            timeout=300,
        )
        assert ingest.probe(with_extras).has_audio

        result = ingest.run(with_extras, tmp_path / "work")

        stripped = ingest.probe(result.video_path)
        assert not stripped.has_audio, "audio survived the remux"
        # The rotation tag must survive even though everything else goes.
        assert stripped.rotation_deg == 0
        assert stripped.duration_s == pytest.approx(22.0, abs=0.5)

        raw = result.video_path.read_bytes()
        assert b"51.5074" not in raw, "GPS coordinates survived the remux"
        assert b"shot at home" not in raw, "comment metadata survived the remux"

    def test_portrait_capture_is_warned_about_not_rejected(
        self, rotated_video: Path, tmp_path: Path
    ) -> None:
        result = ingest.run(rotated_video, tmp_path)
        assert "portrait_capture" in result.warnings
        assert result.video_path.exists()
