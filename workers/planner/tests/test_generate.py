"""L2 to L6 end to end, and 9.1's LLM chaos tests.

9.1 lists "LLM chaos tests: the LLM returns invalid JSON / nonexistent IDs /
absurd budget shares -> the engine still returns a valid layout via clamping
or template" as a gate on every CI run. That is the contract this file
exists to hold, and the assertion in almost every test below is the same
one: **zero hard violations, inside the budget, whatever the model did.**

It is deliberately the same assertion as Phase 4's definition of done. A
misbehaving model is not a special case with a weaker standard -- 7.4 says
"LLM errors -> template fallback (never a user-visible failure)", so a
layout produced despite the model must be as legal as one produced with it.
"""

from __future__ import annotations

from typing import Any

import pytest
from planner_helpers import ScriptedTransport, catalog, plan_arguments, slot
from roomfittr_layout.room import RoomAnalysis
from roomfittr_planner.client import TransportError
from roomfittr_planner.generate import PlannedLayout, generate_layout

BUDGET = 300_000


def assert_legal(planned: PlannedLayout, *, budget_cents: int = BUDGET) -> None:
    """Phase 4's definition of done, as an assertion."""
    report = planned.layout.report
    hard = [v for v in report.violations if v.severity == "hard"]
    for violations in report.items.values():
        hard.extend(v for v in violations if v.severity == "hard")
    assert not hard, "; ".join(v.message for v in hard)
    assert report.fits

    total = sum(item.price_cents for item in planned.layout.items)
    assert total <= budget_cents, f"{total} > {budget_cents}"


def good_plan() -> dict[str, Any]:
    return plan_arguments(
        [
            slot(
                "S1",
                "sofa",
                priority="must",
                budget_share=0.4,
                relation="against_wall",
                anchor={"kind": "wall", "ref": "W1"},
            ),
            slot(
                "S2",
                "coffee_table",
                budget_share=0.15,
                relation="in_front_of",
                anchor={"kind": "slot", "ref": "S1"},
            ),
            slot(
                "S3",
                "rug",
                priority="nice",
                budget_share=0.15,
                relation="under",
                anchor={"kind": "slot", "ref": "S1"},
            ),
            slot("S4", "floor_lamp", priority="nice", budget_share=0.1),
        ]
    )


def good_picks(planned_ids: dict[str, str]) -> dict[str, Any]:
    return {"picks": [{"slot_id": k, "product_id": v} for k, v in planned_ids.items()]}


def products_of(planned: PlannedLayout) -> dict[str, str]:
    """Slot id -> the product actually placed there."""
    return {p.slot_id: p.product_id for p in planned.layout.result.placements}


def no_preference() -> list[dict[str, Any]]:
    """A model that calls the tool and chooses nothing.

    An answer rather than a failure, so it is not retried -- see
    `pick_products`.
    """
    return [{"picks": []}]


class TestTheDeterministicPath:
    def test_no_transport_means_no_call_and_a_real_layout(self, living: RoomAnalysis) -> None:
        """The path that runs with no key, in CI, and in every other test in
        this repo. 5.4's templates are the floor the product stands on."""
        planned = generate_layout(living, catalog(), room_type="living", budget_cents=BUDGET)
        assert_legal(planned)
        assert planned.plan.from_template
        assert planned.spend.micro_dollars == 0
        assert planned.as_meta()["plan_source"] == "template"
        assert planned.layout.items

    @pytest.mark.parametrize("room_type", ["living", "bedroom", "office", "dining"])
    def test_every_room_type_produces_a_layout(self, living: RoomAnalysis, room_type: str) -> None:
        planned = generate_layout(living, catalog(), room_type=room_type, budget_cents=BUDGET)
        assert_legal(planned)


