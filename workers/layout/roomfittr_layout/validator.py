"""The deterministic validator (implementation-plan.md 5.5).

Decision 4 of the plan: "The AI decides *what* and *roughly where*.
Deterministic code decides *exactly where* and *whether it's legal*." This
module is the second half of that sentence, and it is the final authority.
No LLM output reaches a user without passing through here.

Two properties matter more than the rules themselves:

1. **Every violation carries a measured value and a sentence.** The schema
   requires it and 5.5 is explicit: the report is what the UI shows, and it
   "is never an opaque AI opinion". "Walkway narrows to 710 mm between the
   sofa and the bookshelf" is actionable; "layout invalid" is not.
2. **It is implemented twice.** This module and `packages/geometry/
   validator.ts` must produce identical reports, pinned by
   `fixtures/validation/*.json` in CI. The solver uses this one; the browser
   uses the other one on every pointer-move while dragging. If they disagree,
   the editor lets a user build something the server then rejects.

Hard rules (H1-H7) block an AI layout and show red in manual editing. Soft
rules (S1-S6) are warnings a user may ignore -- a manual layout is allowed to
be odd, it is just not allowed to be impossible.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from shapely.geometry import Polygon

from .geometry import (
    TOLERANCE_MM,
    Footprint,
    gap_between,
    protrusion,
    separating_axis_overlap,
)
from .room import (
    MIN_CONNECTED_FRACTION,
    CirculationGrid,
    RoomAnalysis,
    circulation_grid,
    connected_component,
    door_standing_point,
)
from .rules import CATEGORY_RULES, CategoryRule

# A category the rules file has never heard of still has to place: 4.2's
# vocabulary can grow ahead of 5.3, and the right degradation is "no special
# rules" rather than a failed layout.
_DEFAULT_RULE = CategoryRule()

SCHEMA_VERSION = 1

# H6 / S1: the minimum and the comfortable walkway. 760 mm is two person
# radii and is the hard floor; 900 mm is what a room should actually have.
MIN_WALKWAY_MM = 760
COMFORTABLE_WALKWAY_MM = 900

# H5: items must clear the ceiling by this much.
CEILING_CLEARANCE_MM = 50

# S5: an item this much of its wall run, or of the room, is oversized.
MAX_WALL_RUN_FRACTION = 0.7
MAX_ROOM_FRACTION = 0.9

# S6: a price older than this is shown with a caveat.
STALE_PRICE_DAYS = 7


@dataclass(frozen=True, slots=True)
class PlacedItem:
    """One item in a layout, as the validator needs to see it."""

    id: str
    category: str
    footprint: Footprint
    price_cents: int = 0
    # Set when the intent was to put this against a particular wall, so S4 can
    # check it is not facing backwards.
    against_wall_id: str | None = None
    price_age_days: int = 0
    available: bool = True

    @property
    def is_floor_covering(self) -> bool:
        """Rugs and the like: H2 allows anything to sit on top of them."""
        return CATEGORY_RULES.get(self.category, _DEFAULT_RULE).layer == "floor_covering"

    @property
    def rule(self) -> CategoryRule:
        return CATEGORY_RULES.get(self.category, _DEFAULT_RULE)


@dataclass(frozen=True, slots=True)
class Violation:
    code: str
    severity: str
    message: str
    measured_mm: int | None = None
    limit_mm: int | None = None
    related_ids: list[str] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }
        if self.measured_mm is not None:
            payload["measured_mm"] = self.measured_mm
        if self.limit_mm is not None:
            payload["limit_mm"] = self.limit_mm
        if self.related_ids:
            payload["related_ids"] = self.related_ids
        return payload


@dataclass
class ValidationReport:
    """5.5's report. Serialises to the `ValidationReport` schema."""

    fits: bool
    violations: list[Violation]
    items: dict[str, list[Violation]]
    min_walkway_mm: int | None = None
    free_floor_pct: int | None = None
    total_price_cents: int | None = None

    def as_json(self) -> dict[str, Any]:
        measured: dict[str, Any] = {}
        if self.min_walkway_mm is not None:
            measured["min_walkway_mm"] = self.min_walkway_mm
        if self.free_floor_pct is not None:
            measured["free_floor_pct"] = self.free_floor_pct
        if self.total_price_cents is not None:
            measured["total_price"] = {"amount_cents": self.total_price_cents, "currency": "USD"}

        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "fits": self.fits,
            "violations": [v.as_json() for v in self.violations],
            # Sorted so two runs -- and the two implementations -- serialise
            # identically. The parity suite compares JSON, so ordering is
            # part of the contract.
            "items": [
                {"item_id": item_id, "violations": [v.as_json() for v in violations]}
                for item_id, violations in sorted(self.items.items())
                if violations
            ],
        }
        if measured:
            payload["measured"] = measured
        return payload

    @property
    def hard_violations(self) -> list[Violation]:
        everything = list(self.violations) + [v for vs in self.items.values() for v in vs]
        return [v for v in everything if v.severity == "hard"]


