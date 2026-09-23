"""L2: the plan, and the deterministic half of it (implementation-plan.md 5.4).

Decision 4 splits the work: the LLM decides *what* and *roughly where*, and
deterministic code decides everything that could break a room. This module is
the deterministic code around the LLM call:

- **Template plans** per room type. 5.4's fallback: "If the LLM call fails,
  times out, returns an invalid schema twice, or its plan cannot be solved,
  the engine uses a rule-based template plan". A layout is always produced,
  so the templates are not a degraded mode kept for emergencies -- they are
  the floor the product stands on, and they are what runs in every test.
- **Post-checks** over whatever the LLM returns. 5.4 lists them: categories
  in the vocabulary, shares renormalised with a reserve, each share clamped
  to something the catalog can actually supply, intents referencing only
  real ids, slots capped at ten.

The LLM call itself is not here. It needs an API key and a network, and
keeping it out means every rule in this file is testable without either --
which is also what makes 9.1's "LLM chaos tests" possible: feed
`post_check` invalid JSON, nonexistent ids and absurd budget shares, and
assert a valid plan still comes out.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

from .room import RoomAnalysis
from .rules import CATEGORY_RULES, CATEGORY_VOCABULARY, CategoryRule
from .solver import Candidate, Intent, Slot

SCHEMA_VERSION = 1

# 5.4: "Slot counts are capped (<= 10 items in V1)".
MAX_SLOTS = 10

# 5.4: "Budget shares are renormalized to 100% with a 5% unallocated reserve."
# The reserve is not padding -- prices move between the shortlist and the
# checkout, and a layout that spends the budget to the cent is over it by the
# time the user clicks through.
BUDGET_RESERVE = 0.05

# 5.4: a slot whose allocation cannot buy the cheapest thing in its category
# is demoted or dropped. Without the catalog to hand we fall back to this
# floor so a share of essentially zero never survives.
MIN_SHARE = 0.01

PRIORITY_ORDER = {"must": 0, "should": 1, "nice": 2}


@dataclass(frozen=True, slots=True)
class PlanSlot:
    """One slot of a `LayoutPlan`: a category, how much it may spend, and where."""

    slot_id: str
    category: str
    priority: str
    budget_share: float
    anchor_wall: str | None = None
    anchor_slot: str | None = None
    notes: str = ""

    def allocation_cents(self, budget_cents: int) -> int:
        return int(budget_cents * self.budget_share)


@dataclass(frozen=True, slots=True)
class LayoutPlan:
    """What L2 produces. Serialises to the `LayoutPlan` schema."""

    room_type: str
    slots: list[PlanSlot]
    style: str | None = None
    rationale: str = ""
    # True when this came from a template rather than the model, which 6.2
    # records in `generation_meta` so a low human-eval score can be traced to
    # whichever produced it.
    from_template: bool = False

    def as_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "room_type": self.room_type,
            "slots": [
                {
                    "slot_id": slot.slot_id,
                    "category": slot.category,
                    "priority": slot.priority,
                    "budget_share": round(slot.budget_share, 4),
                    **(
                        {
                            "intent": {
                                "anchor": _anchor_json(slot),
                                **({"notes": slot.notes} if slot.notes else {}),
                            }
                        }
                        if slot.anchor_wall or slot.anchor_slot or slot.notes
                        else {}
                    ),
                }
                for slot in self.slots
            ],
        }
        if self.style:
            payload["style"] = self.style
        if self.rationale:
            payload["rationale"] = self.rationale
        return payload


def _anchor_json(slot: PlanSlot) -> dict[str, str] | None:
    if slot.anchor_wall:
        return {"kind": "wall", "ref": slot.anchor_wall}
    if slot.anchor_slot:
        return {"kind": "slot", "ref": slot.anchor_slot}
    return None


# 5.4's "default budget ratios for the room type". Shares are relative and
# get renormalised, so they read as proportions rather than as percentages
# that have to add up.
_TEMPLATES: dict[str, list[tuple[str, str, float, str | None]]] = {
    "living": [
        ("sofa", "must", 0.34, None),
        ("coffee_table", "should", 0.12, "sofa"),
        ("rug", "should", 0.12, "sofa"),
        ("tv_stand", "should", 0.14, None),
        ("armchair", "nice", 0.16, None),
        ("floor_lamp", "nice", 0.06, "sofa"),
        ("bookshelf", "nice", 0.06, None),
    ],
    "bedroom": [
        ("bed_frame", "must", 0.42, None),
        ("nightstand", "should", 0.10, "bed_frame"),
        ("dresser", "should", 0.24, None),
        ("rug", "nice", 0.12, "bed_frame"),
        ("floor_lamp", "nice", 0.06, None),
        ("armchair", "nice", 0.06, None),
    ],
    "office": [
        ("desk", "must", 0.32, None),
        ("office_chair", "must", 0.24, "desk"),
        ("bookshelf", "should", 0.20, None),
        ("rug", "nice", 0.12, None),
        ("floor_lamp", "nice", 0.06, None),
        ("armchair", "nice", 0.06, None),
    ],
    "dining": [
        ("dining_table", "must", 0.40, None),
        ("dining_chair", "must", 0.26, "dining_table"),
        ("rug", "should", 0.14, "dining_table"),
        ("cabinet", "nice", 0.14, None),
        ("floor_lamp", "nice", 0.06, None),
    ],
}

# An unrecognised room type gets the living-room template. It is the most
# generally useful arrangement and every category in it is common; refusing
# to produce a plan would violate 5.4's "a layout is always produced".
DEFAULT_ROOM_TYPE = "living"


def template_plan(room_type: str) -> LayoutPlan:
    """5.4's rule-based fallback plan for a room type."""
    key = room_type if room_type in _TEMPLATES else DEFAULT_ROOM_TYPE
    rows = _TEMPLATES[key]
    slot_id_for_category = {category: f"S{index + 1}" for index, (category, *_) in enumerate(rows)}
    slots = [
        PlanSlot(
            slot_id=f"S{index + 1}",
            category=category,
            priority=priority,
            budget_share=share,
            anchor_slot=slot_id_for_category.get(anchor) if anchor else None,
        )
        for index, (category, priority, share, anchor) in enumerate(rows)
    ]
    return LayoutPlan(
        room_type=key,
        slots=slots,
        rationale=f"A standard {key.replace('_', ' ')} arrangement.",
        from_template=True,
    )


