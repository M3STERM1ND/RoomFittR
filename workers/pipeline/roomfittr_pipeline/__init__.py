"""RoomFittr CV pipeline: video in, `RoomModel` out.

Stages S1-S9 of implementation-plan.md 3. Deliberately free of Modal imports
so the whole pipeline runs locally, in CI and on any GPU host; the Modal
wrappers live in `workers/modal_app/`.
"""

from .errors import ErrorClass, ErrorSpec, PipelineError, RetryPolicy, Stage

__all__ = ["ErrorClass", "ErrorSpec", "PipelineError", "RetryPolicy", "Stage"]

# Semver, recorded on every artefact and every `room_models` row (2.4).
# Bump the minor when a stage changes in a way that alters its output.
PIPELINE_VERSION = "0.1.0"