def _mm(value: float) -> int:
    return int(round(value))


def validate(
    items: list[PlacedItem],
    analysis: RoomAnalysis,
    *,
    budget_cents: int | None = None,
    enforce_budget: bool = True,
) -> ValidationReport:
    """Run every rule. `budget_cents` of None skips H7.

    `enforce_budget` is False for manually edited layouts: 5.5 H7 applies to
    AI layouts only, and a user who deliberately adds an expensive sofa
    should see "over budget" rather than be blocked.
    """
    violations: list[Violation] = []
    per_item: dict[str, list[Violation]] = {item.id: [] for item in items}

    _check_containment(items, analysis, per_item)
    _check_item_overlaps(items, per_item)
    _check_obstacles(items, analysis, per_item)
    _check_door_keepouts(items, analysis, per_item)
    _check_height(items, analysis, per_item)
    _check_windows(items, analysis, per_item)
    _check_wall_facing(items, analysis, per_item)
    _check_oversized(items, analysis, per_item)
    _check_availability(items, per_item)
    min_gap = _check_category_clearances(items, per_item)

    grid, connected_pct, blocked_doors = _check_circulation(items, analysis)
    del grid
    if blocked_doors:
        violations.append(
            Violation(
                code="H6_CIRCULATION_BLOCKED",
                severity="hard",
                message=(
                    "You couldn't walk from " + _join(blocked_doors) + " to the rest of the room."
                ),
                related_ids=blocked_doors,
            )
        )
    elif connected_pct < int(MIN_CONNECTED_FRACTION * 100):
        violations.append(
            Violation(
                code="H6_CIRCULATION_BLOCKED",
                severity="hard",
                message=(
                    f"This layout cuts the room up: only {connected_pct}% of the "
                    "open floor can be reached in one piece."
                ),
                measured_mm=connected_pct,
                limit_mm=int(MIN_CONNECTED_FRACTION * 100),
            )
        )

    total_price = sum(item.price_cents for item in items)
    if budget_cents is not None and total_price > budget_cents:
        over_dollars = (total_price - budget_cents) // 100
        violations.append(
            Violation(
                code="H7_OVER_BUDGET",
                severity="hard" if enforce_budget else "soft",
                message=f"This layout is ${over_dollars:,} over your budget.",
            )
        )

    if min_gap is not None and min_gap < COMFORTABLE_WALKWAY_MM:
        # S1 is the comfort threshold; H6 already covers what is impassable.
        violations.append(
            Violation(
                code="S1_WALKWAY_TIGHT",
                severity="soft",
                message=(
                    f"The tightest gap between furniture is {_mm(min_gap)} mm. "
                    f"{COMFORTABLE_WALKWAY_MM} mm is more comfortable to walk through."
                ),
                measured_mm=_mm(min_gap),
                limit_mm=COMFORTABLE_WALKWAY_MM,
            )
        )

    report = ValidationReport(
        fits=True,
        violations=violations,
        items=per_item,
        min_walkway_mm=_mm(min_gap) if min_gap is not None else None,
        free_floor_pct=connected_pct,
        total_price_cents=total_price,
    )
    report.fits = not report.hard_violations
    return report


def _join(ids: list[str]) -> str:
    if len(ids) == 1:
        return f"the {ids[0]} doorway"
    return "the " + ", ".join(ids[:-1]) + f" and {ids[-1]} doorways"


