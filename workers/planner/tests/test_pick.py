"""L4: the product pick and what overrules it (5.4).

Two things are being protected. One is 5.4's rule that "its choice must be
an ID from the shortlist; otherwise the top-ranked item is used" -- the
model cannot buy something that is not for sale. The other is that the
budget pass runs afterwards regardless: 5.5 H7 is a hard rule, so a
beautifully coherent set that costs double the budget is not a layout.
"""

from __future__ import annotations

import json
from typing import Any

from planner_helpers import ScriptedTransport, catalog
from roomfittr_layout.planner import (
    LayoutPlan,
    PlanSlot,
    Product,
    shortlist,
)
from roomfittr_layout.planner import (
    pick as choose_products,
)
from roomfittr_layout.room import RoomAnalysis
from roomfittr_planner.client import TransportError
from roomfittr_planner.pick import ATTEMPTS, SYSTEM, build_user_message, pick_products

BUDGET = 300_000


def a_plan() -> LayoutPlan:
    return LayoutPlan(
        room_type="living",
        slots=[
            PlanSlot("S1", "sofa", "must", 0.45),
            PlanSlot("S2", "coffee_table", "should", 0.2),
            PlanSlot("S3", "rug", "nice", 0.15),
        ],
        style="modern",
    )


def shortlists_for(plan: LayoutPlan, analysis: RoomAnalysis) -> dict[str, list[Product]]:
    return {
        slot.slot_id: shortlist(slot, catalog(), analysis, budget_cents=BUDGET)
        for slot in plan.slots
    }


def picks(pairs: dict[str, str]) -> dict[str, Any]:
    return {"picks": [{"slot_id": k, "product_id": v} for k, v in pairs.items()]}


class TestPrompt:
    def test_each_slot_sees_only_its_own_options(self, living: RoomAnalysis) -> None:
        """The enum in the schema stops a cross-slot id, but the prompt is
        what stops the model wanting one."""
        plan = a_plan()
        lists = shortlists_for(plan, living)
        text = build_user_message(plan, lists, budget_cents=BUDGET, currency="EUR", style=None)
        payload = json.loads(text[text.index("{") : text.rindex("}") + 1])
        for entry in payload["slots"]:
            expected = {p.id for p in lists[entry["slot_id"]]}
            assert {o["product_id"] for o in entry["options"]} == expected

    def test_it_carries_what_5_4_says_it_gets(self, living: RoomAnalysis) -> None:
        """5.4: "it gets titles, tags, colors, prices"."""
        plan = a_plan()
        text = build_user_message(
            plan,
            shortlists_for(plan, living),
            budget_cents=BUDGET,
            currency="EUR",
            style="modern",
        )
        payload = json.loads(text[text.index("{") : text.rindex("}") + 1])
        option = payload["slots"][0]["options"][0]
        assert set(option) >= {"product_id", "price_cents", "style_tags", "color_hex"}

    def test_a_slot_with_nothing_to_buy_is_left_out(self, living: RoomAnalysis) -> None:
        plan = a_plan()
        lists = shortlists_for(plan, living)
        lists["S3"] = []
        text = build_user_message(plan, lists, budget_cents=BUDGET, currency="EUR", style=None)
        payload = json.loads(text[text.index("{") : text.rindex("}") + 1])
        assert {entry["slot_id"] for entry in payload["slots"]} == {"S1", "S2"}

    def test_the_system_prompt_asks_for_a_coherent_set(self) -> None:
        """R12 is "LLM layouts look unnatural even when valid"; judging the
        set rather than the items is the whole of this call's job."""
        assert "set, not the items" in SYSTEM