def room_types() -> list[str]:
    return sorted(_TEMPLATES)


def post_check(
    plan: LayoutPlan,
    analysis: RoomAnalysis,
    *,
    budget_cents: int | None = None,
    min_price_by_category: dict[str, int] | None = None,
) -> LayoutPlan:
    """5.4's deterministic post-checks. Always returns a usable plan.

    Never raises. Everything here exists because the input may be an LLM's
    best guess: a category that does not exist, a wall id from a different
    room, twenty slots, shares summing to three. The output is a plan the
    solver can run, or -- if nothing survives -- the template for the room
    type, because 5.4 says a layout is always produced.
    """
    wall_ids = {wall.id for wall in analysis.walls}
    kept: list[PlanSlot] = []
    seen_ids: set[str] = set()

    for slot in plan.slots:
        if slot.category not in CATEGORY_VOCABULARY:
            continue
        if slot.slot_id in seen_ids:
            continue
        share = slot.budget_share
        if not (isinstance(share, int | float) and math.isfinite(share) and share > 0):
            # `isfinite` rather than a NaN check alone: infinity passes
            # `share > 0` and `share == share`, and then the renormalisation
            # divides by an infinite total and sets *every* share to zero --
            # so one bad number from the model silently zeroes the budget.
            share = MIN_SHARE
        seen_ids.add(slot.slot_id)
        kept.append(
            replace(
                slot,
                budget_share=float(share),
                # An anchor naming a wall that is not in this room is worse
                # than no anchor: the solver would relax away from a perfectly
                # good wall it was never told about. Slot anchors are resolved
                # afterwards, over the whole set, so a slot may legitimately
                # reference one listed after it.
                anchor_wall=slot.anchor_wall if slot.anchor_wall in wall_ids else None,
            )
        )

    kept = _resolve_anchors(kept)
    kept = _cap_slots(kept)
    kept = _renormalise(kept)
    if budget_cents is not None and min_price_by_category:
        kept = _drop_unaffordable(kept, budget_cents, min_price_by_category)
        kept = _renormalise(kept)

    if not kept:
        return template_plan(plan.room_type)
    return replace(plan, slots=kept)