class TestTheModelPath:
    def test_a_good_plan_and_good_picks_produce_a_layout(self, living: RoomAnalysis) -> None:
        transport = ScriptedTransport(script=[good_plan(), *no_preference()])
        planned = generate_layout(
            living,
            catalog(),
            room_type="living",
            budget_cents=BUDGET,
            transport=transport,
        )
        assert_legal(planned)
        assert not planned.plan.from_template
        assert planned.as_meta()["plan_source"] == "llm"
        assert [s.category for s in planned.plan.slots] == [
            "sofa",
            "coffee_table",
            "rug",
            "floor_lamp",
        ]

    def test_both_calls_are_made_and_both_are_billed(self, living: RoomAnalysis) -> None:
        """Phase 4's cost clause is per *layout*, so both calls have to land
        in one `Spend` or the measurement counts half the bill."""
        transport = ScriptedTransport(script=[good_plan(), *no_preference()])
        planned = generate_layout(
            living,
            catalog(),
            room_type="living",
            budget_cents=BUDGET,
            transport=transport,
        )
        assert len(transport.calls) == 2
        assert len(planned.spend.calls) == 2
        assert planned.spend.micro_dollars > 0

    def test_the_models_product_choice_is_honoured(self, living: RoomAnalysis) -> None:
        transport = ScriptedTransport(script=[good_plan(), *no_preference()])
        baseline = generate_layout(
            living,
            catalog(),
            room_type="living",
            budget_cents=BUDGET,
            transport=transport,
        )
        default_sofa = products_of(baseline)["S1"]
        other_sofa = next(
            p.id
            for p in catalog()
            if p.category == "sofa" and p.id != default_sofa and p.price_cents <= 40_000
        )

        transport = ScriptedTransport(script=[good_plan(), good_picks({"S1": other_sofa})])
        planned = generate_layout(
            living,
            catalog(),
            room_type="living",
            budget_cents=BUDGET,
            transport=transport,
        )
        assert_legal(planned)
        assert products_of(planned)["S1"] == other_sofa

    def test_a_loose_budget_does_not_upgrade_away_the_choice(self, living: RoomAnalysis) -> None:
        """5.4 gives two rules that collide when there is money spare: the
        model picks "favoring visual coherence", and leftover money upgrades
        `must` slots. The upgrade ranks on price alone, so letting it win
        would replace the chosen sofa with the dearest one every time and
        undo the coherence the call was made to buy -- R12 reintroduced by
        the budget pass."""
        cheapest = "sofa-0"
        transport = ScriptedTransport(script=[good_plan(), good_picks({"S1": cheapest})])
        planned = generate_layout(
            living,
            catalog(),
            room_type="living",
            budget_cents=1_000_000,
            transport=transport,
        )
        assert products_of(planned)["S1"] == cheapest

    def test_every_placed_item_records_what_was_bought(self, living: RoomAnalysis) -> None:
        """6.2's `placed_items` has `product_id` and `slot_id` as separate
        columns. Without the product id a solved layout cannot be persisted,
        repriced on a catalog refresh, or linked out to a retailer."""
        transport = ScriptedTransport(script=[good_plan(), *no_preference()])
        planned = generate_layout(
            living,
            catalog(),
            room_type="living",
            budget_cents=BUDGET,
            transport=transport,
        )
        placed = planned.layout.result.placements
        assert placed
        assert all(p.product_id for p in placed)
        assert planned.as_meta()["products"]

    def test_the_rationale_reaches_generation_meta(self, living: RoomAnalysis) -> None:
        """5.4 makes the rationale "short, shown to user"; 6.2 is where the UI
        reads it from."""
        transport = ScriptedTransport(script=[good_plan(), *no_preference()])
        planned = generate_layout(
            living,
            catalog(),
            room_type="living",
            budget_cents=BUDGET,
            transport=transport,
        )
        assert planned.as_meta()["plan_rationale"]