class TestPickProducts:
    def test_a_valid_choice_is_honoured(self, living: RoomAnalysis) -> None:
        plan = a_plan()
        lists = shortlists_for(plan, living)
        wanted = {slot_id: options[-1].id for slot_id, options in lists.items() if options}
        transport = ScriptedTransport(script=[picks(wanted)])

        attempt = pick_products(transport, plan, lists, budget_cents=BUDGET)
        assert attempt.preferred == wanted
        assert attempt.failure == ""
        assert attempt.rejected == ()

    def test_a_hallucinated_id_is_dropped_and_the_rest_kept(self, living: RoomAnalysis) -> None:
        """5.4: not in the shortlist means the top-ranked item. Discarding the
        whole answer over one bad id would throw away the coherence the call
        was made for."""
        plan = a_plan()
        lists = shortlists_for(plan, living)
        good = lists["S1"][-1].id
        transport = ScriptedTransport(
            script=[picks({"S1": good, "S2": "sofa-from-another-catalog"})]
        )

        attempt = pick_products(transport, plan, lists, budget_cents=BUDGET)
        assert attempt.preferred == {"S1": good}
        assert attempt.rejected == ("S2:sofa-from-another-catalog",)

    def test_an_id_from_another_slots_shortlist_is_rejected(self, living: RoomAnalysis) -> None:
        """A real id in the wrong slot is the failure mode a bare
        "does this product exist" check would miss -- and it would put a sofa
        in the rug's slot."""
        plan = a_plan()
        lists = shortlists_for(plan, living)
        wrong = picks({"S3": lists["S1"][0].id})
        transport = ScriptedTransport(script=[wrong, wrong])

        attempt = pick_products(transport, plan, lists, budget_cents=BUDGET)
        assert attempt.preferred == {}
        assert attempt.failure == "no_valid_picks"
        # The rejects survive the total failure: 6.2 needs "it named ids that
        # do not exist" to be distinguishable from "it timed out".
        assert attempt.rejected == (f"S3:{lists['S1'][0].id}",)

    def test_two_failures_fall_back_to_the_ranking(self, living: RoomAnalysis) -> None:
        plan = a_plan()
        lists = shortlists_for(plan, living)
        transport = ScriptedTransport(
            script=[TransportError("rate_limited"), TransportError("rate_limited")]
        )

        attempt = pick_products(transport, plan, lists, budget_cents=BUDGET)
        assert attempt.preferred == {}
        assert attempt.failure == "rate_limited"
        assert len(transport.calls) == ATTEMPTS

    def test_no_call_is_made_when_there_is_nothing_to_choose(self, living: RoomAnalysis) -> None:
        """7.4's "catalog too thin" state. Calling anyway would spend money to
        be told what the shortlist already says."""
        plan = a_plan()
        transport = ScriptedTransport(script=[picks({})])

        attempt = pick_products(
            transport, plan, {s.slot_id: [] for s in plan.slots}, budget_cents=BUDGET
        )
        assert attempt.failure == "no_shortlists"
        assert transport.calls == []

    def test_it_does_not_think(self, living: RoomAnalysis) -> None:
        """1.2 puts this on Haiku, which does not take adaptive thinking, and
        the question is a preference rather than a derivation."""
        plan = a_plan()
        lists = shortlists_for(plan, living)
        transport = ScriptedTransport(script=[picks({"S1": lists["S1"][0].id})])
        pick_products(transport, plan, lists, budget_cents=BUDGET)
        assert transport.calls[0]["think"] is False

    def test_it_runs_on_haiku(self, living: RoomAnalysis) -> None:
        plan = a_plan()
        lists = shortlists_for(plan, living)
        transport = ScriptedTransport(script=[picks({"S1": lists["S1"][0].id})])
        pick_products(transport, plan, lists, budget_cents=BUDGET)
        assert "haiku" in transport.calls[0]["model"]


class TestTheBudgetStillWins:
    def test_an_unaffordable_set_is_swapped_down(self, living: RoomAnalysis) -> None:
        """5.5 H7 is hard for AI layouts, so the model's taste is never the
        last word on what the layout costs."""
        plan = a_plan()
        lists = shortlists_for(plan, living)
        dearest = {
            slot_id: max(options, key=lambda p: p.price_cents).id
            for slot_id, options in lists.items()
            if options
        }
        tight = 60_000

        ordered = choose_products(plan, lists, budget_cents=tight, preferred=dearest)
        total = sum(options[0].price_cents for options in ordered.values() if options)
        assert total <= tight

    def test_an_affordable_choice_is_left_alone(self, living: RoomAnalysis) -> None:
        plan = a_plan()
        lists = shortlists_for(plan, living)
        wanted = {"S1": lists["S1"][0].id}

        ordered = choose_products(plan, lists, budget_cents=BUDGET, preferred=wanted)
        assert ordered["S1"][0].id == wanted["S1"]

    def test_no_preference_takes_the_top_ranked_item(self, living: RoomAnalysis) -> None:
        """The deterministic path, which is what runs with no key."""
        plan = a_plan()
        lists = shortlists_for(plan, living)

        ordered = choose_products(plan, lists, budget_cents=BUDGET)
        for slot_id, options in ordered.items():
            if lists[slot_id]:
                assert options[0].id == lists[slot_id][0].id
