"""The pipeline's error taxonomy (implementation-plan.md 7.4).

Every way a scan can fail is one of these codes. That matters for three
separate readers, which is why the table lives in code rather than in each
`raise` site:

- **The user** sees `message`, and only `message`. It never names a stage, a
  model, a file or an exception type (7.2, "standard error envelope").
- **The worker** reads `retry` to decide whether to try again, and how. A
  `RetryPolicy` of `None` means the run is over.
- **The API** reads `user_fixable` to decide whether to offer "record again"
  (the user can do something) or "retry" (only we can).

The taxonomy is closed on purpose. A stage that wants to fail in a new way
adds a row here, which forces someone to write the sentence the user will
read.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Stage(StrEnum):
    """Pipeline stages, in execution order (implementation-plan.md 2.3).

    The value is what lands in `scans.stage`, in R2 artefact keys
    (`.../{pipeline_version}/{stage}/`) and in the UI's progress label, so
    these strings are a contract and not just an enum.
    """

    INGEST = "ingest"
    FRAMES = "frames"
    RECONSTRUCT = "reconstruct"
    SEGMENT = "segment"
    FUSE = "fuse"
    GEOMETRY = "geometry"
    SCALE = "scale"
    SHELL = "shell"
    PUBLISH = "publish"


class ErrorClass(StrEnum):
    """Who can do something about this failure.

    Drives the UI's call to action, not the retry logic -- a `USER_FIXABLE`
    error is never retried automatically because retrying identical input
    gives an identical failure.
    """

    USER_FIXABLE = "user_fixable"
    SYSTEM = "system"
    TRANSIENT = "transient"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How a failed stage should be re-attempted.

    `backoff_s` is a schedule, not a formula: the plan specifies 30 s / 2 min /
    10 min for transient infrastructure errors, which no single exponent
    reproduces. Its length must equal `max_attempts - 1` (the first attempt
    does not wait), which `__post_init__` enforces so a mis-specified row
    fails at import rather than at 3 a.m.

    `frame_scale` exists for `GPU_OOM` alone: the retry is only worth making
    if it reduces the work, by processing N x 0.7 frames (7.4).
    """

    max_attempts: int
    backoff_s: tuple[int, ...] = ()
    frame_scale: float | None = None

    def __post_init__(self) -> None:
        if self.max_attempts < 2:
            raise ValueError("a RetryPolicy with < 2 attempts is not a retry; use None")
        if self.backoff_s and len(self.backoff_s) != self.max_attempts - 1:
            raise ValueError(
                f"backoff_s has {len(self.backoff_s)} delays for "
                f"{self.max_attempts} attempts; expected {self.max_attempts - 1}"
            )

    def delay_before(self, attempt: int) -> int:
        """Seconds to wait before `attempt` (1-based). Attempt 1 never waits."""
        if attempt <= 1 or not self.backoff_s:
            return 0
        return self.backoff_s[min(attempt - 2, len(self.backoff_s) - 1)]


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    """One row of the 7.4 table."""

    code: str
    error_class: ErrorClass
    message: str
    retry: RetryPolicy | None = None
    fallback_code: str | None = None

    @property
    def user_fixable(self) -> bool:
        return self.error_class is ErrorClass.USER_FIXABLE

    @property
    def retryable(self) -> bool:
        """What `scans.retryable` gets, i.e. whether the *user* may retry.

        Deliberately not the same question as `self.retry`, which is about
        automatic retries inside the worker. A user-fixable error is never
        auto-retried but the user re-recording is exactly the right move, and
        a system error that has exhausted its automatic attempts can still be
        worth one manual go.
        """
        return self.error_class is not ErrorClass.TRANSIENT


