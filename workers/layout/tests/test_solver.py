"""The placement solver (implementation-plan.md 5.4).

The plan's Phase 4 acceptance is a property: "for random rooms/budgets, every
generated layout has zero hard violations and total <= budget". That property
test is here, alongside the specific behaviours 5.4 calls for -- dependency
ordering, intent satisfaction, relaxation, and determinism.

Determinism gets its own test rather than being assumed. Without it,
"Regenerate" and "undo" are indistinguishable to a user, and the 60-case
human evaluation scores a different layout on every run.
"""

from __future__ import annotations

import pytest
from layout_helpers import load_room
from roomfittr_layout.engine import generate
from roomfittr_layout.room import RoomAnalysis, analyse
from roomfittr_layout.solver import (
    Candidate,
    Intent,
    Slot,
    SolverError,
    solve,
)
from roomfittr_layout.validator import validate

SOFA = Candidate("p-sofa", width_mm=2000.0, depth_mm=900.0, height_mm=800.0, price_cents=70_000)
SMALL_SOFA = Candidate(
    "p-sofa-s", width_mm=1500.0, depth_mm=850.0, height_mm=780.0, price_cents=45_000
)
COFFEE = Candidate("p-coffee", width_mm=1100.0, depth_mm=600.0, height_mm=420.0, price_cents=18_000)
RUG = Candidate("p-rug", width_mm=2400.0, depth_mm=1700.0, height_mm=12.0, price_cents=12_000)
SHELF = Candidate("p-shelf", width_mm=800.0, depth_mm=320.0, height_mm=1800.0, price_cents=22_000)
BED = Candidate("p-bed", width_mm=1550.0, depth_mm=2100.0, height_mm=1100.0, price_cents=90_000)
HUGE = Candidate("p-huge", width_mm=4200.0, depth_mm=1100.0, height_mm=900.0, price_cents=300_000)


def living_room_slots() -> list[Slot]:
    return [
        Slot("s1", "sofa", priority="must", candidates=[SOFA, SMALL_SOFA]),
        Slot(
            "s2",
            "coffee_table",
            intent=Intent(anchor_slot="s1"),
            candidates=[COFFEE],
        ),
        Slot("s3", "rug", intent=Intent(anchor_slot="s1"), candidates=[RUG]),
    ]


class TestBasicPlacement:
    def test_a_living_room_is_furnished(self, rectangular: RoomAnalysis) -> None:
        result = solve(living_room_slots(), rectangular)
        assert {p.slot_id for p in result.placements} == {"s1", "s2", "s3"}
        assert not result.dropped

    def test_the_result_passes_the_validator(self, rectangular: RoomAnalysis) -> None:
        """L6: the solver's own hard checks are incremental, so the full
        validator is the thing that actually has to be satisfied."""
        result = solve(living_room_slots(), rectangular)
        report = validate(result.items, rectangular)
        assert report.fits, [v.message for v in report.hard_violations]

    def test_an_empty_plan_produces_an_empty_layout(self, rectangular: RoomAnalysis) -> None:
        result = solve([], rectangular)
        assert result.placements == []
        assert result.dropped == []