class TestChaos:
    """9.1's fault injection. Every case asserts the same contract."""

    def test_prose_instead_of_json(self, living: RoomAnalysis) -> None:
        transport = ScriptedTransport(
            script=["Sure! Here is a plan:", "Let me try again:", {"picks": []}]
        )
        planned = generate_layout(
            living,
            catalog(),
            room_type="living",
            budget_cents=BUDGET,
            transport=transport,
        )
        assert_legal(planned)
        assert planned.plan.from_template
        assert planned.plan_failure == "no_tool_call"

    def test_nonexistent_wall_ids(self, living: RoomAnalysis) -> None:
        """An anchor naming a wall from another room is worse than no anchor:
        the solver would relax away from a wall it was never told about."""
        transport = ScriptedTransport(
            script=[
                plan_arguments(
                    [
                        slot(
                            "S1",
                            "sofa",
                            priority="must",
                            budget_share=0.4,
                            anchor={"kind": "wall", "ref": "W99"},
                        ),
                        slot("S2", "rug", budget_share=0.2, anchor={"kind": "slot", "ref": "S404"}),
                    ]
                ),
                *no_preference(),
            ]
        )
        planned = generate_layout(
            living,
            catalog(),
            room_type="living",
            budget_cents=BUDGET,
            transport=transport,
        )
        assert_legal(planned)
        assert planned.plan.slots[0].anchor_wall is None
        assert planned.plan.slots[1].anchor_slot is None

    def test_absurd_budget_shares(self, living: RoomAnalysis) -> None:
        """5.4: "Budget shares are renormalized to 100% with a 5% unallocated
        reserve" -- whatever they summed to before."""
        transport = ScriptedTransport(
            script=[
                plan_arguments(
                    [
                        slot("S1", "sofa", priority="must", budget_share=0.9),
                        slot("S2", "coffee_table", budget_share=0.85),
                        slot("S3", "rug", budget_share=0.7),
                    ]
                ),
                *no_preference(),
            ]
        )
        planned = generate_layout(
            living,
            catalog(),
            room_type="living",
            budget_cents=BUDGET,
            transport=transport,
        )
        assert_legal(planned)
        total = sum(s.budget_share for s in planned.plan.slots)
        assert total == pytest.approx(0.95, abs=1e-6)

    def test_a_non_finite_budget_share(self, living: RoomAnalysis) -> None:
        """Infinity passes `> 0` and `== itself`, makes the renormalising
        total infinite, and sets *every* share to zero."""
        arguments = plan_arguments(
            [
                slot("S1", "sofa", priority="must", budget_share=0.4),
                slot("S2", "rug", budget_share=0.2),
            ]
        )
        arguments["slots"][1]["budget_share"] = float("inf")
        transport = ScriptedTransport(script=[arguments, *no_preference()])

        planned = generate_layout(
            living,
            catalog(),
            room_type="living",
            budget_cents=BUDGET,
            transport=transport,
        )
        assert_legal(planned)
        assert all(s.budget_share > 0 for s in planned.plan.slots)

    def test_too_many_slots(self, living: RoomAnalysis) -> None:
        """5.4 caps V1 at ten. `strict: true` should stop this at the API, but
        the cap is enforced here too because the schema is a promise about one
        model version."""
        transport = ScriptedTransport(
            script=[
                plan_arguments([slot(f"S{i}", "armchair", budget_share=0.1) for i in range(1, 21)]),
                *no_preference(),
            ]
        )
        planned = generate_layout(
            living,
            catalog(),
            room_type="living",
            budget_cents=BUDGET,
            transport=transport,
        )
        assert_legal(planned)
        assert len(planned.plan.slots) <= 10

    def test_every_category_invented(self, living: RoomAnalysis) -> None:
        """Nothing survives the vocabulary filter, so `post_check` returns the
        template -- 5.4's "a layout is always produced"."""
        transport = ScriptedTransport(
            script=[
                plan_arguments([slot("S1", "chaise_longue"), slot("S2", "chandelier")]),
                *no_preference(),
            ]
        )
        planned = generate_layout(
            living,
            catalog(),
            room_type="living",
            budget_cents=BUDGET,
            transport=transport,
        )
        assert_legal(planned)
        assert planned.plan.from_template

    def test_hallucinated_product_ids(self, living: RoomAnalysis) -> None:
        transport = ScriptedTransport(
            script=[
                good_plan(),
                good_picks({"S1": "sofa-from-the-model's-imagination", "S2": "nope"}),
            ]
        )
        planned = generate_layout(
            living,
            catalog(),
            room_type="living",
            budget_cents=BUDGET,
            transport=transport,
        )
        assert_legal(planned)
        assert len(planned.rejected_picks) == 2
        assert planned.as_meta()["rejected_picks"]

    def test_the_most_expensive_of_everything(self, living: RoomAnalysis) -> None:
        """5.5 H7 is hard for AI layouts. The pick may be extravagant; the
        layout may not be."""
        dearest = {
            f"S{i + 1}": pid
            for i, pid in enumerate(["sofa-2", "coffee_table-2", "rug-2", "floor_lamp-2"])
        }
        transport = ScriptedTransport(script=[good_plan(), good_picks(dearest)])
        planned = generate_layout(
            living,
            catalog(),
            room_type="living",
            budget_cents=120_000,
            transport=transport,
        )
        assert_legal(planned, budget_cents=120_000)

    def test_both_calls_fail_entirely(self, living: RoomAnalysis) -> None:
        transport = ScriptedTransport(script=[TransportError("timeout")] * 4)
        planned = generate_layout(
            living,
            catalog(),
            room_type="living",
            budget_cents=BUDGET,
            transport=transport,
        )
        assert_legal(planned)
        assert planned.plan.from_template
        assert planned.plan_failure == "timeout"
        assert planned.pick_failure == "timeout"

    def test_a_refusal_is_not_read_as_an_answer(self, living: RoomAnalysis) -> None:
        transport = ScriptedTransport(
            script=[
                TransportError("stop_refusal"),
                TransportError("stop_refusal"),
                TransportError("stop_refusal"),
                TransportError("stop_refusal"),
            ]
        )
        planned = generate_layout(
            living,
            catalog(),
            room_type="living",
            budget_cents=BUDGET,
            transport=transport,
        )
        assert_legal(planned)
        assert planned.plan_failure == "stop_refusal"

    def test_an_empty_catalog_gives_an_empty_room_not_no_room(self, living: RoomAnalysis) -> None:
        """7.4 separates "catalog too thin -> skip the slot with an
        explanation" from "solver can't place a must -> fail the layout"."""
        transport = ScriptedTransport(script=[good_plan(), *no_preference()])
        planned = generate_layout(
            living, [], room_type="living", budget_cents=BUDGET, transport=transport
        )
        assert planned.layout.items == []
        assert planned.pick_failure == "no_shortlists"

    @pytest.mark.parametrize("budget", [5_000, 50_000, 300_000, 5_000_000])
    def test_any_budget_still_produces_a_legal_layout(
        self, living: RoomAnalysis, budget: int
    ) -> None:
        transport = ScriptedTransport(script=[good_plan(), *no_preference()])
        planned = generate_layout(
            living,
            catalog(),
            room_type="living",
            budget_cents=budget,
            transport=transport,
        )
        assert_legal(planned, budget_cents=budget)