def _check_containment(
    items: list[PlacedItem], analysis: RoomAnalysis, per_item: dict[str, list[Violation]]
) -> None:
    """H1: the whole footprint inside the floor polygon, within 5 mm."""
    for item in items:
        out = protrusion(item.footprint, analysis.floor)
        if out > TOLERANCE_MM:
            per_item[item.id].append(
                Violation(
                    code="H1_OUTSIDE_FLOOR",
                    severity="hard",
                    message=(f"The {_label(item)} sticks {_mm(out)} mm outside the room."),
                    measured_mm=_mm(out),
                    limit_mm=0,
                )
            )


def _check_item_overlaps(items: list[PlacedItem], per_item: dict[str, list[Violation]]) -> None:
    """H2: no 3D overlap, except that anything may sit on a floor covering.

    Two rugs are not allowed to overlap each other, which is why the exception
    is "one of them is a floor covering" rather than "either is".
    """
    for index, first in enumerate(items):
        for second in items[index + 1 :]:
            if first.is_floor_covering and second.is_floor_covering:
                pass  # rug on rug: not allowed, fall through to the check
            elif first.is_floor_covering or second.is_floor_covering:
                continue
            if not first.footprint.height_overlaps(second.footprint):
                continue
            overlap = separating_axis_overlap(first.footprint, second.footprint)
            if overlap > TOLERANCE_MM:
                message = (
                    f"The {_label(first)} and the {_label(second)} overlap by {_mm(overlap)} mm."
                )
                per_item[first.id].append(
                    Violation(
                        code="H2_ITEM_OVERLAP",
                        severity="hard",
                        message=message,
                        measured_mm=_mm(overlap),
                        limit_mm=0,
                        related_ids=[second.id],
                    )
                )
                per_item[second.id].append(
                    Violation(
                        code="H2_ITEM_OVERLAP",
                        severity="hard",
                        message=message,
                        measured_mm=_mm(overlap),
                        limit_mm=0,
                        related_ids=[first.id],
                    )
                )


def _check_obstacles(
    items: list[PlacedItem], analysis: RoomAnalysis, per_item: dict[str, list[Violation]]
) -> None:
    """H3: nothing overlaps a fixed obstacle or an object the user kept."""
    for item in items:
        for obstacle in analysis.obstacles:
            if not item.footprint.height_overlaps(obstacle.footprint):
                continue
            overlap = separating_axis_overlap(item.footprint, obstacle.footprint)
            if overlap > TOLERANCE_MM:
                per_item[item.id].append(
                    Violation(
                        code="H3_OBSTACLE_OVERLAP",
                        severity="hard",
                        message=(
                            f"The {_label(item)} runs {_mm(overlap)} mm into the {obstacle.label}."
                        ),
                        measured_mm=_mm(overlap),
                        limit_mm=0,
                        related_ids=[obstacle.id],
                    )
                )


def _check_door_keepouts(
    items: list[PlacedItem], analysis: RoomAnalysis, per_item: dict[str, list[Violation]]
) -> None:
    """H4: nothing but a floor covering inside a door's swing and approach.

    The keep-out is an oriented rectangle, so this is the same separating-axis
    test used everywhere else rather than a polygon intersection. That is what
    lets the TypeScript validator return the identical answer (5.5 parity)
    without carrying a polygon clipper into the browser.
    """
    for item in items:
        if item.is_floor_covering:
            continue
        for opening_id, keepout in analysis.door_keepouts.items():
            if keepout.width_mm <= 0:
                continue
            if separating_axis_overlap(item.footprint, keepout) <= TOLERANCE_MM:
                continue
            per_item[item.id].append(
                Violation(
                    code="H4_DOOR_KEEPOUT",
                    severity="hard",
                    message=f"The {_label(item)} is in the way of the door.",
                    related_ids=[opening_id],
                )
            )


def _check_height(
    items: list[PlacedItem], analysis: RoomAnalysis, per_item: dict[str, list[Violation]]
) -> None:
    """H5: height at most the ceiling less 50 mm."""
    limit = analysis.ceiling_height_mm - CEILING_CLEARANCE_MM
    for item in items:
        if item.footprint.top_mm > limit + TOLERANCE_MM:
            per_item[item.id].append(
                Violation(
                    code="H5_TOO_TALL",
                    severity="hard",
                    message=(
                        f"The {_label(item)} is {_mm(item.footprint.top_mm)} mm tall "
                        f"and the ceiling is {_mm(analysis.ceiling_height_mm)} mm."
                    ),
                    measured_mm=_mm(item.footprint.top_mm),
                    limit_mm=_mm(limit),
                )
            )


