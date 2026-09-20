"""L2-L4 without an LLM (implementation-plan.md 5.4).

Two groups of tests here, and the second is the important one.

The first covers the template plans and the shortlist/pick arithmetic. The
second is 9.1's **LLM chaos tests**: "the LLM returns invalid JSON /
nonexistent IDs / absurd budget shares -> the engine still returns a valid
layout via clamping or template". Decision 4 says the model never gets to
break a room, and that claim is only worth anything if something tries to.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import jsonschema
import pytest
from layout_helpers import load_room
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT7
from roomfittr_layout.engine import generate
from roomfittr_layout.planner import (
    BUDGET_RESERVE,
    MAX_SLOTS,
    LayoutPlan,
    PlanSlot,
    Product,
    pick,
    plan_for,
    post_check,
    room_types,
    shortlist,
    template_plan,
)
from roomfittr_layout.room import RoomAnalysis, analyse
from roomfittr_layout.rules import CATEGORY_VOCABULARY

SCHEMA_DIR = Path(__file__).resolve().parents[3] / "packages" / "schemas" / "src"


def _schema_registry() -> Registry:
    resources = []
    for path in SCHEMA_DIR.glob("*.schema.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        resource = Resource.from_contents(document, default_specification=DRAFT7)
        resources.append((path.name, resource))
        resources.append((document["$id"], resource))
    return Registry().with_resources(resources)


@pytest.fixture(scope="module")
def plan_validator() -> jsonschema.protocols.Validator:
    schema = json.loads((SCHEMA_DIR / "layout-plan.schema.json").read_text(encoding="utf-8"))
    return jsonschema.Draft7Validator(schema, registry=_schema_registry())


def catalog() -> list[Product]:
    """A stand-in for `layout_candidates` (4.11).

    Three price points per category so the budget pass has somewhere to go in
    both directions, and sizes that a real room can actually hold.
    """
    rows: list[Product] = []
    specs = {
        "sofa": [(1500.0, 850.0, 780.0), (2000.0, 900.0, 800.0), (2300.0, 950.0, 850.0)],
        "coffee_table": [(900.0, 500.0, 400.0), (1100.0, 600.0, 420.0), (1300.0, 700.0, 440.0)],
        "rug": [(1600.0, 1200.0, 10.0), (2000.0, 1400.0, 12.0), (2400.0, 1700.0, 14.0)],
        "tv_stand": [(1000.0, 400.0, 500.0), (1400.0, 420.0, 520.0), (1800.0, 450.0, 550.0)],
        "armchair": [(700.0, 750.0, 900.0), (800.0, 820.0, 950.0), (900.0, 880.0, 1000.0)],
        "floor_lamp": [(350.0, 350.0, 1500.0), (400.0, 400.0, 1650.0), (450.0, 450.0, 1800.0)],
        "bookshelf": [(600.0, 300.0, 1600.0), (800.0, 320.0, 1800.0), (1000.0, 350.0, 2000.0)],
        "bed_frame": [(1400.0, 2000.0, 1000.0), (1550.0, 2100.0, 1100.0), (1800.0, 2150.0, 1200.0)],
        "nightstand": [(400.0, 380.0, 550.0), (450.0, 400.0, 600.0), (500.0, 420.0, 650.0)],
        "dresser": [(900.0, 450.0, 800.0), (1200.0, 480.0, 850.0), (1500.0, 500.0, 900.0)],
        "desk": [(1100.0, 550.0, 740.0), (1400.0, 600.0, 750.0), (1600.0, 700.0, 760.0)],
        "office_chair": [(600.0, 600.0, 1100.0), (650.0, 640.0, 1150.0), (700.0, 680.0, 1200.0)],
        "dining_table": [(1200.0, 800.0, 750.0), (1600.0, 900.0, 760.0), (1900.0, 1000.0, 770.0)],
        "dining_chair": [(450.0, 500.0, 900.0), (480.0, 520.0, 920.0), (500.0, 550.0, 950.0)],
        "cabinet": [(800.0, 400.0, 1200.0), (1000.0, 420.0, 1400.0), (1200.0, 450.0, 1600.0)],
    }
    prices = [15_000, 40_000, 95_000]
    styles = [("modern",), ("scandinavian",), ("traditional",)]
    for category, sizes in specs.items():
        for index, (width, depth, height) in enumerate(sizes):
            rows.append(
                Product(
                    id=f"{category}-{index}",
                    category=category,
                    width_mm=width,
                    depth_mm=depth,
                    height_mm=height,
                    price_cents=prices[index],
                    style_tags=styles[index],
                )
            )
    return rows


@pytest.fixture(scope="module")
def living() -> RoomAnalysis:
    return analyse(load_room("rectangular-living"))


class TestTemplatePlans:
    @pytest.mark.parametrize("room_type", room_types())
    def test_every_room_type_has_a_workable_template(self, room_type: str) -> None:
        """5.4's fallback is the floor the product stands on: an LLM failure
        must still produce a layout."""
        plan = template_plan(room_type)
        assert plan.from_template
        assert 1 <= len(plan.slots) <= MAX_SLOTS
        assert any(slot.priority == "must" for slot in plan.slots)

    @pytest.mark.parametrize("room_type", room_types())
    def test_every_template_category_is_in_the_vocabulary(self, room_type: str) -> None:
        """4.2's vocabulary is closed, and a template naming something outside
        it would be silently dropped by the post-check."""
        for slot in template_plan(room_type).slots:
            assert slot.category in CATEGORY_VOCABULARY, slot.category

    @pytest.mark.parametrize("room_type", room_types())
    def test_anchors_point_at_slots_that_exist(self, room_type: str) -> None:
        """The templates name their anchor by category because that is how a
        person describes it; the ids have to be resolved or every dependent
        silently places freely."""
        plan = template_plan(room_type)
        ids = {slot.slot_id for slot in plan.slots}
        for slot in plan.slots:
            if slot.anchor_slot is not None:
                assert slot.anchor_slot in ids

    def test_a_bedroom_template_contains_a_bed(self) -> None:
        categories = {slot.category for slot in template_plan("bedroom").slots}
        assert "bed_frame" in categories

    def test_an_unknown_room_type_still_gets_a_plan(self) -> None:
        """ "Conservatory" is not in the vocabulary, and refusing to plan would
        violate "a layout is always produced"."""
        plan = template_plan("conservatory")
        assert plan.slots
        assert plan.room_type == "living"

    @pytest.mark.parametrize("room_type", room_types())
    def test_the_plan_matches_the_schema(
        self, room_type: str, plan_validator: jsonschema.protocols.Validator
    ) -> None:
        plan_validator.validate(template_plan(room_type).as_json())


class TestPostChecks:
    def test_shares_are_renormalised_with_a_reserve(self, living: RoomAnalysis) -> None:
        """5.4: renormalised to 100% with a 5% reserve. Prices move between
        the shortlist and the checkout, and a layout that spends to the cent
        is over budget by the time the user clicks through."""
        plan = post_check(template_plan("living"), living)
        total = sum(slot.budget_share for slot in plan.slots)
        assert total == pytest.approx(1.0 - BUDGET_RESERVE, abs=1e-6)

    def test_slots_are_capped_at_ten(self, living: RoomAnalysis) -> None:
        many = LayoutPlan(
            room_type="living",
            slots=[
                PlanSlot(f"S{i}", "armchair", "nice" if i > 2 else "must", 0.05) for i in range(20)
            ],
        )
        checked = post_check(many, living)
        assert len(checked.slots) == MAX_SLOTS
        # The `must` slots survive the cull.
        assert sum(1 for slot in checked.slots if slot.priority == "must") == 3

    def test_unaffordable_slots_are_dropped_lowest_priority_first(
        self, living: RoomAnalysis
    ) -> None:
        plan = LayoutPlan(
            room_type="living",
            slots=[
                PlanSlot("S1", "sofa", "must", 0.5),
                PlanSlot("S2", "bookshelf", "nice", 0.5),
            ],
        )
        checked = post_check(
            plan,
            living,
            budget_cents=20_000,
            # Nothing in either category is this cheap at a 50% share of
            # 20,000 cents, but `must` is never dropped.
            min_price_by_category={"sofa": 15_000, "bookshelf": 15_000},
        )
        assert [slot.slot_id for slot in checked.slots] == ["S1"]

    def test_a_must_slot_is_never_dropped_for_affordability(self, living: RoomAnalysis) -> None:
        """If a `must` is unaffordable the whole layout is, and the solver
        says so with a sentence. The planner quietly removing the bed from a
        bedroom would be worse."""
        plan = LayoutPlan(room_type="bedroom", slots=[PlanSlot("S1", "bed_frame", "must", 1.0)])
        checked = post_check(
            plan, living, budget_cents=100, min_price_by_category={"bed_frame": 90_000}
        )
        assert [slot.category for slot in checked.slots] == ["bed_frame"]


class TestChaos:
    """9.1's LLM chaos tests.

    Decision 4 claims the model never gets to break a room. These are the
    tests that make the claim mean something.
    """

    def test_a_category_that_does_not_exist_is_dropped(self, living: RoomAnalysis) -> None:
        plan = LayoutPlan(
            room_type="living",
            slots=[
                PlanSlot("S1", "sofa", "must", 0.5),
                PlanSlot("S2", "chandelier", "should", 0.5),
                PlanSlot("S3", "hot tub", "nice", 0.5),
            ],
        )
        checked = post_check(plan, living)
        assert [slot.category for slot in checked.slots] == ["sofa"]

    def test_a_wall_from_another_room_is_ignored(self, living: RoomAnalysis) -> None:
        """A dangling wall anchor is worse than none: the solver would relax
        away from a perfectly good wall it was never told about."""
        plan = LayoutPlan(
            room_type="living",
            slots=[PlanSlot("S1", "sofa", "must", 1.0, anchor_wall="W99")],
        )
        assert post_check(plan, living).slots[0].anchor_wall is None

    def test_a_real_wall_is_kept(self, living: RoomAnalysis) -> None:
        plan = LayoutPlan(
            room_type="living",
            slots=[PlanSlot("S1", "sofa", "must", 1.0, anchor_wall="W2")],
        )
        assert post_check(plan, living).slots[0].anchor_wall == "W2"

    def test_an_anchor_on_a_slot_that_was_dropped_is_cleared(self, living: RoomAnalysis) -> None:
        plan = LayoutPlan(
            room_type="living",
            slots=[
                PlanSlot("S1", "chandelier", "should", 0.5),
                PlanSlot("S2", "coffee_table", "should", 0.5, anchor_slot="S1"),
            ],
        )
        checked = post_check(plan, living)
        assert [slot.category for slot in checked.slots] == ["coffee_table"]
        assert checked.slots[0].anchor_slot is None

    def test_absurd_budget_shares_are_renormalised(self, living: RoomAnalysis) -> None:
        plan = LayoutPlan(
            room_type="living",
            slots=[
                PlanSlot("S1", "sofa", "must", 40.0),
                PlanSlot("S2", "rug", "should", 900.0),
            ],
        )
        checked = post_check(plan, living)
        assert sum(slot.budget_share for slot in checked.slots) == pytest.approx(
            1.0 - BUDGET_RESERVE, abs=1e-6
        )

    def test_negative_and_nan_shares_do_not_poison_the_plan(self, living: RoomAnalysis) -> None:
        """NaN fails its own equality test, which is how it is caught before
        it reaches the renormalisation and turns every share into NaN."""
        plan = LayoutPlan(
            room_type="living",
            slots=[
                PlanSlot("S1", "sofa", "must", float("nan")),
                PlanSlot("S2", "rug", "should", -5.0),
                PlanSlot("S3", "armchair", "nice", float("inf")),
            ],
        )
        checked = post_check(plan, living)
        assert len(checked.slots) == 3
        for slot in checked.slots:
            assert math.isfinite(slot.budget_share)
            assert slot.budget_share > 0

    def test_duplicate_slot_ids_are_deduplicated(self, living: RoomAnalysis) -> None:
        """Two slots with one id would collide in the solver's placement map
        and silently lose one of them."""
        plan = LayoutPlan(
            room_type="living",
            slots=[
                PlanSlot("S1", "sofa", "must", 0.5),
                PlanSlot("S1", "armchair", "nice", 0.5),
            ],
        )
        checked = post_check(plan, living)
        assert len(checked.slots) == 1

    def test_an_entirely_invalid_plan_falls_back_to_the_template(
        self, living: RoomAnalysis
    ) -> None:
        """The end of 5.4's fallback chain."""
        plan = LayoutPlan(
            room_type="bedroom",
            slots=[PlanSlot("S1", "spaceship", "must", 1.0)],
        )
        checked = post_check(plan, living)
        assert checked.from_template
        assert "bed_frame" in {slot.category for slot in checked.slots}

    def test_an_empty_plan_falls_back_to_the_template(self, living: RoomAnalysis) -> None:
        checked = post_check(LayoutPlan(room_type="office", slots=[]), living)
        assert checked.from_template
        assert checked.slots

    @pytest.mark.parametrize("room_type", room_types())
    def test_a_chaotic_plan_still_produces_a_valid_layout(self, room_type: str) -> None:
        """The whole claim, end to end: whatever the model returns, the user
        gets a layout with no hard violations."""
        analysis = analyse(load_room("rectangular-living"))
        nonsense = LayoutPlan(
            room_type=room_type,
            slots=[
                PlanSlot("S1", "teleporter", "must", float("nan")),
                PlanSlot("S1", "sofa", "must", -1.0, anchor_wall="W404"),
                *[PlanSlot(f"S{i + 100}", "armchair", "nice", 99.0) for i in range(30)],
            ],
        )
        _, slots = plan_for(
            analysis,
            catalog(),
            room_type=room_type,
            budget_cents=400_000,
            proposed=nonsense,
        )
        layout = generate(slots, analysis, budget_cents=400_000)
        assert layout.report.fits, [v.message for v in layout.report.hard_violations]


