"""L2: the planner call and its fallback (5.4, 5.1).

The call is scripted, so what is under test is everything around it: that
the prompt carries what 5.4 says it must, that two bad answers become the
template, and that the user's own choices outrank the model's.
"""

from __future__ import annotations

import json

import pytest
from planner_helpers import ScriptedTransport, catalog, plan_arguments, slot
from roomfittr_layout.planner import template_plan
from roomfittr_layout.room import RoomAnalysis
from roomfittr_planner.client import TransportError
from roomfittr_planner.plan import (
    ATTEMPTS,
    SYSTEM,
    build_user_message,
    default_ratios,
    price_percentiles,
    propose_plan,
)

BUDGET = 300_000


class TestPricePercentiles:
    def test_every_answer_is_a_price_something_actually_costs(self) -> None:
        """These numbers are shown to the model as what it can buy;
        interpolating would invent a price no product has."""
        rows = catalog()
        prices = {p.price_cents for p in rows}
        for stats in price_percentiles(rows).values():
            assert {stats["p10"], stats["p50"], stats["p90"]} <= prices

    def test_they_are_ordered(self) -> None:
        for stats in price_percentiles(catalog()).values():
            assert stats["p10"] <= stats["p50"] <= stats["p90"]

    def test_p10_of_one_product_is_that_product(self) -> None:
        rows = [p for p in catalog() if p.id == "sofa-1"]
        assert price_percentiles(rows)["sofa"]["p10"] == 40_000

    def test_an_empty_catalog_prices_nothing_rather_than_crashing(self) -> None:
        assert price_percentiles([]) == {}


class TestPrompt:
    def test_it_carries_everything_5_4_lists(self, living: RoomAnalysis) -> None:
        """5.4 L2's Input: "room summary, room type, budget, currency,
        optional style, the category vocabulary with price percentiles per
        category from the live catalog, and default budget ratios"."""
        payload = json.loads(
            build_user_message(
                living,
                room_type="living",
                budget_cents=BUDGET,
                currency="EUR",
                catalog=catalog(),
                style="scandinavian",
            ).split("\n\n")[1]
        )
        assert payload["room"]["walls"]
        assert payload["room_type"] == "living"
        assert payload["budget"]["total_cents"] == BUDGET
        assert payload["budget"]["currency"] == "EUR"
        assert payload["style_preference"] == "scandinavian"
        assert payload["catalog_prices_cents"]["sofa"]["p10"] == 15_000
        assert payload["default_budget_ratios"]["sofa"] > 0

    def test_it_sends_no_coordinates(self, living: RoomAnalysis) -> None:
        """5.2: the summary is "compact JSON, not geometry dumps". Decision 4
        gives the model ids and the solver coordinates; sending vertices
        invites it to reason about geometry it is not the authority on."""
        payload = json.loads(
            build_user_message(
                living,
                room_type="living",
                budget_cents=BUDGET,
                currency="EUR",
                catalog=catalog(),
            ).split("\n\n")[1]
        )
        assert "floor" not in payload["room"]
        for wall in payload["room"]["walls"]:
            assert set(wall) <= {"id", "kind", "length_mm", "free_runs_mm", "has"}

    def test_the_system_prompt_forbids_placement(self) -> None:
        assert "do not place" in SYSTEM.lower()
        assert "solver" in SYSTEM.lower()

    def test_default_ratios_come_from_the_template(self) -> None:
        ratios = default_ratios("bedroom")
        assert set(ratios) == {s.category for s in template_plan("bedroom").slots}
        assert ratios["bed_frame"] == pytest.approx(0.42, abs=1e-3)

    def test_an_unknown_room_type_still_has_ratios(self) -> None:
        """`template_plan` falls back to living rather than refusing, and the
        prompt must not be the thing that breaks instead."""
        assert default_ratios("conservatory")