class TestIntent:
    def test_a_sofa_goes_against_a_wall_facing_the_room(self, rectangular: RoomAnalysis) -> None:
        """5.3 marks a sofa `against_wall_preferred` and `faces: focal`. A
        sofa that satisfies every hard rule while facing the wall is the
        classic absurd-but-valid layout."""
        result = solve([Slot("s1", "sofa", candidates=[SOFA])], rectangular)
        sofa = result.placements[0].item

        # Its back should be near a wall: the footprint should touch the
        # room's boundary within the rule's gap.
        distance = sofa.footprint.polygon().distance(rectangular.floor.exterior)
        assert distance < 200.0, f"the sofa is {distance:.0f} mm from any wall"

        # And its front should point inwards, i.e. towards the room centre.
        centre = rectangular.floor.centroid
        to_centre = (
            centre.x - sofa.footprint.center_x_mm,
            centre.y - sofa.footprint.center_z_mm,
        )
        facing = sofa.footprint.front_normal()
        assert facing[0] * to_centre[0] + facing[1] * to_centre[1] > 0, "the sofa faces the wall"

    def test_a_named_wall_is_honoured(self, rectangular: RoomAnalysis) -> None:
        result = solve(
            [Slot("s1", "sofa", intent=Intent(anchor_wall="W2"), candidates=[SMALL_SOFA])],
            rectangular,
        )
        sofa = result.placements[0].item
        wall = rectangular.wall("W2")
        assert wall is not None
        # W2 is the x = 2300 wall, so the sofa should sit near it.
        assert sofa.footprint.center_x_mm > 1000.0
        assert not result.placements[0].relaxed

    def test_a_coffee_table_lands_in_front_of_its_sofa(self, rectangular: RoomAnalysis) -> None:
        result = solve(living_room_slots(), rectangular)
        by_slot = {p.slot_id: p.item for p in result.placements}
        sofa, table = by_slot["s1"].footprint, by_slot["s2"].footprint

        from roomfittr_layout.geometry import gap_between

        gap = gap_between(sofa, table)
        assert 300.0 <= gap <= 560.0, f"gap of {gap:.0f} mm is outside 5.3's band"

        # And in front, not behind.
        facing = sofa.front_normal()
        towards = (table.center_x_mm - sofa.center_x_mm, table.center_z_mm - sofa.center_z_mm)
        assert facing[0] * towards[0] + facing[1] * towards[1] > 0

    def test_a_rug_is_allowed_to_sit_under_the_furniture(self, rectangular: RoomAnalysis) -> None:
        result = solve(living_room_slots(), rectangular)
        by_slot = {p.slot_id: p.item for p in result.placements}

        from roomfittr_layout.geometry import separating_axis_overlap

        overlap = separating_axis_overlap(by_slot["s3"].footprint, by_slot["s1"].footprint)
        assert overlap > 0, "the rug is not under anything"
        assert validate(result.items, rectangular).fits


class TestOrdering:
    def test_anchors_are_placed_before_their_dependents(self, rectangular: RoomAnalysis) -> None:
        """5.4 step 1. A coffee table solved first would have nothing to be
        in front of, and would then be relaxed into a float."""
        slots = [
            Slot("s2", "coffee_table", intent=Intent(anchor_slot="s1"), candidates=[COFFEE]),
            Slot("s1", "sofa", candidates=[SOFA]),
        ]
        result = solve(slots, rectangular)
        order = [p.slot_id for p in result.placements]
        assert order.index("s1") < order.index("s2")

    def test_a_dependent_without_its_anchor_is_placed_freely(
        self, rectangular: RoomAnalysis
    ) -> None:
        """A coffee table in a plan with no sofa has nothing to be relative
        to. Placing it first and freely beats relaxing it into whatever space
        is left after the room has filled up."""
        result = solve([Slot("s2", "coffee_table", candidates=[COFFEE])], rectangular)
        assert len(result.placements) == 1
        assert validate(result.items, rectangular).fits

    def test_floor_coverings_go_last(self, rectangular: RoomAnalysis) -> None:
        result = solve(living_room_slots(), rectangular)
        assert [p.slot_id for p in result.placements][-1] == "s3"


class TestRelaxationAndFailure:
    def test_an_unsatisfiable_wall_intent_relaxes_rather_than_failing(
        self, rectangular: RoomAnalysis
    ) -> None:
        """5.4 step 2's ladder. The named wall has no run long enough, so the
        solver should use another wall and say it relaxed."""
        result = solve(
            [Slot("s1", "sofa", intent=Intent(anchor_wall="W4"), candidates=[SOFA])],
            rectangular,
        )
        assert len(result.placements) == 1
        assert validate(result.items, rectangular).fits

    def test_a_shortlist_is_walked_when_the_first_choice_does_not_fit(
        self, bedroom: RoomAnalysis
    ) -> None:
        """5.4 step 4: try the next product, smaller footprint first.

        The bedroom is 3.0 x 2.6 m, so the 4.2 m sofa cannot go anywhere and
        the solver has to fall through to the shortlist.
        """
        result = solve([Slot("s1", "sofa", candidates=[HUGE, SOFA, SMALL_SOFA])], bedroom)
        assert len(result.placements) == 1
        assert result.placements[0].item.footprint.width_mm < HUGE.width_mm

    def test_a_droppable_slot_is_dropped_with_a_readable_reason(
        self, bedroom: RoomAnalysis
    ) -> None:
        slots = [
            Slot("s1", "bed_frame", priority="must", candidates=[BED]),
            Slot("s2", "sofa", priority="nice", candidates=[HUGE]),
        ]
        result = solve(slots, bedroom)
        assert [d.slot_id for d in result.dropped] == ["s2"]
        reason = result.dropped[0].reason
        assert reason.endswith(".") and reason[0].isupper()
        assert "sofa" in reason

    def test_a_must_slot_that_cannot_be_placed_fails_the_layout(
        self, bedroom: RoomAnalysis
    ) -> None:
        """7.4: better to fail with an explanation than to ship a bedroom
        with no bed in it."""
        with pytest.raises(SolverError) as exc:
            solve([Slot("s1", "sofa", priority="must", candidates=[HUGE])], bedroom)
        assert exc.value.code == "ROOM_TOO_SMALL_FOR_BUDGET_OR_TYPE"
        assert exc.value.detail

    def test_a_slot_with_no_candidates_is_dropped_with_a_catalog_reason(
        self, rectangular: RoomAnalysis
    ) -> None:
        """4.11: the layout engine "degrades gracefully (skips a slot and
        says so) when a category is thin"."""
        result = solve([Slot("s1", "bookshelf", candidates=[])], rectangular)
        assert result.dropped and "catalog" in result.dropped[0].reason


