"""L2 to L6 end to end (implementation-plan.md 5.4).

The order is the plan's: ask for a plan, check it deterministically,
shortlist against the catalog, ask which products go together, settle the
budget, solve, validate, retry. What this module adds over the layout
package is the two Claude calls and -- more importantly -- the guarantee
that neither of them can stop a layout being produced.

5.1: "If the LLM call fails, times out, returns an invalid schema twice, or
**its plan cannot be solved**, the engine uses a rule-based template plan."
The first three are handled inside `propose_plan`. The fourth cannot be:
whether a plan is solvable is only knowable after solving it, so it is
handled here, by solving again with the template. That second attempt is the
reason `generate_layout` cannot raise where the deterministic engine could.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from roomfittr_layout.engine import LayoutResult, generate
from roomfittr_layout.planner import (
    LayoutPlan,
    Product,
    post_check,
    shortlist,
    template_plan,
    to_solver_slots,
)
from roomfittr_layout.planner import (
    pick as choose_products,
)
from roomfittr_layout.room import RoomAnalysis
from roomfittr_layout.solver import SolverError

from .client import Transport
from .cost import Spend
from .pick import pick_products
from .plan import propose_plan


@dataclass(frozen=True, slots=True)
class PlannedLayout:
    """A finished layout plus the provenance of every decision behind it."""

    layout: LayoutResult
    plan: LayoutPlan
    spend: Spend = field(default_factory=Spend)
    plan_failure: str = ""
    pick_failure: str = ""
    rejected_picks: tuple[str, ...] = ()
    solved_from_template: bool = False

    def as_meta(self) -> dict[str, Any]:
        """6.2's `generation_meta`.

        Written so that a layout a person scored 1 out of 5 can be traced to
        whichever component chose it. Phase 4's human eval is the test of the
        solver's scoring weights, and it is worthless if a bad score cannot
        be attributed to the template, the model or the solver.
        """
        meta = self.layout.as_meta()
        meta.update(self.spend.as_meta())
        meta["plan_source"] = "template" if self.plan.from_template else "llm"
        meta["plan_rationale"] = self.plan.rationale
        if self.plan_failure:
            meta["plan_failure"] = self.plan_failure
        if self.pick_failure:
            meta["pick_failure"] = self.pick_failure
        if self.rejected_picks:
            meta["rejected_picks"] = list(self.rejected_picks)
        if self.solved_from_template:
            meta["solved_from_template"] = True
        return meta


def generate_layout(
    analysis: RoomAnalysis,
    catalog: list[Product],
    *,
    room_type: str,
    budget_cents: int,
    transport: Transport | None = None,
    currency: str = "EUR",
    style: str | None = None,
    seed: int = 0,
    enforce_budget: bool = True,
) -> PlannedLayout:
    """Produce a layout for this room.

    `transport=None` is the fully deterministic path: the template plan and
    the top-ranked product in each shortlist. It is not a degraded mode kept
    for emergencies -- it is what runs with no key, in CI, and in every test
    in this repo that is not specifically about the model.
    """
    spend = Spend()
    plan_failure = ""

    if transport is None:
        proposed = template_plan(room_type)
    else:
        attempt = propose_plan(
            transport,
            analysis,
            room_type=room_type,
            budget_cents=budget_cents,
            catalog=catalog,
            currency=currency,
            style=style,
        )
        proposed = attempt.plan
        plan_failure = attempt.failure
        for usage in attempt.spend.calls:
            spend = spend.plus(usage)

    built = _build(
        proposed,
        analysis,
        catalog,
        transport=transport,
        budget_cents=budget_cents,
        currency=currency,
        style=style,
        spend=spend,
    )

    try:
        layout = generate(
            built.slots,
            analysis,
            budget_cents=budget_cents,
            seed=seed,
            enforce_budget=enforce_budget,
        )
    except SolverError:
        # 5.1's fourth fallback. A model plan that cannot be solved is not a
        # failed layout -- it is a plan to discard. The template is known to
        # be solvable in a room that admits any layout at all, so if this
        # raises too, the room really has no layout and 7.4 wants that
        # reported to the user with suggestions.
        if built.plan.from_template:
            raise
        fallback = _build(
            template_plan(room_type),
            analysis,
            catalog,
            transport=None,
            budget_cents=budget_cents,
            currency=currency,
            style=style,
            spend=built.spend,
        )
        layout = generate(
            fallback.slots,
            analysis,
            budget_cents=budget_cents,
            seed=seed,
            enforce_budget=enforce_budget,
        )
        return PlannedLayout(
            layout=layout,
            plan=fallback.plan,
            spend=fallback.spend,
            plan_failure=plan_failure or "unsolvable_plan",
            pick_failure=built.pick_failure,
            rejected_picks=built.rejected,
            solved_from_template=True,
        )

    return PlannedLayout(
        layout=layout,
        plan=built.plan,
        spend=built.spend,
        plan_failure=plan_failure,
        pick_failure=built.pick_failure,
        rejected_picks=built.rejected,
    )


@dataclass(frozen=True, slots=True)
class _Built:
    plan: LayoutPlan
    slots: list[Any]
    spend: Spend
    pick_failure: str = ""
    rejected: tuple[str, ...] = ()


def _build(
    proposed: LayoutPlan,
    analysis: RoomAnalysis,
    catalog: list[Product],
    *,
    transport: Transport | None,
    budget_cents: int,
    currency: str,
    style: str | None,
    spend: Spend,
) -> _Built:
    """Check the plan, shortlist against the catalog, then choose (L2 post-checks, L3, L4)."""
    minimums = _cheapest_per_category(catalog)
    checked = post_check(
        proposed, analysis, budget_cents=budget_cents, min_price_by_category=minimums
    )

    shortlists = {
        slot.slot_id: shortlist(slot, catalog, analysis, budget_cents=budget_cents, style=style)
        for slot in checked.slots
    }

    preferred: dict[str, str] = {}
    pick_failure = ""
    rejected: tuple[str, ...] = ()
    if transport is not None:
        picked = pick_products(
            transport,
            checked,
            shortlists,
            budget_cents=budget_cents,
            currency=currency,
            style=style,
        )
        preferred = picked.preferred
        pick_failure = picked.failure
        rejected = picked.rejected
        for usage in picked.spend.calls:
            spend = spend.plus(usage)

    ordered = choose_products(checked, shortlists, budget_cents=budget_cents, preferred=preferred)
    return _Built(
        plan=checked,
        slots=to_solver_slots(checked, ordered),
        spend=spend,
        pick_failure=pick_failure,
        rejected=rejected,
    )


def _cheapest_per_category(catalog: list[Product]) -> dict[str, int]:
    minimums: dict[str, int] = {}
    for product in catalog:
        current = minimums.get(product.category)
        if current is None or product.price_cents < current:
            minimums[product.category] = product.price_cents
    return minimums