def _check_windows(
    items: list[PlacedItem], analysis: RoomAnalysis, per_item: dict[str, list[Violation]]
) -> None:
    """S3: a tall item standing in a window's zone blocks the view.

    Height is measured against the sill, so a sideboard under a high window
    is fine and a bookcase in front of it is not.
    """
    by_id = {opening.id: opening for opening in analysis.openings}
    for item in items:
        for opening_id, zone in analysis.window_zones.items():
            opening = by_id.get(opening_id)
            if opening is None or zone.width_mm <= 0:
                continue
            if item.footprint.top_mm <= opening.sill_mm - 50.0:
                continue
            if separating_axis_overlap(item.footprint, zone) <= TOLERANCE_MM:
                continue
            per_item[item.id].append(
                Violation(
                    code="S3_WINDOW_BLOCKED",
                    severity="soft",
                    message=f"The {_label(item)} blocks the window.",
                    measured_mm=_mm(item.footprint.top_mm),
                    limit_mm=_mm(opening.sill_mm),
                    related_ids=[opening_id],
                )
            )


def _check_wall_facing(
    items: list[PlacedItem], analysis: RoomAnalysis, per_item: dict[str, list[Violation]]
) -> None:
    """S4: an against-wall item whose back is not to the wall.

    A sofa rotated 180 degrees still satisfies every hard rule -- it is inside
    the room, it overlaps nothing -- and is obviously wrong to a person. This
    is the rule that catches it.
    """
    for item in items:
        if item.against_wall_id is None:
            continue
        wall = analysis.wall(item.against_wall_id)
        if wall is None:
            continue
        inward = wall.inward_normal()
        facing = item.footprint.front_normal()
        alignment = facing[0] * inward[0] + facing[1] * inward[1]
        if alignment < 0.3:
            per_item[item.id].append(
                Violation(
                    code="S4_BACK_NOT_TO_WALL",
                    severity="soft",
                    message=f"The {_label(item)} is facing the wall rather than the room.",
                    related_ids=[wall.id],
                )
            )


def _check_oversized(
    items: list[PlacedItem], analysis: RoomAnalysis, per_item: dict[str, list[Violation]]
) -> None:
    """S5: an item out of proportion with the room it is in."""
    min_x, min_z, max_x, max_z = analysis.floor.bounds
    room_span = max(max_x - min_x, max_z - min_z)
    for item in items:
        widest = max(item.footprint.width_mm, item.footprint.depth_mm)
        if room_span > 0 and widest > room_span * MAX_ROOM_FRACTION:
            per_item[item.id].append(
                Violation(
                    code="S5_OVERSIZED_FOR_ROOM",
                    severity="soft",
                    message=(
                        f"The {_label(item)} is {_mm(widest)} mm across in a room "
                        f"{_mm(room_span)} mm wide."
                    ),
                    measured_mm=_mm(widest),
                    limit_mm=_mm(room_span * MAX_ROOM_FRACTION),
                )
            )
            continue

        if item.against_wall_id is None:
            continue
        runs = analysis.runs_for(item.against_wall_id)
        longest = max((run.length_mm for run in runs), default=0.0)
        if longest > 0 and item.footprint.width_mm > longest * MAX_WALL_RUN_FRACTION:
            per_item[item.id].append(
                Violation(
                    code="S5_OVERSIZED_FOR_ROOM",
                    severity="soft",
                    message=(
                        f"The {_label(item)} takes up most of that wall "
                        f"({_mm(item.footprint.width_mm)} mm of {_mm(longest)} mm)."
                    ),
                    measured_mm=_mm(item.footprint.width_mm),
                    limit_mm=_mm(longest * MAX_WALL_RUN_FRACTION),
                )
            )


def _check_availability(items: list[PlacedItem], per_item: dict[str, list[Violation]]) -> None:
    """S6: the product is gone, or its price has not been checked lately."""
    for item in items:
        if not item.available:
            per_item[item.id].append(
                Violation(
                    code="S6_PRICE_STALE_OR_UNAVAILABLE",
                    severity="soft",
                    message=f"The {_label(item)} is no longer available.",
                )
            )
        elif item.price_age_days > STALE_PRICE_DAYS:
            per_item[item.id].append(
                Violation(
                    code="S6_PRICE_STALE_OR_UNAVAILABLE",
                    severity="soft",
                    message=(
                        f"The price for the {_label(item)} was last checked "
                        f"{item.price_age_days} days ago."
                    ),
                )
            )