class TestShortlistAndPick:
    def test_a_shortlist_only_offers_the_right_category(self, living: RoomAnalysis) -> None:
        slot = PlanSlot("S1", "sofa", "must", 0.4)
        options = shortlist(slot, catalog(), living, budget_cents=300_000)
        assert options
        assert {product.category for product in options} == {"sofa"}

    def test_a_shortlist_respects_the_allocation(self, living: RoomAnalysis) -> None:
        """5.4 L3 allows 15% slack: the budget split is a guess, and one slot
        running slightly over is usually paid for by another running under."""
        slot = PlanSlot("S1", "sofa", "must", 0.1)
        options = shortlist(slot, catalog(), living, budget_cents=200_000)
        allocation = slot.allocation_cents(200_000)
        assert all(product.price_cents <= allocation * 1.15 for product in options)

    def test_nothing_taller_than_the_ceiling_is_offered(self) -> None:
        analysis = analyse(load_room("rectangular-living"))
        tall = Product("tall", "bookshelf", 800.0, 320.0, 9000.0, 20_000)
        options = shortlist(
            PlanSlot("S1", "bookshelf", "nice", 0.5),
            [*catalog(), tall],
            analysis,
            budget_cents=200_000,
        )
        assert all(product.id != "tall" for product in options)

    def test_style_is_preferred_when_asked_for(self, living: RoomAnalysis) -> None:
        slot = PlanSlot("S1", "sofa", "must", 0.5)
        options = shortlist(slot, catalog(), living, budget_cents=300_000, style="traditional")
        assert "traditional" in options[0].style_tags

    def test_the_pick_stays_within_budget(self, living: RoomAnalysis) -> None:
        plan = post_check(template_plan("living"), living)
        shortlists = {
            slot.slot_id: shortlist(slot, catalog(), living, budget_cents=150_000)
            for slot in plan.slots
        }
        chosen = pick(plan, shortlists, budget_cents=150_000)
        total = sum(options[0].price_cents for options in chosen.values() if options)
        assert total <= 150_000

    def test_leftover_budget_is_spent_on_the_must_slots(self, living: RoomAnalysis) -> None:
        """5.4: upgrade within the shortlist when more than 10% is left. A
        budget deliberately left unspent is not a feature anyone asked for."""
        plan = LayoutPlan(room_type="living", slots=[PlanSlot("S1", "sofa", "must", 0.95)])
        shortlists = {
            "S1": sorted(
                [p for p in catalog() if p.category == "sofa"],
                key=lambda p: p.price_cents,
            )
        }
        chosen = pick(plan, shortlists, budget_cents=200_000)
        assert chosen["S1"][0].price_cents > shortlists["S1"][0].price_cents


