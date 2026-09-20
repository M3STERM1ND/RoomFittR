"""L6: the solve-validate-retry loop (implementation-plan.md 5.4).

The solver's hard checks are local by necessity -- they run per candidate
pose. Two hard rules are not local: circulation is a property of the whole
layout, and budget is a sum over it. 5.4 L6 closes that gap by running the
full validator on the finished layout and retrying without the violating
slot.

This is what makes Phase 4's acceptance criterion a guarantee rather than a
hope: "100% of eval cases produce a layout with zero hard violations and
within budget".
"""

from __future__ import annotations

import pytest
from layout_helpers import load_room
from roomfittr_layout.engine import MAX_PASSES, generate
from roomfittr_layout.room import RoomAnalysis, analyse
from roomfittr_layout.solver import Candidate, Intent, Slot, SolverError

SOFA = Candidate("p-sofa", width_mm=2000.0, depth_mm=900.0, height_mm=800.0, price_cents=70_000)
SMALL_SOFA = Candidate(
    "p-sofa-s", width_mm=1500.0, depth_mm=850.0, height_mm=780.0, price_cents=45_000
)
COFFEE = Candidate("p-coffee", width_mm=1100.0, depth_mm=600.0, height_mm=420.0, price_cents=18_000)
SHELF = Candidate("p-shelf", width_mm=800.0, depth_mm=320.0, height_mm=1800.0, price_cents=22_000)
WARDROBE = Candidate(
    "p-wrdb", width_mm=1800.0, depth_mm=650.0, height_mm=2000.0, price_cents=60_000
)
RUG = Candidate("p-rug", width_mm=2400.0, depth_mm=1700.0, height_mm=12.0, price_cents=12_000)
BED = Candidate("p-bed", width_mm=1550.0, depth_mm=2100.0, height_mm=1100.0, price_cents=90_000)
HUGE = Candidate("p-huge", width_mm=4200.0, depth_mm=1100.0, height_mm=900.0, price_cents=300_000)