class TestDeterminism:
    def test_the_same_plan_gives_the_same_layout(self, rectangular: RoomAnalysis) -> None:
        """5.4 step 5. Without this, "Regenerate" is indistinguishable from
        "undo" and the human eval set scores a different layout each run."""
        first = solve(living_room_slots(), rectangular, seed=7)
        second = solve(living_room_slots(), rectangular, seed=7)

        def poses(result: object) -> list[tuple[str, float, float, float]]:
            return [
                (
                    p.slot_id,
                    round(p.item.footprint.center_x_mm, 6),
                    round(p.item.footprint.center_z_mm, 6),
                    round(p.item.footprint.rotation_deg, 6),
                )
                for p in result.placements  # type: ignore[attr-defined]
            ]

        assert poses(first) == poses(second)

    def test_slot_order_in_the_plan_does_not_change_the_result(
        self, rectangular: RoomAnalysis
    ) -> None:
        """The dependency order is derived, so the planner listing slots in a
        different order must not move the furniture."""
        forward = solve(living_room_slots(), rectangular)
        backward = solve(list(reversed(living_room_slots())), rectangular)
        assert [
            (p.slot_id, round(p.item.footprint.center_x_mm, 3)) for p in forward.placements
        ] == [(p.slot_id, round(p.item.footprint.center_x_mm, 3)) for p in backward.placements]


class TestAcrossEveryFixtureRoom:
    """Phase 4's acceptance property, over the rooms Phase 0 hand-authored.

    "100% of eval cases produce a layout with zero hard violations and within
    budget." The real eval set needs a catalog; this is the same property on
    the six fixture rooms with a stand-in catalog.
    """

    ROOMS = [
        "rectangular-living",
        "small-bedroom",
        "l-shaped-living",
        "open-plan-boundary",
        "many-openings",
        "narrow-room",
    ]

    @pytest.mark.parametrize("room_name", ROOMS)
    def test_no_hard_violations_in_any_room(self, room_name: str) -> None:
        analysis = analyse(load_room(room_name))
        slots = [
            Slot("s1", "sofa", candidates=[SOFA, SMALL_SOFA]),
            Slot("s2", "coffee_table", intent=Intent(anchor_slot="s1"), candidates=[COFFEE]),
            Slot("s3", "bookshelf", priority="nice", candidates=[SHELF]),
            Slot("s4", "rug", priority="nice", intent=Intent(anchor_slot="s1"), candidates=[RUG]),
        ]
        layout = generate(slots, analysis)
        assert layout.report.fits, f"{room_name}: " + "; ".join(
            v.message for v in layout.report.hard_violations
        )

    @pytest.mark.parametrize("room_name", ROOMS)
    def test_the_layout_stays_within_budget(self, room_name: str) -> None:
        analysis = analyse(load_room(room_name))
        slots = [
            Slot("s1", "sofa", candidates=[SOFA, SMALL_SOFA]),
            Slot("s2", "coffee_table", intent=Intent(anchor_slot="s1"), candidates=[COFFEE]),
        ]
        budget = 200_000
        layout = generate(slots, analysis, budget_cents=budget)
        assert layout.report.total_price_cents is not None
        assert layout.report.total_price_cents <= budget
        assert layout.report.fits


class TestMetadata:
    def test_generation_meta_records_what_happened(self, bedroom: RoomAnalysis) -> None:
        """6.2 keeps `generation_meta` for explainability and regeneration."""
        slots = [
            Slot("s1", "bed_frame", priority="must", candidates=[BED]),
            Slot("s2", "sofa", priority="nice", candidates=[HUGE]),
        ]
        meta = solve(slots, bedroom, seed=42).as_meta()
        assert meta["seed"] == 42
        assert meta["placed"] == 1
        assert meta["dropped"][0]["category"] == "sofa"