class TestDeterminism:
    def test_the_same_plan_and_seed_give_the_same_layout(self, living: RoomAnalysis) -> None:
        """5.4 L5: "the same plan + seed gives the same layout"."""

        def run() -> PlannedLayout:
            return generate_layout(
                living,
                catalog(),
                room_type="living",
                budget_cents=BUDGET,
                transport=ScriptedTransport(script=[good_plan(), *no_preference()]),
                seed=7,
            )

        first, second = run(), run()
        assert [(i.id, i.footprint) for i in first.layout.items] == [
            (i.id, i.footprint) for i in second.layout.items
        ]

    def test_a_different_seed_is_allowed_to_differ(self, living: RoomAnalysis) -> None:
        """ "Regenerate" changes the seed, so the layouts must not be pinned
        together -- but both must be legal."""

        def run(seed: int) -> PlannedLayout:
            return generate_layout(
                living,
                catalog(),
                room_type="living",
                budget_cents=BUDGET,
                transport=ScriptedTransport(script=[good_plan(), *no_preference()]),
                seed=seed,
            )

        assert_legal(run(1))
        assert_legal(run(2))


class TestSmallRooms:
    @pytest.mark.parametrize("room", ["bedroom", "narrow"])
    def test_a_room_that_barely_fits_anything_still_validates(
        self, request: pytest.FixtureRequest, room: str
    ) -> None:
        analysis = request.getfixturevalue(room)
        transport = ScriptedTransport(script=[good_plan(), *no_preference()])
        planned = generate_layout(
            analysis,
            catalog(),
            room_type="bedroom",
            budget_cents=BUDGET,
            transport=transport,
        )
        assert_legal(planned)
