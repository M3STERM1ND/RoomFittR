"""The error taxonomy is a contract with the UI, so it gets tested like one."""

from __future__ import annotations

import pytest
from roomfittr_pipeline.errors import (
    ERROR_SPECS,
    ErrorClass,
    PipelineError,
    RetryPolicy,
    Stage,
)


def test_every_code_in_the_plan_is_present() -> None:
    """7.4's table, verbatim. A missing row means a failure mode with no message."""
    assert set(ERROR_SPECS) == {
        "VIDEO_UNREADABLE",
        "VIDEO_TOO_SHORT_OR_BLURRY",
        "VIDEO_TOO_DARK",
        "INSUFFICIENT_COVERAGE",
        "RECON_FAILED",
        "GPU_OOM",
        "LAYOUT_EXTRACTION_FAILED",
        "MODEL_UNAVAILABLE",
        "TIMEOUT",
        "CANCELED",
        "INTERNAL",
    }


def test_user_messages_never_leak_internals() -> None:
    """7.2: `message` is always user-safe.

    The banned words are the ones that actually showed up in draft copy --
    our stage names and our tooling. A user reading "reconstruction failed"
    learns nothing and worries more.
    """
    banned = {
        "stage",
        "reconstruction",
        "keyframe",
        "gpu",
        "modal",
        "traceback",
        "exception",
        "null",
        "none",
        "sam",
        "vggt",
        "r2",
        "s3",
    }
    for spec in ERROR_SPECS.values():
        words = set(spec.message.lower().replace(".", " ").replace(",", " ").split())
        assert not (words & banned), f"{spec.code} message leaks: {words & banned}"
        assert spec.message[0].isupper(), f"{spec.code} message is not a sentence"


def test_user_fixable_errors_are_never_auto_retried() -> None:
    """Retrying identical input gives an identical failure; only the user can help."""
    for spec in ERROR_SPECS.values():
        if spec.error_class is ErrorClass.USER_FIXABLE:
            assert spec.retry is None, f"{spec.code} auto-retries a user-fixable error"


def test_transient_errors_are_not_offered_to_the_user_as_retryable() -> None:
    """A transient error is already being retried for them; a button would confuse."""
    for spec in ERROR_SPECS.values():
        if spec.error_class is ErrorClass.TRANSIENT:
            assert not spec.retryable


def test_gpu_oom_reduces_frames_on_retry() -> None:
    """7.4: retry with N x 0.7. A retry at the same N just burns the GPU again."""
    spec = ERROR_SPECS["GPU_OOM"]
    assert spec.retry is not None
    assert spec.retry.frame_scale == 0.7
    assert spec.retry.max_attempts == 3


def test_recon_failure_degrades_to_a_coverage_message() -> None:
    """7.4: after the internal retry, the honest answer is about the video."""
    assert ERROR_SPECS["RECON_FAILED"].fallback_code == "INSUFFICIENT_COVERAGE"


def test_backoff_schedule_is_consumed_in_order() -> None:
    policy = RetryPolicy(max_attempts=3, backoff_s=(30, 120))
    assert policy.delay_before(1) == 0
    assert policy.delay_before(2) == 30
    assert policy.delay_before(3) == 120
    # Past the schedule the last delay repeats rather than falling back to 0,
    # which would turn a backoff into a hot loop.
    assert policy.delay_before(9) == 120


def test_mismatched_backoff_length_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="backoff_s"):
        RetryPolicy(max_attempts=3, backoff_s=(30,))


def test_single_attempt_policy_is_rejected() -> None:
    with pytest.raises(ValueError, match="not a retry"):
        RetryPolicy(max_attempts=1)


def test_unknown_code_fails_loudly() -> None:
    """An invented code would otherwise reach the UI as an unhandled string."""
    with pytest.raises(ValueError, match="not in the 7.4 error taxonomy"):
        PipelineError("NOPE", Stage.INGEST)


def test_envelope_carries_no_internal_detail() -> None:
    exc = PipelineError("VIDEO_UNREADABLE", Stage.INGEST, "/tmp/x.mp4 moov atom not found")
    envelope = exc.envelope()["error"]
    assert isinstance(envelope, dict)
    assert envelope["message"] == ERROR_SPECS["VIDEO_UNREADABLE"].message
    assert "moov" not in str(envelope)
    assert "/tmp" not in str(envelope)
    # ...while the detail is still there for us.
    assert "moov" in exc.detail