def _resolve_anchors(slots: list[PlanSlot]) -> list[PlanSlot]:
    """Drop anchors pointing at slots that are not in the plan.

    Done after the vocabulary filter, because a slot may have been removed
    for an unrelated reason and its dependents would then point at nothing.
    A dangling anchor is not an error -- the slot simply places freely.
    """
    present = {slot.slot_id for slot in slots}
    return [
        replace(slot, anchor_slot=slot.anchor_slot if slot.anchor_slot in present else None)
        for slot in slots
    ]


def _cap_slots(slots: list[PlanSlot]) -> list[PlanSlot]:
    """5.4: at most ten slots, highest priority first.

    Ties keep the plan's own order, so a model that listed the sofa before
    the armchair gets the sofa. `sorted` is stable, which is what makes that
    true without a tie-breaker.
    """
    if len(slots) <= MAX_SLOTS:
        return slots
    ordered = sorted(slots, key=lambda slot: PRIORITY_ORDER.get(slot.priority, 1))
    surviving = {slot.slot_id for slot in ordered[:MAX_SLOTS]}
    return [slot for slot in slots if slot.slot_id in surviving]


def _renormalise(slots: list[PlanSlot]) -> list[PlanSlot]:
    """Scale shares to sum to 1 - reserve, whatever they summed to before."""
    total = sum(slot.budget_share for slot in slots)
    if total <= 0:
        if not slots:
            return []
        even = (1.0 - BUDGET_RESERVE) / len(slots)
        return [replace(slot, budget_share=even) for slot in slots]
    scale = (1.0 - BUDGET_RESERVE) / total
    return [replace(slot, budget_share=slot.budget_share * scale) for slot in slots]


def _drop_unaffordable(
    slots: list[PlanSlot], budget_cents: int, min_price_by_category: dict[str, int]
) -> list[PlanSlot]:
    """5.4: a slot that cannot afford its category's cheapest item is dropped.

    Lowest priority first, and `must` slots are never dropped -- if a `must`
    is unaffordable the whole layout is, and the solver reports that with a
    sentence the user can act on rather than the planner quietly removing the
    bed from a bedroom.
    """
    working = list(slots)
    while True:
        unaffordable = [
            slot
            for slot in working
            if slot.priority != "must"
            and slot.allocation_cents(budget_cents) < min_price_by_category.get(slot.category, 0)
        ]
        if not unaffordable:
            return working
        victim = max(
            unaffordable,
            key=lambda slot: (PRIORITY_ORDER.get(slot.priority, 1), -slot.budget_share),
        )
        working = [slot for slot in working if slot.slot_id != victim.slot_id]
        working = _renormalise(working)
        if not working:
            return []


@dataclass(frozen=True, slots=True)
class Product:
    """A catalog row, as the layout engine needs to see it.

    The real source is the `layout_candidates` materialized view (4.11). This
    is the shape, so the shortlist and the pick can be written and tested
    before Postgres exists.
    """

    id: str
    category: str
    width_mm: float
    depth_mm: float
    height_mm: float
    price_cents: int
    style_tags: tuple[str, ...] = ()
    color_hex: str = ""

    @property
    def footprint_area_mm2(self) -> float:
        return self.width_mm * self.depth_mm

    def to_candidate(self) -> Candidate:
        return Candidate(
            product_id=self.id,
            width_mm=self.width_mm,
            depth_mm=self.depth_mm,
            height_mm=self.height_mm,
            price_cents=self.price_cents,
        )


# 5.4 L3: "price_cents <= allocation x 1.15". The slack exists because the
# budget split is a guess -- one slot running slightly over is usually paid
# for by another running under, and the budget pass in L4 settles it.
ALLOCATION_SLACK = 1.15

# 5.4 L3: "Top K = 8".
SHORTLIST_SIZE = 8


