"""S7 scale: turn reconstruction units into millimetres (implementation-plan.md 3.7).

This is the stage the product's central promise rests on. "Will it fit?" is a
question about absolute size, and a monocular reconstruction has none: a
doll's house and a ballroom project to identical images. So scale is fused
from several weak, independent estimates, and the *spread between them* is
what tells the UI whether to trust the answer.

Two things this module refuses to do:

- **Present a guess as a measurement.** `confidence` is derived from how much
  the sources disagree, not from how many there are. Three sources that agree
  to 1% is a high-confidence answer; three that disagree by 20% is a low one,
  and 3.11 is explicit that a low-confidence scale must reach the user as a
  best guess.
- **Average away an outlier.** A door prior that latched onto a cupboard is
  wrong by a factor, not by a percent. The fuse is a weighted *median*, so
  one bad source moves the answer a little and never dominates it.

`rescale_scan` (7.1) re-runs this stage alone on stored intermediates when
the user supplies a measurement, which is why nothing here touches the GPU
or the frames.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

import numpy as np

from .errors import PipelineError, Stage

# 3.7's confidence bands, as fractional spread between the surviving sources.
HIGH_CONFIDENCE_SPREAD = 0.03
MEDIUM_CONFIDENCE_SPREAD = 0.08

# Default weights. 3.7 says these should be "learned from Phase 1 data", which
# has not been collected, so they encode the ordering the plan states and
# nothing finer. E2 is what replaces them with measured values; until then
# they are honest about being a prior rather than a result.
DEFAULT_WEIGHTS: dict[str, float] = {
    "user": 1.0,
    "model": 0.35,
    "mono": 0.30,
    "door": 0.25,
    "ceiling": 0.10,
}

# A scale factor this far from the others is not a noisy estimate, it is a
# different answer -- typically a door prior fitted to a cupboard, or a metric
# model that silently fell back to relative output. Excluded before fusing so
# it cannot drag the median, and reported so it is visible.
OUTLIER_RATIO = 1.5

# Sanity band on the final factor's consequences, checked by the caller
# against real dimensions. A scale outside this is a bug, not a bad estimate.
MIN_FACTOR = 1e-6
MAX_FACTOR = 1e9


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True, slots=True)
class ScaleSource:
    """One independent estimate of "reconstruction units -> millimetres"."""

    name: str
    factor: float
    weight: float | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.factor) or self.factor <= 0:
            raise ValueError(f"{self.name} scale factor must be finite and positive")

    @property
    def effective_weight(self) -> float:
        if self.weight is not None:
            return self.weight
        return DEFAULT_WEIGHTS.get(self.name, 0.1)


@dataclass(frozen=True, slots=True)
class ScaleResult:
    """S7's output, written straight into `RoomModel.scale`."""

    factor: float
    confidence: Confidence
    user_calibrated: bool
    sources: list[ScaleSource]
    excluded: list[str]
    spread: float

    def as_json(self) -> dict[str, object]:
        return {
            "factor": self.factor,
            "sources": [
                {"name": s.name, "factor": s.factor, "weight": s.effective_weight}
                for s in self.sources
            ],
            "confidence": str(self.confidence),
            "user_calibrated": self.user_calibrated,
        }


def weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    """The value where cumulative weight first reaches half the total.

    A median rather than a mean because scale estimates fail by being
    *wrong*, not by being noisy: a door prior that measured a cupboard is out
    by 30%, and a mean would carry a third of that error into the answer
    while a median barely moves.
    """
    if len(values) == 0:
        raise ValueError("weighted_median of an empty set")
    order = np.argsort(values)
    ordered_values = np.asarray(values, dtype=np.float64)[order]
    ordered_weights = np.asarray(weights, dtype=np.float64)[order]
    total = float(ordered_weights.sum())
    if total <= 0:
        return float(np.median(ordered_values))
    cumulative = np.cumsum(ordered_weights)
    return float(ordered_values[int(np.searchsorted(cumulative, total / 2.0))])


def relative_spread(factors: np.ndarray) -> float:
    """Disagreement between estimates, as a fraction of their middle value.

    Full range rather than a standard deviation: with three or four sources a
    deviation is barely meaningful, and what the user needs to know is how
    far apart the *extremes* are. Two sources agreeing while a third is 20%
    out should not read as a tight cluster.
    """
    if len(factors) < 2:
        return 0.0
    middle = float(np.median(factors))
    if middle <= 0:
        return math.inf
    return float((factors.max() - factors.min()) / middle)


def confidence_for(spread: float, *, source_count: int) -> Confidence:
    """3.7's bands, with one addition the plan leaves implicit.

    A single source has nothing to disagree with, so its spread is zero and
    the band would read `high`. That would be a lie: one unchecked estimate
    is exactly the situation the confidence field exists to warn about. A
    lone source is capped at `medium`, and only a user measurement earns
    `high` on its own.
    """
    if source_count < 2:
        return Confidence.MEDIUM
    if spread <= HIGH_CONFIDENCE_SPREAD:
        return Confidence.HIGH
    if spread <= MEDIUM_CONFIDENCE_SPREAD:
        return Confidence.MEDIUM
    return Confidence.LOW