class TestTheLoop:
    def test_a_layout_that_already_fits_is_returned_in_one_pass(
        self, rectangular: RoomAnalysis
    ) -> None:
        layout = generate(
            [
                Slot("s1", "sofa", candidates=[SOFA]),
                Slot("s2", "coffee_table", intent=Intent(anchor_slot="s1"), candidates=[COFFEE]),
            ],
            rectangular,
        )
        assert layout.report.fits
        assert layout.passes == 1
        assert not layout.result.dropped

    def test_an_overfilled_narrow_room_still_comes_out_legal(self, narrow: RoomAnalysis) -> None:
        """The case that motivated this module.

        A 5.2 x 1.9 m room leaves a walkable band barely wider than one
        person once the walls are accounted for, so a single badly-placed
        coffee table seals it. Whether that is avoided by the solver's
        circulation gate or repaired by this loop dropping a slot is an
        implementation detail; what must hold is that the layout the user
        sees has no hard violations.
        """
        slots = [
            Slot("s1", "sofa", priority="must", candidates=[SOFA, SMALL_SOFA]),
            Slot("s2", "coffee_table", intent=Intent(anchor_slot="s1"), candidates=[COFFEE]),
            Slot("s3", "bookshelf", priority="nice", candidates=[SHELF]),
            Slot("s4", "cabinet", priority="nice", candidates=[WARDROBE]),
            Slot("s5", "dresser", priority="nice", candidates=[WARDROBE]),
        ]
        layout = generate(slots, narrow)
        assert layout.report.fits, [v.message for v in layout.report.hard_violations]
        # The `must` slot survives whatever else happens.
        assert "s1" in {p.slot_id for p in layout.result.placements}

    def test_a_must_slot_is_never_dropped_to_make_room(self, narrow: RoomAnalysis) -> None:
        """A bedroom with no bed is not a layout. 7.4 would rather fail."""
        slots = [
            Slot("s1", "sofa", priority="must", candidates=[SOFA]),
            Slot("s2", "bookshelf", priority="nice", candidates=[SHELF]),
            Slot("s3", "cabinet", priority="nice", candidates=[WARDROBE]),
            Slot("s4", "dresser", priority="nice", candidates=[WARDROBE]),
        ]
        layout = generate(slots, narrow)
        placed = {p.slot_id for p in layout.result.placements}
        assert "s1" in placed
        assert layout.report.fits

    def test_the_lowest_priority_slot_goes_first(self, narrow: RoomAnalysis) -> None:
        """A global violation names nobody, so the choice has to be made on
        priority -- the item the user would least miss -- rather than on
        whatever happened to be solved last."""
        slots = [
            Slot("s1", "sofa", priority="must", candidates=[SOFA]),
            Slot("s2", "bookshelf", priority="should", candidates=[SHELF]),
            Slot("s3", "cabinet", priority="nice", candidates=[WARDROBE]),
            Slot("s4", "dresser", priority="nice", candidates=[WARDROBE]),
        ]
        layout = generate(slots, narrow)
        if layout.result.dropped:
            dropped = [d.slot_id for d in layout.result.dropped]
            # If anything with `should` priority went, something `nice`
            # should have gone first.
            if "s2" in dropped:
                assert {"s3", "s4"} & set(dropped)

    def test_a_drop_reason_is_a_sentence_from_the_validator(self, narrow: RoomAnalysis) -> None:
        """5.5 already writes a sentence a person can act on. Writing a
        second, vaguer one here would be a downgrade."""
        slots = [
            Slot("s1", "sofa", priority="must", candidates=[SOFA]),
            Slot("s2", "cabinet", priority="nice", candidates=[WARDROBE]),
            Slot("s3", "dresser", priority="nice", candidates=[WARDROBE]),
            Slot("s4", "bookshelf", priority="nice", candidates=[SHELF]),
        ]
        layout = generate(slots, narrow)
        for dropped in layout.result.dropped:
            assert dropped.reason.strip()
            assert dropped.reason.endswith(".")

    def test_a_must_slot_that_cannot_be_placed_raises(self, bedroom: RoomAnalysis) -> None:
        with pytest.raises(SolverError):
            generate([Slot("s1", "sofa", priority="must", candidates=[HUGE])], bedroom)


class TestBudget:
    def test_an_over_budget_layout_sheds_items(self, rectangular: RoomAnalysis) -> None:
        """H7 is a sum over the layout, so the solver cannot see it while
        placing. The loop is where it gets enforced."""
        slots = [
            Slot("s1", "sofa", priority="must", candidates=[SOFA]),
            Slot("s2", "bookshelf", priority="nice", candidates=[SHELF]),
            Slot("s3", "cabinet", priority="nice", candidates=[WARDROBE]),
        ]
        layout = generate(slots, rectangular, budget_cents=80_000)
        assert layout.report.fits
        assert layout.report.total_price_cents is not None
        assert layout.report.total_price_cents <= 80_000

    def test_a_manual_layout_may_exceed_its_budget(self, rectangular: RoomAnalysis) -> None:
        """5.5: H7 applies to AI layouts. A user who chose an expensive sofa
        should be told, not have it deleted."""
        layout = generate(
            [Slot("s1", "sofa", priority="must", candidates=[SOFA])],
            rectangular,
            budget_cents=1,
            enforce_budget=False,
        )
        assert layout.report.fits
        assert {v.code for v in layout.report.violations} == {"H7_OVER_BUDGET"}
        assert layout.result.placements

    def test_a_budget_nothing_can_meet_still_returns_a_report(
        self, rectangular: RoomAnalysis
    ) -> None:
        """When the only remaining slot is `must` and it is over budget,
        there is nothing left to drop. Returning the report beats looping:
        5.5 says the report is what the UI shows."""
        layout = generate(
            [Slot("s1", "sofa", priority="must", candidates=[SOFA])],
            rectangular,
            budget_cents=1,
        )
        assert not layout.report.fits
        assert "H7_OVER_BUDGET" in {v.code for v in layout.report.violations}
        assert layout.passes < MAX_PASSES, "the loop did not terminate early"


