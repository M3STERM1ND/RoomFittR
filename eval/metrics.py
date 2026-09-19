"""Metrics that score a produced RoomModel against laser-measured ground truth.

These are the numbers the Phase 1 go/no-go gate is decided on
(implementation-plan.md 3.10), so each one names the experiment it feeds:

- E2/E3  wall-length error, uncalibrated and calibrated
- E4     floor-polygon IoU and corner count
- E5     door/window recall, precision and offset error

Every function is pure and takes plain dicts, so the harness can score a
pipeline run, a fixture or a hand-written dummy without any of them knowing
about the others.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from shapely.geometry import Polygon

# 3.10 E5: an opening counts as found only if it is on the right wall and
# within this distance along it. Wider than this and it is a different opening.
OPENING_MATCH_TOLERANCE_MM = 500


def percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile. Empty input gives nan, not an exception.

    A room that produced no comparable walls should show up as a gap in the
    report, not take the whole run down.
    """
    if not values:
        return math.nan
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * pct
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return ordered[int(k)]
    return ordered[lo] * (hi - k) + ordered[hi] * (k - lo)


@dataclass
class WallMetrics:
    """E2/E3. Errors are relative, because a 50 mm miss means something very
    different on a 1 m wall than on a 6 m one."""

    matched: int = 0
    truth_count: int = 0
    predicted_count: int = 0
    abs_pct_errors: list[float] = field(default_factory=list)

    @property
    def median_pct(self) -> float:
        return percentile(self.abs_pct_errors, 0.5)

    @property
    def p90_pct(self) -> float:
        return percentile(self.abs_pct_errors, 0.9)


def wall_length_metrics(truth: dict[str, Any], predicted: dict[str, Any]) -> WallMetrics:
    """Compare wall lengths by id.

    Matching by id assumes the pipeline labels walls in the same order the
    measurer did, which it will not do on its own. Until wall correspondence
    is solved properly this deliberately reports how many walls it could not
    match rather than quietly scoring only the easy ones.
    """
    truth_walls = {w["id"]: w["length_mm"] for w in truth.get("walls", [])}
    pred_walls = {w["id"]: w["length_mm"] for w in predicted.get("walls", [])}

    m = WallMetrics(truth_count=len(truth_walls), predicted_count=len(pred_walls))
    for wall_id, truth_len in truth_walls.items():
        pred_len = pred_walls.get(wall_id)
        if pred_len is None or truth_len <= 0:
            continue
        m.matched += 1
        m.abs_pct_errors.append(abs(pred_len - truth_len) / truth_len * 100.0)
    return m


def ceiling_error_pct(truth: dict[str, Any], predicted: dict[str, Any]) -> float:
    t = truth.get("ceiling_height_mm")
    p = predicted.get("ceiling_height_mm")
    if not t or not p:
        return math.nan
    return abs(float(p) - float(t)) / float(t) * 100.0


@dataclass
class PolygonMetrics:
    """E4. IoU is computed on polygons already centred on their centroid, so a
    difference in where the origin ended up does not count as a shape error."""

    iou: float = math.nan
    truth_corners: int = 0
    predicted_corners: int = 0

    @property
    def corner_delta(self) -> int:
        return self.predicted_corners - self.truth_corners


def _centred(points: list[list[int]]) -> Polygon:
    poly = Polygon([(p[0], p[1]) for p in points])
    if not poly.is_valid:
        poly = poly.buffer(0)  # fixes self-touching rings
    c = poly.centroid
    from shapely.affinity import translate

    return translate(poly, xoff=-c.x, yoff=-c.y)


def polygon_metrics(truth: dict[str, Any], predicted: dict[str, Any]) -> PolygonMetrics:
    t_pts = truth.get("floor_polygon_mm")
    p_pts = predicted.get("floor_polygon")
    m = PolygonMetrics(
        truth_corners=len(t_pts) if t_pts else 0,
        predicted_corners=len(p_pts) if p_pts else 0,
    )
    if not t_pts or not p_pts or len(t_pts) < 3 or len(p_pts) < 3:
        return m

    a = _centred(t_pts)
    b = _centred(p_pts)
    union = a.union(b).area
    m.iou = (a.intersection(b).area / union) if union > 0 else math.nan
    return m


@dataclass
class OpeningMetrics:
    """E5, scored separately for doors and windows because the pass criteria
    differ (door recall 85%, window recall 80%)."""

    kind: str
    truth_count: int = 0
    predicted_count: int = 0
    true_positives: int = 0
    offset_errors_mm: list[float] = field(default_factory=list)
    width_errors_mm: list[float] = field(default_factory=list)

    @property
    def recall(self) -> float:
        return self.true_positives / self.truth_count if self.truth_count else math.nan

    @property
    def precision(self) -> float:
        return self.true_positives / self.predicted_count if self.predicted_count else math.nan

    @property
    def median_offset_error_mm(self) -> float:
        return percentile(self.offset_errors_mm, 0.5)


def opening_metrics(truth: dict[str, Any], predicted: dict[str, Any], kind: str) -> OpeningMetrics:
    """Greedy nearest-match on the same wall, within the tolerance.

    Greedy rather than optimal assignment: with a handful of openings per wall
    the two agree, and a Hungarian solve here would be complexity nobody can
    debug at 2am.
    """
    t_open = [o for o in truth.get("openings", []) if o["type"] == kind]
    p_open = [o for o in predicted.get("openings", []) if o["type"] == kind]

    m = OpeningMetrics(kind=kind, truth_count=len(t_open), predicted_count=len(p_open))
    unclaimed = list(p_open)

    for t in t_open:
        best = None
        best_d = math.inf
        for p in unclaimed:
            if p["wall_id"] != t["wall_id"]:
                continue
            d = abs(p["offset_mm"] - t["offset_mm"])
            if d < best_d:
                best, best_d = p, d
        if best is not None and best_d <= OPENING_MATCH_TOLERANCE_MM:
            unclaimed.remove(best)
            m.true_positives += 1
            m.offset_errors_mm.append(best_d)
            m.width_errors_mm.append(abs(best["width_mm"] - t["width_mm"]))

    return m