# The taxonomy. Messages are the user-visible strings from 7.4; keep them in
# the user's vocabulary ("video", "room", "walls"), never ours ("keyframes",
# "reconstruction", "point cloud").
_SPECS: tuple[ErrorSpec, ...] = (
    ErrorSpec(
        code="VIDEO_UNREADABLE",
        error_class=ErrorClass.USER_FIXABLE,
        message="We couldn't read this video. Try recording again or uploading an MP4.",
    ),
    ErrorSpec(
        code="VIDEO_TOO_SHORT_OR_BLURRY",
        error_class=ErrorClass.USER_FIXABLE,
        message=(
            "We couldn't get enough clear frames from this video. Walk slowly around the "
            "room for at least 20 seconds, keeping the camera steady."
        ),
    ),
    ErrorSpec(
        code="VIDEO_TOO_DARK",
        error_class=ErrorClass.USER_FIXABLE,
        message=(
            "This room was too dark to measure. Turn the lights on, open the blinds and try again."
        ),
    ),
    ErrorSpec(
        code="INSUFFICIENT_COVERAGE",
        error_class=ErrorClass.USER_FIXABLE,
        message=(
            "We couldn't see enough of the walls to measure this room. Walk the whole "
            "perimeter, keeping the line where the walls meet the floor in view."
        ),
    ),
    # Retried once with different keyframe spacing, then reported as the
    # user-fixable coverage error: by then the honest answer is that the video
    # did not show us enough, not that our model fell over.
    ErrorSpec(
        code="RECON_FAILED",
        error_class=ErrorClass.SYSTEM,
        message="Something went wrong while building your room. We'll try again.",
        retry=RetryPolicy(max_attempts=2),
        fallback_code="INSUFFICIENT_COVERAGE",
    ),
    ErrorSpec(
        code="GPU_OOM",
        error_class=ErrorClass.SYSTEM,
        message="Something went wrong while building your room. We'll try again.",
        retry=RetryPolicy(max_attempts=3, frame_scale=0.7),
    ),
    ErrorSpec(
        code="LAYOUT_EXTRACTION_FAILED",
        error_class=ErrorClass.SYSTEM,
        message="Something went wrong while measuring your room. Try again.",
        retry=RetryPolicy(max_attempts=2),
    ),
    ErrorSpec(
        code="MODEL_UNAVAILABLE",
        error_class=ErrorClass.TRANSIENT,
        message="This is taking longer than usual. We're still working on it.",
        retry=RetryPolicy(max_attempts=3, backoff_s=(30, 120)),
    ),
    ErrorSpec(
        code="TIMEOUT",
        error_class=ErrorClass.SYSTEM,
        message="This took too long and we stopped it. Try again.",
        retry=RetryPolicy(max_attempts=2),
    ),
    ErrorSpec(
        code="CANCELED",
        error_class=ErrorClass.SYSTEM,
        message="Canceled.",
    ),
    ErrorSpec(
        code="INTERNAL",
        error_class=ErrorClass.SYSTEM,
        message="Something went wrong; retry.",
        retry=RetryPolicy(max_attempts=2),
    ),
)

ERROR_SPECS: dict[str, ErrorSpec] = {spec.code: spec for spec in _SPECS}


class PipelineError(Exception):
    """A stage failed with a code from the taxonomy.

    `detail` is for us -- logs, Sentry, `scan_events.data`. It must never be
    shown to the user, who gets `spec.message`. Keeping the two in one object
    is what stops an internal path or a stack trace leaking into the UI by
    someone reaching for `str(exc)`.
    """

    def __init__(self, code: str, stage: Stage, detail: str = "") -> None:
        try:
            self.spec = ERROR_SPECS[code]
        except KeyError:
            raise ValueError(
                f"{code!r} is not in the 7.4 error taxonomy; add a row to _SPECS"
            ) from None
        self.stage = stage
        self.detail = detail
        super().__init__(f"[{stage}] {code}: {detail}" if detail else f"[{stage}] {code}")

    @property
    def code(self) -> str:
        return self.spec.code

    @property
    def user_message(self) -> str:
        return self.spec.message

    def envelope(self) -> dict[str, object]:
        """The `{error: {...}}` body of 7.2. Contains nothing internal."""
        return {
            "error": {
                "code": self.code,
                "message": self.user_message,
                "retryable": self.spec.retryable,
            }
        }