class TestMetadata:
    def test_the_meta_records_the_passes_and_the_drops(self, narrow: RoomAnalysis) -> None:
        slots = [
            Slot("s1", "sofa", priority="must", candidates=[SOFA]),
            Slot("s2", "cabinet", priority="nice", candidates=[WARDROBE]),
            Slot("s3", "dresser", priority="nice", candidates=[WARDROBE]),
        ]
        meta = generate(slots, narrow, seed=11).as_meta()
        assert meta["seed"] == 11
        assert meta["solver_passes"] >= 1
        assert isinstance(meta["dropped"], list)


class TestEveryFixtureRoom:
    """Phase 4's acceptance property across Phase 0's six rooms."""

    ROOMS = [
        "rectangular-living",
        "small-bedroom",
        "l-shaped-living",
        "open-plan-boundary",
        "many-openings",
        "narrow-room",
    ]

    @pytest.mark.parametrize("room_name", ROOMS)
    @pytest.mark.parametrize("budget", [150_000, 400_000])
    def test_zero_hard_violations_and_within_budget(self, room_name: str, budget: int) -> None:
        analysis = analyse(load_room(room_name))
        slots = [
            Slot("s1", "sofa", priority="should", candidates=[SOFA, SMALL_SOFA]),
            Slot("s2", "coffee_table", intent=Intent(anchor_slot="s1"), candidates=[COFFEE]),
            Slot("s3", "bookshelf", priority="nice", candidates=[SHELF]),
            Slot("s4", "rug", priority="nice", intent=Intent(anchor_slot="s1"), candidates=[RUG]),
            Slot("s5", "cabinet", priority="nice", candidates=[WARDROBE]),
        ]
        layout = generate(slots, analysis, budget_cents=budget)
        assert layout.report.fits, f"{room_name} @ {budget}: " + "; ".join(
            v.message for v in layout.report.hard_violations
        )
        assert layout.report.total_price_cents is not None
        assert layout.report.total_price_cents <= budget

    @pytest.mark.parametrize("room_name", ROOMS)
    def test_a_bedroom_plan_works_everywhere_too(self, room_name: str) -> None:
        """A different room type over the same geometry, so the property is
        not accidentally specific to living-room furniture."""
        analysis = analyse(load_room(room_name))
        slots = [
            Slot("s1", "bed_frame", priority="should", candidates=[BED]),
            Slot("s2", "nightstand", intent=Intent(anchor_slot="s1"), candidates=[COFFEE]),
            Slot("s3", "dresser", priority="nice", candidates=[WARDROBE]),
        ]
        layout = generate(slots, analysis, budget_cents=500_000)
        assert layout.report.fits, f"{room_name}: " + "; ".join(
            v.message for v in layout.report.hard_violations
        )


class TestPerformance:
    """Phase 4's stated budget: "solver alone <= 5 s".

    A generous bound rather than a tight one, because CI machines vary and a
    flaky performance test gets deleted rather than fixed. What it catches is
    a regression of the kind this code already had: the hard check ran a
    shapely point-in-polygon per corner on every candidate pose, and a float
    intent generates thousands of those.
    """

    @pytest.mark.parametrize("room_name", ["many-openings", "l-shaped-living"])
    def test_a_five_item_layout_is_generated_within_budget(self, room_name: str) -> None:
        import time

        analysis = analyse(load_room(room_name))
        slots = [
            Slot("s1", "sofa", priority="must", candidates=[SOFA, SMALL_SOFA]),
            Slot("s2", "coffee_table", intent=Intent(anchor_slot="s1"), candidates=[COFFEE]),
            Slot("s3", "bookshelf", priority="nice", candidates=[SHELF]),
            Slot("s4", "rug", priority="nice", intent=Intent(anchor_slot="s1"), candidates=[RUG]),
            Slot("s5", "cabinet", priority="nice", candidates=[WARDROBE]),
        ]

        started = time.perf_counter()
        layout = generate(slots, analysis, budget_cents=400_000)
        elapsed = time.perf_counter() - started

        assert layout.report.fits
        assert elapsed < 15.0, f"{room_name} took {elapsed:.1f}s; the plan budgets 5s"
