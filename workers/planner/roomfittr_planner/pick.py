"""L4: ask Haiku which products go together (implementation-plan.md 5.4).

One call for the whole layout. Every option it sees has already passed L3 --
right category, inside the allocation, physically capable of fitting the
room -- so nothing it can choose is illegal. That is the point of the
shortlist: the model is answering a question of taste, which is the one
thing here that deterministic code is bad at, and it cannot answer it
wrongly enough to break anything.

R12 is the risk this addresses: "LLM layouts look unnatural even when
valid". Six individually sensible purchases from six different catalogs do
not look like a room somebody furnished. The mitigation 9.3 lists is "the
LLM chooses among valid options", and this is that choice.

Haiku rather than Sonnet because 1.2 says so and because the question is
narrow: read ten short product descriptions, pick the set that matches. The
budget pass afterwards is deterministic and may overrule every choice.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from roomfittr_layout.planner import LayoutPlan, Product

from .client import PICKER_MODEL, PICKER_TIMEOUT_S, Transport, TransportError
from .cost import Spend
from .schemas import parse_picks, pick_tool

ATTEMPTS = 2
MAX_TOKENS = 2000

SYSTEM = """\
You are furnishing one room for RoomFittr. For each slot you are given a \
shortlist of products that all fit the room and the budget, and you choose \
one from each.

Judge the set, not the items. The right answer is the combination that looks \
like one person furnished the room in one go: materials that belong together \
(not oak, chrome and rattan at once), a colour palette of two or three \
tones, and one consistent style. A slightly worse individual item that ties \
the room together beats a better one that clashes.

The large items set the tone -- match the smaller ones to them rather than \
the other way round.

Choose a product id only from that slot's own options. Call \
submit_product_picks once with every slot, and reply with nothing else.\
"""


@dataclass(frozen=True, slots=True)
class PickAttempt:
    """The model's choices, what they cost, and what was discarded."""

    preferred: dict[str, str] = field(default_factory=dict)
    spend: Spend = field(default_factory=Spend)
    failure: str = ""
    rejected: tuple[str, ...] = ()

    @property
    def from_model(self) -> bool:
        return bool(self.preferred)


def build_user_message(
    plan: LayoutPlan,
    shortlists: dict[str, list[Product]],
    *,
    budget_cents: int,
    currency: str,
    style: str | None,
) -> str:
    slots = []
    for slot in plan.slots:
        options = shortlists.get(slot.slot_id, [])
        if not options:
            continue
        slots.append(
            {
                "slot_id": slot.slot_id,
                "category": slot.category,
                "priority": slot.priority,
                "allocation_cents": slot.allocation_cents(budget_cents),
                "options": [
                    {
                        "product_id": product.id,
                        "price_cents": product.price_cents,
                        "width_mm": int(round(product.width_mm)),
                        "depth_mm": int(round(product.depth_mm)),
                        "height_mm": int(round(product.height_mm)),
                        "style_tags": list(product.style_tags),
                        "color_hex": product.color_hex,
                    }
                    for product in options
                ],
            }
        )

    payload = {
        "room_type": plan.room_type,
        "style_preference": style or plan.style,
        "budget": {"currency": currency, "total_cents": budget_cents},
        "slots": slots,
    }
    return (
        "Choose one product per slot.\n\n"
        f"{json.dumps(payload, indent=2, sort_keys=True)}\n\n"
        "Call submit_product_picks with one entry per slot."
    )


def pick_products(
    transport: Transport,
    plan: LayoutPlan,
    shortlists: dict[str, list[Product]],
    *,
    budget_cents: int,
    currency: str = "EUR",
    style: str | None = None,
    model: str = PICKER_MODEL,
) -> PickAttempt:
    """5.4 L4's model call. Never raises; an empty result means "use the ranking"."""
    usable = {
        slot.slot_id: shortlists[slot.slot_id]
        for slot in plan.slots
        if shortlists.get(slot.slot_id)
    }
    if not usable:
        # Nothing to choose between. Calling anyway would spend money to be
        # told what the shortlist already says.
        return PickAttempt(failure="no_shortlists")

    tool = pick_tool(sorted(usable))
    user = build_user_message(
        plan, shortlists, budget_cents=budget_cents, currency=currency, style=style
    )
    spend = Spend()
    last_failure = ""
    # Kept across attempts so a run that ends with nothing usable still
    # records *what* the model asked for. 6.2 wants that: "the model picked
    # three ids that do not exist" and "the model timed out" are the same
    # empty result and entirely different problems.
    last_rejected: tuple[str, ...] = ()

    for attempt in range(ATTEMPTS):
        message = user if attempt == 0 else f"{user}\n\nThe previous attempt failed. Try again."
        try:
            result = transport.call_tool(
                model=model,
                system=SYSTEM,
                user=message,
                tool=tool,
                max_tokens=MAX_TOKENS,
                timeout_s=PICKER_TIMEOUT_S,
                think=False,
            )
        except TransportError as error:
            last_failure = error.kind
            continue

        spend = spend.plus(result.usage)
        picks = parse_picks(result.arguments)
        if not picks:
            # The model called the tool and chose nothing. That is an answer,
            # not a failure: it has no preference, and the ranking is what
            # 5.4 falls back to anyway. Retrying would spend a second Haiku
            # call on every layout to be told the same thing.
            return PickAttempt(spend=spend, failure="no_preference")

        preferred, rejected = _keep_valid(picks, usable)
        if not preferred:
            # It did try, and every id it named was wrong. Worth one retry.
            last_failure = "no_valid_picks"
            last_rejected = rejected or last_rejected
            continue
        return PickAttempt(preferred=preferred, spend=spend, rejected=rejected)

    return PickAttempt(spend=spend, failure=last_failure or "unknown", rejected=last_rejected)


def _keep_valid(
    picks: dict[str, str], shortlists: dict[str, list[Product]]
) -> tuple[dict[str, str], tuple[str, ...]]:
    """5.4: "Its choice must be an ID from the shortlist; otherwise the top-ranked item".

    A hallucinated id is dropped rather than the whole answer: the model
    getting one of six wrong is no reason to discard the five that tie the
    room together. The rejects are returned so 9.1's chaos tests can assert
    they were caught and 6.2 can record that they happened.
    """
    kept: dict[str, str] = {}
    rejected: list[str] = []
    for slot_id, product_id in picks.items():
        options = shortlists.get(slot_id)
        if options and any(product.id == product_id for product in options):
            kept[slot_id] = product_id
        else:
            rejected.append(f"{slot_id}:{product_id}")
    return kept, tuple(rejected)