class TestProposePlan:
    def test_a_good_answer_is_used(self, living: RoomAnalysis) -> None:
        transport = ScriptedTransport(
            script=[
                plan_arguments(
                    [
                        slot(
                            "S1",
                            "sofa",
                            priority="must",
                            budget_share=0.45,
                            anchor={"kind": "wall", "ref": "W1"},
                        ),
                        slot(
                            "S2",
                            "coffee_table",
                            budget_share=0.2,
                            anchor={"kind": "slot", "ref": "S1"},
                        ),
                    ]
                )
            ]
        )
        attempt = propose_plan(
            transport, living, room_type="living", budget_cents=BUDGET, catalog=catalog()
        )
        assert attempt.from_model
        assert not attempt.plan.from_template
        assert [s.category for s in attempt.plan.slots] == ["sofa", "coffee_table"]
        assert attempt.failure == ""
        assert attempt.spend.micro_dollars > 0

    def test_it_thinks_on_the_planner_call(self, living: RoomAnalysis) -> None:
        """D12 is about the quality of exactly this judgement."""
        transport = ScriptedTransport(script=[plan_arguments([slot("S1", "sofa")])])
        propose_plan(transport, living, room_type="living", budget_cents=BUDGET, catalog=catalog())
        assert transport.calls[0]["think"] is True

    def test_two_failures_become_the_template(self, living: RoomAnalysis) -> None:
        """5.1: "If the LLM call fails, times out, returns an invalid schema
        twice ... the engine uses a rule-based template plan"."""
        transport = ScriptedTransport(script=[TransportError("timeout"), TransportError("timeout")])
        attempt = propose_plan(
            transport, living, room_type="bedroom", budget_cents=BUDGET, catalog=catalog()
        )
        assert attempt.plan.from_template
        assert attempt.plan.room_type == "bedroom"
        assert attempt.failure == "timeout"
        assert len(transport.calls) == ATTEMPTS

    def test_it_retries_once_and_only_once(self, living: RoomAnalysis) -> None:
        """A third attempt costs another timeout to arrive at the template the
        caller can have immediately -- and 7.1 budgets the whole generation at
        10-40 s."""
        transport = ScriptedTransport(
            script=[TransportError("timeout"), plan_arguments([slot("S1", "sofa")])]
        )
        attempt = propose_plan(
            transport, living, room_type="living", budget_cents=BUDGET, catalog=catalog()
        )
        assert not attempt.plan.from_template
        assert len(transport.calls) == 2

    def test_the_retry_says_what_was_wrong(self, living: RoomAnalysis) -> None:
        transport = ScriptedTransport(
            script=[plan_arguments([]), plan_arguments([slot("S1", "sofa")])]
        )
        propose_plan(transport, living, room_type="living", budget_cents=BUDGET, catalog=catalog())
        assert "no usable slots" in transport.calls[1]["user"]

    def test_an_empty_plan_is_treated_as_a_failure(self, living: RoomAnalysis) -> None:
        transport = ScriptedTransport(script=[plan_arguments([]), plan_arguments([])])
        attempt = propose_plan(
            transport, living, room_type="living", budget_cents=BUDGET, catalog=catalog()
        )
        assert attempt.plan.from_template
        assert attempt.failure == "empty_plan"

    def test_prose_instead_of_a_tool_call_is_a_failure(self, living: RoomAnalysis) -> None:
        """`tool_choice` is `auto`, so this is a real answer shape, and 5.1
        makes it indistinguishable from an invalid schema."""
        transport = ScriptedTransport(script=["Happy to help! First, ...", "Sure thing."])
        attempt = propose_plan(
            transport, living, room_type="living", budget_cents=BUDGET, catalog=catalog()
        )
        assert attempt.plan.from_template
        assert attempt.failure == "no_tool_call"

    def test_the_users_style_outranks_the_models(self, living: RoomAnalysis) -> None:
        """5.1: style is "User choice (optional) or LLM inference", in that
        order."""
        transport = ScriptedTransport(
            script=[plan_arguments([slot("S1", "sofa")], style="industrial")]
        )
        attempt = propose_plan(
            transport,
            living,
            room_type="living",
            budget_cents=BUDGET,
            catalog=catalog(),
            style="scandinavian",
        )
        assert attempt.plan.style == "scandinavian"

    def test_the_models_style_is_kept_when_the_user_gave_none(self, living: RoomAnalysis) -> None:
        transport = ScriptedTransport(
            script=[plan_arguments([slot("S1", "sofa")], style="industrial")]
        )
        attempt = propose_plan(
            transport, living, room_type="living", budget_cents=BUDGET, catalog=catalog()
        )
        assert attempt.plan.style == "industrial"

    def test_a_failure_still_reports_what_the_attempts_cost(self, living: RoomAnalysis) -> None:
        """A model that answers twice in prose is not free, and a cost
        measurement that only counts successes understates the bill."""
        transport = ScriptedTransport(script=[plan_arguments([]), plan_arguments([])])
        attempt = propose_plan(
            transport, living, room_type="living", budget_cents=BUDGET, catalog=catalog()
        )
        assert attempt.plan.from_template
        assert attempt.spend.micro_dollars > 0
