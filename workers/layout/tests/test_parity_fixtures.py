"""The Python half of the validator parity suite (5.5, 9.1).

9.1 gates "Validator parity (Python <-> TS)" on every CI run. This file is
the Python side: it asserts the validator still produces exactly what the
committed fixtures record. `packages/geometry/test/parity.test.ts` is the
other side, asserting the TypeScript validator produces the same thing.

The split matters. Together they say "these two implementations agree";
separately, this one is a regression test over the rules themselves, which is
useful on its own -- a message reworded without thinking fails here first.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT7
from roomfittr_layout.parity import ParityCase, load_cases, run_case

SCHEMA_DIR = Path(__file__).resolve().parents[3] / "packages" / "schemas" / "src"
CASES = load_cases()


def _schema_registry() -> Registry:
    resources = []
    for path in SCHEMA_DIR.glob("*.schema.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        resource = Resource.from_contents(document, default_specification=DRAFT7)
        resources.append((path.name, resource))
        resources.append((document["$id"], resource))
    return Registry().with_resources(resources)


@pytest.fixture(scope="module")
def report_validator() -> jsonschema.protocols.Validator:
    schema = json.loads((SCHEMA_DIR / "validation-report.schema.json").read_text(encoding="utf-8"))
    return jsonschema.Draft7Validator(schema, registry=_schema_registry())


def test_the_suite_is_not_empty() -> None:
    """A parity suite that silently collects nothing passes forever."""
    assert len(CASES) >= 20, f"only {len(CASES)} parity fixtures found"


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_the_validator_still_produces_the_recorded_report(case: ParityCase) -> None:
    assert run_case(case) == case.expected


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_every_expected_report_matches_the_schema(
    case: ParityCase, report_validator: jsonschema.protocols.Validator
) -> None:
    report_validator.validate(case.expected)


def test_the_suite_covers_every_rule() -> None:
    """A parity suite is only as good as its coverage: a rule no fixture
    exercises is a rule the two implementations are free to disagree about.
    """
    seen: set[str] = set()
    for case in CASES:
        seen |= {v["code"] for v in case.expected["violations"]}
        for entry in case.expected["items"]:
            seen |= {v["code"] for v in entry["violations"]}

    expected = {
        "H1_OUTSIDE_FLOOR",
        "H2_ITEM_OVERLAP",
        "H4_DOOR_KEEPOUT",
        "H5_TOO_TALL",
        "H6_CIRCULATION_BLOCKED",
        "H7_OVER_BUDGET",
        "S1_WALKWAY_TIGHT",
        "S2_CATEGORY_CLEARANCE",
        "S3_WINDOW_BLOCKED",
        "S4_BACK_NOT_TO_WALL",
        "S5_OVERSIZED_FOR_ROOM",
        "S6_PRICE_STALE_OR_UNAVAILABLE",
    }
    missing = expected - seen
    assert not missing, f"no parity fixture exercises {sorted(missing)}"


def test_a_clean_layout_and_a_broken_one_are_both_covered() -> None:
    """Fixtures that all fail would let a validator that rejects everything
    pass the suite; fixtures that all pass would let one that accepts
    everything pass it."""
    verdicts = {case.expected["fits"] for case in CASES}
    assert verdicts == {True, False}


def test_the_half_millimetre_case_rounds_half_to_even() -> None:
    """Python rounds 12.5 to 12; JavaScript's `Math.round` gives 13. The
    fixture pins the Python answer so the TypeScript side has to implement
    the same rule rather than the language's default."""
    case = next(c for c in CASES if c.name.endswith("half-millimetre-overlap"))
    violation = case.expected["items"][0]["violations"][0]
    assert violation["measured_mm"] == 12