def _check_category_clearances(
    items: list[PlacedItem], per_item: dict[str, list[Violation]]
) -> float | None:
    """S2: the per-category clearances from `category_rules.yaml`.

    Returns the tightest gap found between any two items, which the caller
    reports as `min_walkway_mm` and tests against S1.
    """
    tightest: float | None = None
    for index, first in enumerate(items):
        for second in items[index + 1 :]:
            if first.is_floor_covering or second.is_floor_covering:
                continue
            if not first.footprint.height_overlaps(second.footprint):
                continue
            gap = gap_between(first.footprint, second.footprint)
            if gap <= 0:
                continue  # an overlap; H2 has it
            tightest = gap if tightest is None else min(tightest, gap)

            required = max(first.rule.front_clearance_mm, second.rule.front_clearance_mm)
            if required > 0 and gap < required:
                message = (
                    f"Only {_mm(gap)} mm between the {_label(first)} and the "
                    f"{_label(second)}; {_mm(required)} mm is the usual minimum."
                )
                per_item[first.id].append(
                    Violation(
                        code="S2_CATEGORY_CLEARANCE",
                        severity="soft",
                        message=message,
                        measured_mm=_mm(gap),
                        limit_mm=_mm(required),
                        related_ids=[second.id],
                    )
                )
    return tightest


def _check_circulation(
    items: list[PlacedItem], analysis: RoomAnalysis
) -> tuple[CirculationGrid, int, list[str]]:
    """H6: every door reachable, and most of the free floor connected.

    Floor coverings do not block: you can walk on a rug.
    """
    blocking = [item.footprint for item in items if not item.is_floor_covering] + [
        obstacle.footprint for obstacle in analysis.obstacles
    ]
    grid = circulation_grid(analysis.floor, blocking, base=analysis.base_grid)

    total = grid.walkable_cells
    if total == 0:
        return grid, 0, []

    doors = [
        (opening, analysis.wall(opening.wall_id))
        for opening in analysis.openings
        if opening.type in ("door", "passage")
    ]
    seeds: list[tuple[str, tuple[int, int]]] = []
    for opening, wall in doors:
        if wall is None:
            continue
        x, z = door_standing_point(wall, opening)
        seeds.append((opening.id, grid.cell_of(x, z)))

    if not seeds:
        # No door to start from. Use the largest reachable region so the
        # measurement still means something.
        largest = _largest_region(grid)
        return grid, 100 * largest // total, []

    reachable = connected_component(grid, seeds[0][1])
    blocked = [opening_id for opening_id, cell in seeds[1:] if not _cell_in(reachable, cell)]
    # A door whose own standing point is walled in is blocked too.
    if not _cell_in(grid.walkable, seeds[0][1]):
        blocked.insert(0, seeds[0][0])

    return grid, 100 * int(reachable.sum()) // total, blocked


def _cell_in(mask: np.ndarray, cell: tuple[int, int]) -> bool:
    rows, columns = mask.shape
    return bool(0 <= cell[0] < rows and 0 <= cell[1] < columns and mask[cell])


def _largest_region(grid: CirculationGrid) -> int:
    seen = np.zeros_like(grid.walkable)
    largest = 0
    for row, column in zip(*np.nonzero(grid.walkable), strict=False):
        if seen[row, column]:
            continue
        region = connected_component(grid, (int(row), int(column)))
        seen |= region
        largest = max(largest, int(region.sum()))
    return largest


def _label(item: PlacedItem) -> str:
    """The category as a person would say it: `coffee_table` -> `coffee table`."""
    return item.category.replace("_", " ")


def walkway_between(a: Footprint, b: Footprint) -> float:
    """Exposed for the solver's scoring, which wants the same number S1 uses."""
    return gap_between(a, b)


def bounding_span(polygon: Polygon) -> float:
    min_x, min_z, max_x, max_z = polygon.bounds
    return math.hypot(max_x - min_x, max_z - min_z)
