"""The deterministic validator, rule by rule (implementation-plan.md 5.5).

The plan asks for "every H/S rule with pass/fail fixtures", and that pairing
is the point: a rule that only ever fires proves nothing, and neither does one
that never does. Each rule here gets a layout that satisfies it and one that
breaks it, differing only in the thing under test.

The rooms are Phase 0's hand-authored fixtures, so the validator is exercised
against the same geometry the solver and the web app will see.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from layout_helpers import codes, item, load_room
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT7
from roomfittr_layout.room import RoomAnalysis
from roomfittr_layout.validator import (
    COMFORTABLE_WALKWAY_MM,
    PlacedItem,
    validate,
)

SCHEMA_DIR = Path(__file__).resolve().parents[3] / "packages" / "schemas" / "src"


def _schema_registry() -> Registry:
    """A registry that can resolve the schemas' cross-file `$ref`s.

    `RefResolver` is the obvious API and is deprecated; `referencing` is what
    jsonschema 4.18+ uses underneath. Worth using directly rather than
    carrying a DeprecationWarning through every test run.
    """
    resources = []
    for path in SCHEMA_DIR.glob("*.schema.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        resource = Resource.from_contents(document, default_specification=DRAFT7)
        # Registered under both the filename and the $id, because the schemas
        # reference each other by relative filename.
        resources.append((path.name, resource))
        resources.append((document["$id"], resource))
    return Registry().with_resources(resources)


@pytest.fixture(scope="module")
def report_validator() -> jsonschema.protocols.Validator:
    schema = json.loads((SCHEMA_DIR / "validation-report.schema.json").read_text(encoding="utf-8"))
    return jsonschema.Draft7Validator(schema, registry=_schema_registry())


def sofa_against_w1(
    *,
    x: float = -1200.0,
    z: float = -1450.0,
    width: float = 2000.0,
    rotation: float = 180.0,
    price_cents: int = 0,
    price_age_days: int = 0,
    available: bool = True,
) -> PlacedItem:
    """A 2 m sofa on the window wall, facing into the room.

    W1 runs from (-2300, -1900) to (2300, -1900), so its inward normal is +Z
    and a sofa with its back to it faces +Z, which is a rotation of 180.
    """
    return item(
        "sofa1",
        "sofa",
        x=x,
        z=z,
        width=width,
        depth=900.0,
        height=800.0,
        rotation=rotation,
        price_cents=price_cents,
        price_age_days=price_age_days,
        available=available,
    )


class TestHardRules:
    def test_a_sensible_layout_fits(self, rectangular: RoomAnalysis) -> None:
        report = validate([sofa_against_w1()], rectangular)
        assert report.fits, [v.message for v in report.hard_violations]

    def test_h1_an_item_outside_the_room(self, rectangular: RoomAnalysis) -> None:
        report = validate([sofa_against_w1(z=-2200.0)], rectangular)
        assert not report.fits
        assert "H1_OUTSIDE_FLOOR" in codes(report)
        violation = report.items["sofa1"][0]
        assert violation.measured_mm is not None and violation.measured_mm > 0
        assert "outside the room" in violation.message

    def test_h1_tolerates_flush_against_a_wall(self, rectangular: RoomAnalysis) -> None:
        """5.5's 5 mm tolerance. The solver places items flush, and without
        the tolerance the validator rejects its own output."""
        flush = sofa_against_w1(z=-1900.0 + 450.0)
        assert "H1_OUTSIDE_FLOOR" not in codes(validate([flush], rectangular))

    def test_h2_two_items_overlapping(self, rectangular: RoomAnalysis) -> None:
        pair = [
            sofa_against_w1(),
            item(
                "table1",
                "coffee_table",
                x=-1200.0,
                z=-1400.0,
                width=1100.0,
                depth=600.0,
                height=420.0,
            ),
        ]
        report = validate(pair, rectangular)
        assert not report.fits
        assert "H2_ITEM_OVERLAP" in codes(report)
        # Both items are told, because an overlap is never about one alone.
        assert report.items["sofa1"] and report.items["table1"]
        assert report.items["sofa1"][0].related_ids == ["table1"]

    def test_h2_allows_a_rug_under_a_sofa(self, rectangular: RoomAnalysis) -> None:
        """A rug is a floor covering: 5.5 H2 exempts it explicitly, and a
        layout that could not put a rug under a sofa would be useless."""
        pair = [
            sofa_against_w1(),
            item("rug1", "rug", x=-1100.0, z=-1000.0, width=2200.0, depth=1600.0, height=10.0),
        ]
        assert "H2_ITEM_OVERLAP" not in codes(validate(pair, rectangular))

    def test_h2_still_rejects_a_rug_on_a_rug(self, rectangular: RoomAnalysis) -> None:
        pair = [
            item("rug1", "rug", x=0.0, z=0.0, width=2400.0, depth=1600.0, height=10.0),
            item("rug2", "rug", x=200.0, z=100.0, width=2000.0, depth=1400.0, height=12.0),
        ]
        assert "H2_ITEM_OVERLAP" in codes(validate(pair, rectangular))

    def test_h2_ignores_items_at_different_heights(self, rectangular: RoomAnalysis) -> None:
        """A wall shelf above a sideboard shares a footprint and not a volume."""
        pair = [
            item(
                "side1", "console_table", x=0.0, z=-1600.0, width=1200.0, depth=400.0, height=800.0
            ),
            item(
                "shelf1",
                "bookshelf",
                x=0.0,
                z=-1600.0,
                width=1200.0,
                depth=300.0,
                height=300.0,
                elevation=1400.0,
            ),
        ]
        assert "H2_ITEM_OVERLAP" not in codes(validate(pair, rectangular))

    def test_h3_overlapping_a_fixed_obstacle(self, rectangular: RoomAnalysis) -> None:
        import copy

        from roomfittr_layout.room import Obstacle, analyse

        raw = copy.deepcopy(load_room("rectangular-living"))
        raw["fixed_obstacles"] = [
            {
                "id": "F1",
                "label": "radiator",
                "center": [0, -1800],
                "size": [1200, 150, 600],
                "yaw_deg": 0,
                "source": "detected",
            }
        ]
        with_radiator = analyse(raw)
        assert isinstance(with_radiator.obstacles[0], Obstacle)

        clash = item(
            "shelf1", "bookshelf", x=0.0, z=-1750.0, width=900.0, depth=300.0, height=1800.0
        )
        report = validate([clash], with_radiator)
        assert "H3_OBSTACLE_OVERLAP" in codes(report)
        assert "radiator" in report.items["shelf1"][0].message

    def test_h4_blocking_the_door(self, rectangular: RoomAnalysis) -> None:
        """The door is on W3 at offset 1200, so its keep-out reaches into the
        room around (1100, 1900)."""
        blocker = item(
            "shelf1", "bookshelf", x=1100.0, z=1500.0, width=900.0, depth=350.0, height=1800.0
        )
        report = validate([blocker], rectangular)
        assert not report.fits
        assert "H4_DOOR_KEEPOUT" in codes(report)

    def test_h4_allows_a_rug_across_a_doorway(self, rectangular: RoomAnalysis) -> None:
        """You can walk on a rug. 5.5 H4 exempts floor coverings."""
        rug = item("rug1", "rug", x=1100.0, z=1500.0, width=1600.0, depth=800.0, height=10.0)
        assert "H4_DOOR_KEEPOUT" not in codes(validate([rug], rectangular))

    def test_h5_taller_than_the_ceiling(self, rectangular: RoomAnalysis) -> None:
        tall = item(
            "shelf1", "bookshelf", x=-1400.0, z=-1600.0, width=900.0, depth=300.0, height=2700.0
        )
        report = validate([tall], rectangular)
        assert not report.fits
        assert "H5_TOO_TALL" in codes(report)
        violation = next(v for v in report.items["shelf1"] if v.code == "H5_TOO_TALL")
        assert violation.limit_mm == 2700 - 50

    def test_h5_allows_a_tall_shelf_that_clears(self, rectangular: RoomAnalysis) -> None:
        ok = item(
            "shelf1", "bookshelf", x=-1400.0, z=-1600.0, width=900.0, depth=300.0, height=2400.0
        )
        assert "H5_TOO_TALL" not in codes(validate([ok], rectangular))

    def test_h6_a_wall_of_furniture_across_the_room(self, narrow: RoomAnalysis) -> None:
        """The rule that needs a flood fill rather than pairwise distances:
        each item can be a comfortable distance from everything and still
        seal the room in half."""
        barricade = [
            item("s1", "bookshelf", x=-2000.0, z=0.0, width=350.0, depth=1900.0, height=1800.0),
            item("s2", "bookshelf", x=-1600.0, z=0.0, width=350.0, depth=1900.0, height=1800.0),
        ]
        report = validate(barricade, narrow)
        assert not report.fits
        assert "H6_CIRCULATION_BLOCKED" in codes(report)

    def test_h6_passes_for_a_normal_room(self, rectangular: RoomAnalysis) -> None:
        report = validate([sofa_against_w1()], rectangular)
        assert "H6_CIRCULATION_BLOCKED" not in codes(report)
        assert report.free_floor_pct is not None and report.free_floor_pct > 80

    def test_h7_over_budget(self, rectangular: RoomAnalysis) -> None:
        report = validate([sofa_against_w1(price_cents=150_000)], rectangular, budget_cents=100_000)
        assert not report.fits
        assert "H7_OVER_BUDGET" in codes(report)
        assert "$500" in next(v for v in report.violations if v.code == "H7_OVER_BUDGET").message

    def test_h7_is_only_a_warning_for_a_manually_edited_layout(
        self, rectangular: RoomAnalysis
    ) -> None:
        """5.5: H7 applies to AI layouts. A user who deliberately picks an
        expensive sofa should be told, not blocked."""
        report = validate(
            [sofa_against_w1(price_cents=150_000)],
            rectangular,
            budget_cents=100_000,
            enforce_budget=False,
        )
        assert report.fits
        assert "H7_OVER_BUDGET" in codes(report)

    def test_no_budget_means_no_budget_rule(self, rectangular: RoomAnalysis) -> None:
        report = validate([sofa_against_w1(price_cents=999_999)], rectangular)
        assert "H7_OVER_BUDGET" not in codes(report)


class TestSoftRules:
    def test_s1_a_tight_walkway_is_a_warning_not_a_block(self, rectangular: RoomAnalysis) -> None:
        pair = [
            sofa_against_w1(),
            item(
                "table1",
                "coffee_table",
                x=-1200.0,
                z=-600.0,
                width=1100.0,
                depth=600.0,
                height=420.0,
            ),
        ]
        report = validate(pair, rectangular)
        assert report.fits, "a tight gap must not block a layout"
        assert "S1_WALKWAY_TIGHT" in codes(report)
        assert report.min_walkway_mm is not None
        assert report.min_walkway_mm < COMFORTABLE_WALKWAY_MM

    def test_s2_a_coffee_table_too_close_to_its_sofa(self, rectangular: RoomAnalysis) -> None:
        """5.3 puts the sofa-to-table gap at 350-500 mm. Closer and you
        cannot get past it."""
        pair = [
            sofa_against_w1(),
            item(
                "table1",
                "coffee_table",
                x=-1200.0,
                z=-520.0,
                width=1100.0,
                depth=600.0,
                height=420.0,
            ),
        ]
        report = validate(pair, rectangular)
        assert "S2_CATEGORY_CLEARANCE" in codes(report)
        violation = next(v for v in report.items["sofa1"] if v.code == "S2_CATEGORY_CLEARANCE")
        assert violation.measured_mm is not None
        assert violation.limit_mm == 450

    def test_s3_a_bookshelf_in_front_of_a_window(self, rectangular: RoomAnalysis) -> None:
        """The window on W1 has a 900 mm sill, so anything taller blocks it."""
        blocker = item(
            "shelf1", "bookshelf", x=0.0, z=-1600.0, width=900.0, depth=350.0, height=1800.0
        )
        report = validate([blocker], rectangular)
        assert report.fits, "blocking a window is a warning, not a blocker"
        assert "S3_WINDOW_BLOCKED" in codes(report)

    def test_s3_ignores_something_below_the_sill(self, rectangular: RoomAnalysis) -> None:
        """A sideboard under a high window is a good idea, not a warning."""
        low = item(
            "side1", "console_table", x=0.0, z=-1600.0, width=1200.0, depth=400.0, height=800.0
        )
        assert "S3_WINDOW_BLOCKED" not in codes(validate([low], rectangular))

    def test_s4_a_sofa_facing_the_wall(self, rectangular: RoomAnalysis) -> None:
        """Satisfies every hard rule and is obviously wrong to a person. This
        is the only rule that catches it."""
        backwards = sofa_against_w1(rotation=0.0)
        backwards = PlacedItem(
            id=backwards.id,
            category=backwards.category,
            footprint=backwards.footprint,
            against_wall_id="W1",
        )
        report = validate([backwards], rectangular)
        assert report.fits
        assert "S4_BACK_NOT_TO_WALL" in codes(report)

    def test_s4_passes_when_the_sofa_faces_the_room(self, rectangular: RoomAnalysis) -> None:
        correct = sofa_against_w1()
        correct = PlacedItem(
            id=correct.id,
            category=correct.category,
            footprint=correct.footprint,
            against_wall_id="W1",
        )
        assert "S4_BACK_NOT_TO_WALL" not in codes(validate([correct], rectangular))

    def test_s5_a_sofa_too_big_for_the_room(self, bedroom: RoomAnalysis) -> None:
        """The small bedroom is 3.0 x 2.6 m; a 2.9 m sofa dominates it."""
        huge = item(
            "sofa1",
            "sofa",
            x=0.0,
            z=-1000.0,
            width=2900.0,
            depth=900.0,
            height=800.0,
            rotation=180.0,
        )
        report = validate([huge], bedroom)
        assert "S5_OVERSIZED_FOR_ROOM" in codes(report)

    def test_s5_passes_for_a_proportionate_item(self, rectangular: RoomAnalysis) -> None:
        assert "S5_OVERSIZED_FOR_ROOM" not in codes(validate([sofa_against_w1()], rectangular))

    def test_s6_an_unavailable_product(self, rectangular: RoomAnalysis) -> None:
        report = validate([sofa_against_w1(available=False)], rectangular)
        assert report.fits
        assert "S6_PRICE_STALE_OR_UNAVAILABLE" in codes(report)
        assert "no longer available" in report.items["sofa1"][0].message

    def test_s6_a_stale_price(self, rectangular: RoomAnalysis) -> None:
        report = validate([sofa_against_w1(price_age_days=30)], rectangular)
        assert "S6_PRICE_STALE_OR_UNAVAILABLE" in codes(report)
        assert "30 days ago" in report.items["sofa1"][0].message

    def test_s6_silent_for_a_fresh_price(self, rectangular: RoomAnalysis) -> None:
        report = validate([sofa_against_w1(price_age_days=2)], rectangular)
        assert "S6_PRICE_STALE_OR_UNAVAILABLE" not in codes(report)


class TestReportShape:
    def test_a_clean_report_matches_the_schema(
        self, rectangular: RoomAnalysis, report_validator: jsonschema.protocols.Validator
    ) -> None:
        report_validator.validate(validate([sofa_against_w1()], rectangular).as_json())

    def test_a_report_full_of_violations_matches_the_schema(
        self, rectangular: RoomAnalysis, report_validator: jsonschema.protocols.Validator
    ) -> None:
        messy = [
            sofa_against_w1(z=-2200.0, price_cents=500_000, available=False),
            item(
                "table1",
                "coffee_table",
                x=-1200.0,
                z=-2100.0,
                width=1100.0,
                depth=600.0,
                height=420.0,
            ),
            item(
                "shelf1", "bookshelf", x=1100.0, z=1500.0, width=900.0, depth=350.0, height=2900.0
            ),
        ]
        report = validate(messy, rectangular, budget_cents=100_000)
        report_validator.validate(report.as_json())
        assert not report.fits

    def test_every_violation_carries_a_sentence(self, rectangular: RoomAnalysis) -> None:
        """5.5: the report is what the UI shows, and is "never an opaque AI
        opinion". A code with no sentence would reach a user as a code."""
        messy = [
            sofa_against_w1(z=-2200.0),
            item(
                "table1",
                "coffee_table",
                x=-1200.0,
                z=-2100.0,
                width=1100.0,
                depth=600.0,
                height=420.0,
            ),
        ]
        report = validate(messy, rectangular, budget_cents=1)
        everything = report.violations + [v for vs in report.items.values() for v in vs]
        assert everything
        for violation in everything:
            assert violation.message.strip()
            assert violation.message[0].isupper() or violation.message.startswith("Only")
            assert len(violation.message) <= 300

    def test_items_with_nothing_wrong_are_omitted(self, rectangular: RoomAnalysis) -> None:
        payload = validate([sofa_against_w1()], rectangular).as_json()
        assert payload["items"] == []

    def test_serialisation_is_stable(self, rectangular: RoomAnalysis) -> None:
        """The parity suite compares JSON, so ordering is part of the
        contract, not an implementation detail."""
        layout = [
            sofa_against_w1(z=-2200.0),
            item(
                "atable",
                "coffee_table",
                x=-1200.0,
                z=-2100.0,
                width=1100.0,
                depth=600.0,
                height=420.0,
            ),
        ]
        first = json.dumps(validate(layout, rectangular).as_json(), sort_keys=False)
        second = json.dumps(validate(layout, rectangular).as_json(), sort_keys=False)
        assert first == second

    def test_measured_values_are_reported_even_when_nothing_is_wrong(
        self, rectangular: RoomAnalysis
    ) -> None:
        """The schema's `measured` block exists so the UI can show numbers on
        a good layout, not only complaints on a bad one."""
        payload = validate([sofa_against_w1(price_cents=1234)], rectangular).as_json()
        assert payload["measured"]["total_price"]["amount_cents"] == 1234
        assert payload["measured"]["free_floor_pct"] > 0


class TestEmptyAndEdgeCases:
    def test_an_empty_layout_fits(self, rectangular: RoomAnalysis) -> None:
        report = validate([], rectangular)
        assert report.fits
        assert report.total_price_cents == 0

    def test_an_open_plan_room_still_validates(self, open_plan: RoomAnalysis) -> None:
        """No door at all. H6 has no seed to flood from, so it must fall back
        rather than crash or declare everything unreachable."""
        report = validate([], open_plan)
        assert report.fits

    def test_an_l_shaped_room_places_items_in_its_notch_correctly(
        self, l_shaped: RoomAnalysis
    ) -> None:
        """The notch is outside the floor polygon but inside its bounding box,
        so a containment test done on bounds rather than on the polygon would
        pass this."""
        min_x, min_z, max_x, max_z = l_shaped.floor.bounds
        in_the_notch = item(
            "shelf1",
            "bookshelf",
            x=max_x - 400.0,
            z=max_z - 400.0,
            width=700.0,
            depth=700.0,
            height=1800.0,
        )
        report = validate([in_the_notch], l_shaped)
        inside = l_shaped.floor.contains(in_the_notch.footprint.polygon())
        assert inside == ("H1_OUTSIDE_FLOOR" not in codes(report))