def shortlist(
    slot: PlanSlot,
    catalog: list[Product],
    analysis: RoomAnalysis,
    *,
    budget_cents: int,
    style: str | None = None,
) -> list[Product]:
    """5.4 L3: the products that could fill this slot, best first.

    Filters on what is knowable without placing anything -- category, price,
    and whether the thing could physically fit the room at all -- then ranks.
    Whether it fits *here* is the solver's question, and asking it now would
    mean solving the layout once per candidate.
    """
    allocation = slot.allocation_cents(budget_cents)
    ceiling = analysis.ceiling_height_mm - 100.0
    longest_run = max((run.length_mm for run in analysis.free_runs), default=0.0)
    rule = CATEGORY_RULES.get(slot.category, CategoryRule())

    viable = [
        product
        for product in catalog
        if product.category == slot.category
        and product.price_cents <= allocation * ALLOCATION_SLACK
        and product.height_mm <= ceiling
        and _fits_the_room(product, rule, longest_run, analysis)
    ]

    def rank(product: Product) -> tuple[float, str]:
        # Style overlap first, then closeness to the allocation: spending the
        # allocation is the point of having split the budget, and both the
        # cheapest and the most expensive option are usually the wrong one.
        style_score = 1.0 if style and style in product.style_tags else 0.0
        price_distance = abs(product.price_cents - allocation) / max(allocation, 1)
        return (-(style_score * 2.0 - price_distance), product.id)

    return sorted(viable, key=rank)[:SHORTLIST_SIZE]


def _fits_the_room(
    product: Product, rule: CategoryRule, longest_run: float, analysis: RoomAnalysis
) -> bool:
    """Could this product go anywhere in this room at all?

    A generous test on purpose. It exists to keep a three-metre sideboard out
    of a shortlist for a box room, not to predict the solver -- which has the
    whole layout in hand and is the only thing that can decide.
    """
    if rule.prefers_wall and longest_run > 0 and product.width_mm > longest_run:
        return False
    min_x, min_z, max_x, max_z = analysis.floor.bounds
    span = max(max_x - min_x, max_z - min_z)
    return min(product.width_mm, product.depth_mm) <= span


def pick(
    plan: LayoutPlan,
    shortlists: dict[str, list[Product]],
    *,
    budget_cents: int,
    preferred: dict[str, str] | None = None,
) -> dict[str, list[Product]]:
    """5.4 L4: choose per slot, then settle the budget.

    `preferred` is the model's choice per slot when there was a call --
    5.4's "a Haiku call per layout selects one product ID per slot, favoring
    visual coherence". An id that is not in that slot's own shortlist is
    ignored in favour of the top-ranked item, which is what 5.4 requires and
    is also the whole of the deterministic path: pass nothing and every slot
    takes its best-ranked option.

    The budget pass afterwards runs either way -- 5.5 H7 is a hard rule for
    AI layouts, so the model's choice is never the last word on what the
    layout costs.

    Returns the shortlist per slot reordered with the chosen product first,
    because the solver walks the list when its first choice does not fit.
    """
    wanted = preferred or {}
    chosen: dict[str, Product] = {}
    for slot in plan.slots:
        options = shortlists.get(slot.slot_id, [])
        if not options:
            continue
        by_id = {product.id: product for product in options}
        chosen[slot.slot_id] = by_id.get(wanted.get(slot.slot_id, ""), options[0])

    chosen = _fit_to_budget(plan, chosen, shortlists, budget_cents, deliberate=set(wanted))

    result: dict[str, list[Product]] = {}
    for slot in plan.slots:
        options = shortlists.get(slot.slot_id, [])
        winner = chosen.get(slot.slot_id)
        if winner is None:
            result[slot.slot_id] = list(options)
            continue
        result[slot.slot_id] = [winner, *[p for p in options if p.id != winner.id]]
    return result


