"""L2: ask Claude for the plan (implementation-plan.md 5.4).

One call. Its input is 5.2's compact room summary plus the things the model
cannot see -- the budget, the category vocabulary priced against the live
catalog, and the default ratios for the room type. Its output is a
`LayoutPlan`: what furniture, how the money splits, and which wall or which
other slot each thing relates to.

What it is *not* allowed to decide is anything that could break the room.
Decision 4 draws that line and 5.1 tabulates it; the code that enforces it
is `planner.post_check`, which runs over whatever comes back here.

5.1's fallback -- "fails, times out, returns an invalid schema twice" --
is implemented as two attempts, the second told what was wrong with the
first. A third would cost another timeout to arrive at the template the
caller can have immediately.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from roomfittr_layout.planner import LayoutPlan, Product, room_types, template_plan
from roomfittr_layout.room import RoomAnalysis, llm_summary

from .client import PLANNER_MODEL, PLANNER_TIMEOUT_S, Transport, TransportError
from .cost import Spend
from .schemas import parse_plan, plan_tool

ATTEMPTS = 2
MAX_TOKENS = 8000

SYSTEM = """\
You are the layout planner for RoomFittr. You are given one scanned room and \
a budget, and you decide what furniture goes in it.

You do not place anything. A deterministic solver turns your plan into \
coordinates and a deterministic validator is the final authority on whether \
a layout is legal, so you never give positions, rotations or sizes. What you \
give is: which categories the room should have, how important each one is, \
how the budget divides between them, and which wall or which other item each \
one belongs to.

How to think about it:

- Anchor the room first. The largest item -- the sofa, the bed, the dining \
table, the desk -- decides everything else, so give it the best wall and the \
largest share, and list it before the things that depend on it.
- Use the free runs. A wall's usable length is the free run, not the wall \
length; an anchor wider than every free run has nowhere to go.
- Spend the budget where it shows. A room with one good sofa and a cheap rug \
reads better than one with six mediocre things.
- Mark as "must" only what makes the room that kind of room. A "must" is \
never dropped to fit the budget or the space, so an over-used "must" means a \
layout that fails instead of one that is merely smaller.
- Refer to walls and openings only by the ids you were given. An id you \
invent is discarded and the item loses its preferred wall.
- Prefer fewer, better-related items to filling every slot you are allowed.

When you have decided, call the submit_layout_plan tool. Reply with the tool \
call and nothing else.\
"""


@dataclass(frozen=True, slots=True)
class PlanAttempt:
    """What came back, what it cost, and why it fell back if it did."""

    plan: LayoutPlan
    spend: Spend
    failure: str = ""

    @property
    def from_model(self) -> bool:
        return not self.plan.from_template


def price_percentiles(catalog: list[Product]) -> dict[str, dict[str, int]]:
    """5.4 L2's "price percentiles per category from the live catalog".

    Without these the model is dividing a budget it cannot price: a 12%
    share of EUR 2000 sounds generous until the cheapest sofa in the catalog
    is EUR 400. p10 is the one that matters -- it is the number
    `post_check` drops a slot against -- so it is given alongside the median
    and p90 rather than a bare average.
    """
    by_category: dict[str, list[int]] = {}
    for product in catalog:
        by_category.setdefault(product.category, []).append(product.price_cents)

    out: dict[str, dict[str, int]] = {}
    for category, prices in by_category.items():
        prices.sort()
        out[category] = {
            "p10": _percentile(prices, 10),
            "p50": _percentile(prices, 50),
            "p90": _percentile(prices, 90),
            "n": len(prices),
        }
    return out


def _percentile(sorted_prices: list[int], percentile: int) -> int:
    """Nearest-rank, so every answer is a price that exists in the catalog.

    Interpolating would invent a price no product has, and these numbers are
    shown to the model as what it can buy.
    """
    if not sorted_prices:
        return 0
    rank = max(1, (percentile * len(sorted_prices) + 99) // 100)
    return sorted_prices[min(rank, len(sorted_prices)) - 1]


def default_ratios(room_type: str) -> dict[str, float]:
    """The template's shares, as 5.4's "default budget ratios for the room type"."""
    return {slot.category: round(slot.budget_share, 3) for slot in template_plan(room_type).slots}


def build_user_message(
    analysis: RoomAnalysis,
    *,
    room_type: str,
    budget_cents: int,
    currency: str,
    catalog: list[Product],
    style: str | None = None,
) -> str:
    summary = llm_summary(analysis, room_type_guess=room_type)
    percentiles = price_percentiles(catalog)
    payload = {
        "room": summary,
        "room_type": room_type,
        "budget": {
            "currency": currency,
            "total_cents": budget_cents,
            "note": (
                "5% is held back for price movement, so your shares are scaled to 95% of this."
            ),
        },
        "style_preference": style,
        "catalog_prices_cents": percentiles,
        "default_budget_ratios": default_ratios(room_type),
    }
    return (
        "Plan this room.\n\n"
        f"{json.dumps(payload, indent=2, sort_keys=True)}\n\n"
        "Only the categories listed in catalog_prices_cents have stock; a "
        "category absent from it will find nothing to buy. Call "
        "submit_layout_plan with your plan."
    )


def propose_plan(
    transport: Transport,
    analysis: RoomAnalysis,
    *,
    room_type: str,
    budget_cents: int,
    catalog: list[Product],
    currency: str = "EUR",
    style: str | None = None,
    model: str = PLANNER_MODEL,
) -> PlanAttempt:
    """5.4 L2. Never raises: a failure returns the template for the room type."""
    tool = plan_tool(room_types())
    user = build_user_message(
        analysis,
        room_type=room_type,
        budget_cents=budget_cents,
        currency=currency,
        catalog=catalog,
        style=style,
    )
    spend = Spend()
    last_failure = ""

    for attempt in range(ATTEMPTS):
        message = user if attempt == 0 else f"{user}\n\n{_correction(last_failure)}"
        try:
            result = transport.call_tool(
                model=model,
                system=SYSTEM,
                user=message,
                tool=tool,
                max_tokens=MAX_TOKENS,
                timeout_s=PLANNER_TIMEOUT_S,
                think=True,
            )
        except TransportError as error:
            last_failure = error.kind
            continue

        spend = spend.plus(result.usage)
        plan = parse_plan(result.arguments)
        if not plan.slots:
            last_failure = "empty_plan"
            continue
        # The style the user asked for outranks the one the model inferred:
        # 5.1 gives style direction to "User choice (optional) or LLM
        # inference", in that order.
        if style:
            plan = LayoutPlan(
                room_type=plan.room_type,
                slots=plan.slots,
                style=style,
                rationale=plan.rationale,
                from_template=False,
            )
        return PlanAttempt(plan=plan, spend=spend)

    return PlanAttempt(
        plan=template_plan(room_type),
        spend=spend,
        failure=last_failure or "unknown",
    )


def _correction(failure: str) -> str:
    if failure == "empty_plan":
        return (
            "Your previous answer contained no usable slots. Every slot needs a "
            "category from the allowed list. Try again."
        )
    return "The previous attempt did not produce a usable plan. Try again."