def fuse(sources: list[ScaleSource]) -> ScaleResult:
    """Combine the available estimates into one factor plus a confidence.

    A `user` source short-circuits everything: 3.7 calls it authoritative,
    and it is the only source measured with a tape rather than inferred. The
    others are still recorded, so a later look at a scan can see what the
    pipeline would have said on its own -- which is the data E2 needs.
    """
    if not sources:
        raise PipelineError(
            "INSUFFICIENT_COVERAGE",
            Stage.SCALE,
            "no scale estimate available; the room cannot be given real dimensions",
        )

    user = next((s for s in sources if s.name == "user"), None)
    if user is not None:
        return ScaleResult(
            factor=user.factor,
            confidence=Confidence.HIGH,
            user_calibrated=True,
            sources=list(sources),
            excluded=[],
            spread=relative_spread(np.array([s.factor for s in sources])),
        )

    kept, excluded = reject_outliers(sources)
    factors = np.array([s.factor for s in kept], dtype=np.float64)
    weights = np.array([s.effective_weight for s in kept], dtype=np.float64)

    factor = weighted_median(factors, weights)
    if not (MIN_FACTOR < factor < MAX_FACTOR):
        raise PipelineError(
            "INSUFFICIENT_COVERAGE", Stage.SCALE, f"fused scale factor {factor} is not usable"
        )

    spread = relative_spread(factors)
    return ScaleResult(
        factor=factor,
        confidence=confidence_for(spread, source_count=len(kept)),
        user_calibrated=False,
        sources=list(sources),
        excluded=excluded,
        spread=spread,
    )


def reject_outliers(sources: list[ScaleSource]) -> tuple[list[ScaleSource], list[str]]:
    """Drop estimates that differ from the median by more than `OUTLIER_RATIO`.

    Never drops everything: with two sources that disagree wildly there is no
    majority to appeal to, so both are kept and the spread reports the
    disagreement honestly as low confidence.
    """
    if len(sources) < 3:
        return list(sources), []

    factors = np.array([s.factor for s in sources], dtype=np.float64)
    middle = float(np.median(factors))

    kept: list[ScaleSource] = []
    excluded: list[str] = []
    for source in sources:
        ratio = max(source.factor / middle, middle / source.factor)
        if ratio <= OUTLIER_RATIO:
            kept.append(source)
        else:
            excluded.append(source.name)

    if not kept:
        return list(sources), []
    return kept, excluded


def from_door_height(measured_units: float, *, prior_mm: float = 2030.0) -> ScaleSource:
    """3.7's `s_door`: a fully visible door is about 2030 mm tall.

    Only worth calling for a door seen top to bottom. A partly occluded door
    measures short, which would scale the whole room up.
    """
    if measured_units <= 0:
        raise ValueError("door height must be positive")
    return ScaleSource(name="door", factor=prior_mm / measured_units)


def from_ceiling_height(measured_units: float, *, prior_mm: float = 2590.0) -> ScaleSource:
    """3.7's `s_ceiling`, which the plan calls "weak -- sanity check only".

    Ceiling heights vary far more than door heights (2440 in one house, 2740
    in the next, 3500 in a Victorian conversion), so this carries the lowest
    default weight of any source.
    """
    if measured_units <= 0:
        raise ValueError("ceiling height must be positive")
    return ScaleSource(name="ceiling", factor=prior_mm / measured_units)


def from_user_measurement(measured_units: float, known_mm: float) -> ScaleSource:
    """3.7's `s_user`: the user measured something real with a tape."""
    if measured_units <= 0 or known_mm <= 0:
        raise ValueError("user measurement and its model counterpart must be positive")
    return ScaleSource(name="user", factor=known_mm / measured_units)


def from_mono_depth(reconstruction_depth: np.ndarray, metric_depth_mm: np.ndarray) -> ScaleSource:
    """3.7's `s_mono`: the median ratio of a metric depth model to ours.

    Median over confident pixels, for the same reason the fuse is a median:
    depth models fail on specific regions -- windows, mirrors, dark corners --
    and those failures are large rather than noisy.
    """
    ours = np.asarray(reconstruction_depth, dtype=np.float64).ravel()
    theirs = np.asarray(metric_depth_mm, dtype=np.float64).ravel()
    if ours.shape != theirs.shape:
        raise ValueError(f"depth maps differ in size: {ours.shape} vs {theirs.shape}")

    usable = (ours > 0) & (theirs > 0) & np.isfinite(ours) & np.isfinite(theirs)
    if int(usable.sum()) < 100:
        raise PipelineError(
            "INSUFFICIENT_COVERAGE",
            Stage.SCALE,
            f"only {int(usable.sum())} pixels have depth in both maps",
        )
    return ScaleSource(name="mono", factor=float(np.median(theirs[usable] / ours[usable])))