def _fit_to_budget(
    plan: LayoutPlan,
    chosen: dict[str, Product],
    shortlists: dict[str, list[Product]],
    budget_cents: int,
    deliberate: set[str] | None = None,
) -> dict[str, Product]:
    """5.4 L4: swap down while over budget, then upgrade if money is left.

    "Repeatedly swap the item with the best price saved per score lost, and
    if still over, drop the lowest-priority slot." Score-lost is approximated
    by shortlist position, which is what the ranking already encodes.

    `deliberate` is the set of slots a model actually chose for, and the
    upgrade pass leaves them alone. 5.4 gives both rules -- the model picks
    "favoring visual coherence", and leftover money upgrades `must` slots --
    and with a loose budget they contradict each other: the upgrade pass
    ranks on price alone, so it would systematically replace the chosen sofa
    with the dearest one in the shortlist and undo the coherence the call was
    made to buy. That is R12 ("layouts look unnatural even when valid")
    reintroduced by the budget pass, so the deliberate choice wins.

    Swapping *down* is not exempt: 5.5 H7 is a hard rule and a layout over
    budget has no legal alternative.
    """
    by_id = {slot.slot_id: slot for slot in plan.slots}
    working = dict(chosen)

    def total() -> int:
        return sum(product.price_cents for product in working.values())

    # Swap down.
    while total() > budget_cents:
        best_swap: tuple[int, str, Product] | None = None
        for slot_id, current in working.items():
            for alternative in shortlists.get(slot_id, []):
                if alternative.price_cents >= current.price_cents:
                    continue
                saved = current.price_cents - alternative.price_cents
                if best_swap is None or saved > best_swap[0]:
                    best_swap = (saved, slot_id, alternative)
        if best_swap is None:
            break
        working[best_swap[1]] = best_swap[2]

    # Still over: drop, lowest priority first, never a `must`.
    while total() > budget_cents:
        droppable = [slot_id for slot_id in working if by_id[slot_id].priority != "must"]
        if not droppable:
            break
        victim = max(
            droppable,
            key=lambda slot_id: (
                PRIORITY_ORDER.get(by_id[slot_id].priority, 1),
                working[slot_id].price_cents,
            ),
        )
        del working[victim]

    # 5.4: "If there is money left over (> 10% of budget), upgrade `must`
    # slots within their shortlists." A budget deliberately left unspent is
    # not a feature the user asked for.
    remaining = budget_cents - total()
    untouchable = deliberate or set()
    if remaining > budget_cents * 0.10:
        for slot in sorted(plan.slots, key=lambda s: PRIORITY_ORDER.get(s.priority, 1)):
            chosen_here = working.get(slot.slot_id)
            if chosen_here is None or slot.slot_id in untouchable:
                continue
            for alternative in shortlists.get(slot.slot_id, []):
                extra = alternative.price_cents - chosen_here.price_cents
                if 0 < extra <= remaining:
                    working[slot.slot_id] = alternative
                    remaining -= extra
                    break
    return working


def to_solver_slots(plan: LayoutPlan, shortlists: dict[str, list[Product]]) -> list[Slot]:
    """Turn a checked plan plus its shortlists into what the solver takes."""
    return [
        Slot(
            slot_id=slot.slot_id,
            category=slot.category,
            priority=slot.priority,
            intent=Intent(
                anchor_wall=slot.anchor_wall,
                anchor_slot=slot.anchor_slot,
            ),
            candidates=[product.to_candidate() for product in shortlists.get(slot.slot_id, [])],
        )
        for slot in plan.slots
    ]


def plan_for(
    analysis: RoomAnalysis,
    catalog: list[Product],
    *,
    room_type: str,
    budget_cents: int,
    style: str | None = None,
    proposed: LayoutPlan | None = None,
    preferred: dict[str, str] | None = None,
) -> tuple[LayoutPlan, list[Slot]]:
    """The whole of L2-L4: plan, shortlist, pick.

    `proposed` is the model's plan when there is one and `preferred` its
    product choices. Passing neither runs the entirely deterministic path --
    the template plan and the top-ranked product per slot -- which is what
    happens on an LLM failure and in every test that has no network.
    """
    minimums = _cheapest_per_category(catalog)
    raw = proposed if proposed is not None else template_plan(room_type)
    checked = post_check(raw, analysis, budget_cents=budget_cents, min_price_by_category=minimums)

    shortlists = {
        slot.slot_id: shortlist(slot, catalog, analysis, budget_cents=budget_cents, style=style)
        for slot in checked.slots
    }
    return checked, to_solver_slots(
        checked,
        pick(checked, shortlists, budget_cents=budget_cents, preferred=preferred),
    )


def _cheapest_per_category(catalog: list[Product]) -> dict[str, int]:
    minimums: dict[str, int] = {}
    for product in catalog:
        current = minimums.get(product.category)
        if current is None or product.price_cents < current:
            minimums[product.category] = product.price_cents
    return minimums