@dataclass
class RoomScore:
    """Everything scored for one capture, flat enough to become a CSV row."""

    room_id: str
    capture_id: str
    walls: WallMetrics
    ceiling_pct: float
    polygon: PolygonMetrics
    doors: OpeningMetrics
    windows: OpeningMetrics
    note: str = ""

    def as_row(self) -> dict[str, Any]:
        def r(v: float, places: int = 2) -> float | str:
            return "" if isinstance(v, float) and math.isnan(v) else round(v, places)

        return {
            "room_id": self.room_id,
            "capture_id": self.capture_id,
            "walls_matched": f"{self.walls.matched}/{self.walls.truth_count}",
            "wall_err_median_pct": r(self.walls.median_pct),
            "wall_err_p90_pct": r(self.walls.p90_pct),
            "ceiling_err_pct": r(self.ceiling_pct),
            "polygon_iou": r(self.polygon.iou, 3),
            "corner_delta": self.polygon.corner_delta,
            "door_recall": r(self.doors.recall, 3),
            "door_precision": r(self.doors.precision, 3),
            "door_offset_err_mm": r(self.doors.median_offset_error_mm, 0),
            "window_recall": r(self.windows.recall, 3),
            "window_precision": r(self.windows.precision, 3),
            "window_offset_err_mm": r(self.windows.median_offset_error_mm, 0),
            "note": self.note,
        }


def score_capture(
    room_id: str, capture_id: str, truth: dict[str, Any], predicted: dict[str, Any]
) -> RoomScore:
    return RoomScore(
        room_id=room_id,
        capture_id=capture_id,
        walls=wall_length_metrics(truth, predicted),
        ceiling_pct=ceiling_error_pct(truth, predicted),
        polygon=polygon_metrics(truth, predicted),
        doors=opening_metrics(truth, predicted, "door"),
        windows=opening_metrics(truth, predicted, "window"),
    )


# --------------------------------------------------------------------------- #
# Gate evaluation                                                              #
# --------------------------------------------------------------------------- #


@dataclass
class GateResult:
    experiment: str
    question: str
    measured: str
    criterion: str
    passed: bool | None  # None when there was not enough data to judge


def evaluate_gates(scores: list[RoomScore], calibrated: bool) -> list[GateResult]:
    """Apply the 3.10 pass criteria to a set of scores.

    `calibrated` picks between E2 (uncalibrated: median <= 5%, p90 <= 10%) and
    E3 (one user measurement: median <= 2%, p90 <= 4%). A run must say which it
    was; inferring it from the data would be guessing at the thing being tested.
    """
    wall_errors = [e for s in scores for e in s.walls.abs_pct_errors]
    ious = [s.polygon.iou for s in scores if not math.isnan(s.polygon.iou)]

    def fmt(v: float, unit: str = "%") -> str:
        return "no data" if math.isnan(v) else f"{v:.2f}{unit}"

    median = percentile(wall_errors, 0.5)
    p90 = percentile(wall_errors, 0.9)
    med_limit, p90_limit = (2.0, 4.0) if calibrated else (5.0, 10.0)
    exp = "E3" if calibrated else "E2"

    results = [
        GateResult(
            experiment=exp,
            question=("Does one measurement fix scale?" if calibrated else "Is scale good enough?"),
            measured=f"median {fmt(median)}, p90 {fmt(p90)}",
            criterion=f"median <= {med_limit}%, p90 <= {p90_limit}%",
            passed=None if math.isnan(median) else (median <= med_limit and p90 <= p90_limit),
        )
    ]

    if ious:
        good = sum(1 for v in ious if v >= 0.85) / len(ious)
        results.append(
            GateResult(
                experiment="E4",
                question="Is the floor polygon right?",
                measured=f"{good * 100:.0f}% of captures at IoU >= 0.85 (n={len(ious)})",
                criterion="IoU >= 0.85 on >= 80% of captures",
                passed=good >= 0.80,
            )
        )
    else:
        results.append(
            GateResult(
                "E4", "Is the floor polygon right?", "no data", "IoU >= 0.85 on >= 80%", None
            )
        )

    for kind, limit, attr in (("door", 0.85, "doors"), ("window", 0.80, "windows")):
        rec = [getattr(s, attr).recall for s in scores if getattr(s, attr).truth_count]
        offs = [e for s in scores for e in getattr(s, attr).offset_errors_mm]
        if not rec:
            results.append(
                GateResult("E5", f"{kind} detection", "no data", f"recall >= {limit:.0%}", None)
            )
            continue
        mean_recall = sum(rec) / len(rec)
        med_off = percentile(offs, 0.5)
        results.append(
            GateResult(
                experiment="E5",
                question=f"{kind} detection",
                measured=f"recall {mean_recall:.0%}, median offset {fmt(med_off, ' mm')}",
                criterion=f"recall >= {limit:.0%}, offset <= 150 mm",
                passed=mean_recall >= limit and (math.isnan(med_off) or med_off <= 150),
            )
        )

    return results