class TestEndToEnd:
    @pytest.mark.parametrize("room_type", room_types())
    @pytest.mark.parametrize(
        "room_name", ["rectangular-living", "small-bedroom", "l-shaped-living"]
    )
    def test_a_template_plan_furnishes_every_room(self, room_type: str, room_name: str) -> None:
        """Phase 4's acceptance property, now driven by real plans rather than
        hand-written slot lists."""
        analysis = analyse(load_room(room_name))
        budget = 400_000
        plan, slots = plan_for(analysis, catalog(), room_type=room_type, budget_cents=budget)
        assert plan.slots

        layout = generate(slots, analysis, budget_cents=budget)
        assert layout.report.fits, f"{room_type} in {room_name}: " + "; ".join(
            v.message for v in layout.report.hard_violations
        )
        assert layout.report.total_price_cents is not None
        assert layout.report.total_price_cents <= budget

    def test_a_tiny_budget_still_produces_something(self) -> None:
        """4.11: the engine "degrades gracefully (skips a slot and says so)"."""
        analysis = analyse(load_room("rectangular-living"))
        plan, slots = plan_for(analysis, catalog(), room_type="living", budget_cents=20_000)
        layout = generate(slots, analysis, budget_cents=20_000)
        assert layout.report.fits
        assert layout.report.total_price_cents is not None
        assert layout.report.total_price_cents <= 20_000

    def test_an_empty_catalog_does_not_crash(self) -> None:
        """Phase 3 may not have run. A layout of nothing beats an exception."""
        analysis = analyse(load_room("rectangular-living"))
        _, slots = plan_for(analysis, [], room_type="living", budget_cents=100_000)
        layout = generate(slots, analysis, budget_cents=100_000)
        assert layout.report.fits
        assert layout.result.placements == []
