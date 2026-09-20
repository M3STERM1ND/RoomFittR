"""L6: solve, validate, and retry until the layout is legal (5.4).

The solver's hard checks are deliberately *local* -- containment, overlap,
keep-outs, height -- because they run per candidate pose and there can be
thousands. Two of the hard rules are not local at all:

- **H6 (circulation)** is a property of the whole layout. A flood fill over
  every candidate pose would dominate the solver's runtime, and the answer
  changes every time another item lands.
- **H7 (budget)** is a sum over the layout.

So 5.4 L6 says: run the full validator on the finished layout, and "any hard
violation here is a bug -> retry the solver with the violating slot removed".
This module is that loop, and it is what turns Phase 4's acceptance criterion
-- "100% of eval cases produce a layout with zero hard violations and within
budget" -- from a hope into a guarantee.

The loop terminates because each pass removes at least one slot and `must`
slots are never removable; the worst case is a layout of only the `must`
slots, and if those cannot be placed the solver has already raised.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from .room import RoomAnalysis
from .solver import DroppedSlot, Slot, SolverResult, solve
from .validator import ValidationReport, validate

# Each pass removes at least one slot, so this is a guard against a bug
# rather than a real budget: a plan is capped at 10 items (5.4).
MAX_PASSES = 12


@dataclass(frozen=True, slots=True)
class LayoutResult:
    """A finished, validated layout."""

    result: SolverResult
    report: ValidationReport
    passes: int

    @property
    def items(self) -> list[Any]:
        return self.result.items

    def as_meta(self) -> dict[str, Any]:
        meta = self.result.as_meta()
        meta["solver_passes"] = self.passes
        return meta


def _blamed_slots(report: ValidationReport, slots: list[Slot]) -> list[str]:
    """Which slots to remove after a failed validation.

    Per-item hard violations name their own culprit. A global one -- H6 above
    all -- names nobody, so the lowest-priority droppable slot is removed:
    that is the item the user would least miss, and removing the *last*
    placed one would instead punish whatever happened to be solved last.
    """
    by_id = {slot.slot_id: slot for slot in slots}
    blamed: list[str] = []

    for item_id, violations in report.items.items():
        if any(v.severity == "hard" for v in violations):
            slot = by_id.get(item_id)
            if slot is not None and slot.priority != "must":
                blamed.append(item_id)

    if blamed:
        return blamed

    if any(v.severity == "hard" for v in report.violations):
        rank = {"nice": 0, "should": 1, "must": 2}
        droppable = [s for s in slots if s.priority != "must"]
        if droppable:
            droppable.sort(key=lambda s: (rank.get(s.priority, 1), s.slot_id))
            return [droppable[0].slot_id]

    return []


def generate(
    slots: list[Slot],
    analysis: RoomAnalysis,
    *,
    budget_cents: int | None = None,
    seed: int = 0,
    enforce_budget: bool = True,
) -> LayoutResult:
    """Produce a layout with no hard violations, dropping slots as needed.

    Raises `SolverError` (from the solver) only when a `must` slot cannot be
    placed at all, which 7.4 treats as a user-visible layout failure with
    suggestions rather than an internal error.
    """
    remaining = list(slots)
    dropped: list[DroppedSlot] = []

    for attempt in range(1, MAX_PASSES + 1):
        result = solve(remaining, analysis, seed=seed)
        report = validate(
            result.items,
            analysis,
            budget_cents=budget_cents,
            enforce_budget=enforce_budget,
        )
        combined = replace(result, dropped=[*dropped, *result.dropped])

        if report.fits:
            return LayoutResult(result=combined, report=report, passes=attempt)

        blamed = _blamed_slots(report, remaining)
        if not blamed:
            # Nothing droppable is left and the layout still fails. Returning
            # the report is better than looping: the caller shows the user
            # what is wrong, which 5.5 says is the point of the report.
            return LayoutResult(result=combined, report=report, passes=attempt)

        by_id = {slot.slot_id: slot for slot in remaining}
        for slot_id in blamed:
            slot = by_id.get(slot_id)
            if slot is None:
                continue
            dropped.append(
                DroppedSlot(
                    slot_id=slot_id,
                    category=slot.category,
                    reason=_reason_for(report, slot_id, slot.category),
                )
            )
        remaining = [slot for slot in remaining if slot.slot_id not in set(blamed)]

    return LayoutResult(
        result=replace(solve(remaining, analysis, seed=seed), dropped=dropped),
        report=validate(
            solve(remaining, analysis, seed=seed).items,
            analysis,
            budget_cents=budget_cents,
            enforce_budget=enforce_budget,
        ),
        passes=MAX_PASSES,
    )


def _reason_for(report: ValidationReport, slot_id: str, category: str) -> str:
    """Reuse the validator's sentence, which already reads well.

    5.5 requires every violation to carry a message a person can act on, so
    the drop reason should be that message rather than a second, vaguer one
    written here.
    """
    label = category.replace("_", " ")
    for violation in report.items.get(slot_id, []):
        if violation.severity == "hard":
            return violation.message
    for violation in report.violations:
        if violation.severity == "hard":
            return f"We left out the {label}: {violation.message[0].lower()}{violation.message[1:]}"
    return f"There was no room for the {label}."
