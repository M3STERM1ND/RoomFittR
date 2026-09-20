"""Generating and loading the validator parity fixtures (5.5, 9.1).

9.1 lists "Validator parity (Python <-> TS)" as a gate on **every CI run**,
because "the AI and the editor must agree on what 'fits' means". A user
dragging a sofa is told by the browser whether it fits; the server decides
again when the edit is saved. If the two disagree, the editor either blocks
something legal or accepts something the save will reject.

A fixture is a room, a set of placed items, an optional budget, and the
report the validator is expected to produce. The expected report is generated
from the Python validator, so:

- the **Python** test asserts the implementation still produces what it did
  when the fixture was written -- a regression test;
- the **TypeScript** test asserts it produces the same thing -- the parity
  test.

Regenerating the fixtures after changing a rule is correct and expected. Doing
so to make a failing parity test pass is not: the fixture would then record
whatever Python does, and the TypeScript side would still disagree. The
failure to read is always "which of the two changed, and which one is right".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .geometry import Footprint
from .room import analyse
from .validator import PlacedItem, validate

FIXTURE_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "validation"
ROOM_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "rooms"


@dataclass(frozen=True, slots=True)
class ParityCase:
    """One fixture: what to validate, and what the answer must be."""

    name: str
    room: str
    items: list[dict[str, Any]]
    budget_cents: int | None
    enforce_budget: bool
    expected: dict[str, Any]

    def placed_items(self) -> list[PlacedItem]:
        return [_item_from(raw) for raw in self.items]


def _item_from(raw: dict[str, Any]) -> PlacedItem:
    return PlacedItem(
        id=raw["id"],
        category=raw["category"],
        footprint=Footprint(
            center_x_mm=float(raw["center_x_mm"]),
            center_z_mm=float(raw["center_z_mm"]),
            width_mm=float(raw["width_mm"]),
            depth_mm=float(raw["depth_mm"]),
            height_mm=float(raw["height_mm"]),
            rotation_deg=float(raw.get("rotation_deg", 0.0)),
            elevation_mm=float(raw.get("elevation_mm", 0.0)),
        ),
        price_cents=int(raw.get("price_cents", 0)),
        against_wall_id=raw.get("against_wall_id"),
        price_age_days=int(raw.get("price_age_days", 0)),
        available=bool(raw.get("available", True)),
    )


def load_room(name: str) -> dict[str, Any]:
    room: dict[str, Any] = json.loads((ROOM_DIR / f"{name}.json").read_text(encoding="utf-8"))
    return room


def run_case(case: ParityCase) -> dict[str, Any]:
    """Validate a case and return the report as JSON."""
    analysis = analyse(load_room(case.room))
    report = validate(
        case.placed_items(),
        analysis,
        budget_cents=case.budget_cents,
        enforce_budget=case.enforce_budget,
    )
    return report.as_json()


def load_cases() -> list[ParityCase]:
    cases: list[ParityCase] = []
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        cases.append(
            ParityCase(
                name=path.stem,
                room=raw["room"],
                items=raw["items"],
                budget_cents=raw.get("budget_cents"),
                enforce_budget=raw.get("enforce_budget", True),
                expected=raw["expected"],
            )
        )
    return cases


def item(
    item_id: str,
    category: str,
    *,
    x: float,
    z: float,
    width: float,
    depth: float,
    height: float,
    rotation: float = 0.0,
    elevation: float = 0.0,
    price_cents: int = 0,
    against_wall_id: str | None = None,
    price_age_days: int = 0,
    available: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": item_id,
        "category": category,
        "center_x_mm": x,
        "center_z_mm": z,
        "width_mm": width,
        "depth_mm": depth,
        "height_mm": height,
        "rotation_deg": rotation,
        "elevation_mm": elevation,
        "price_cents": price_cents,
        "price_age_days": price_age_days,
        "available": available,
    }
    if against_wall_id is not None:
        payload["against_wall_id"] = against_wall_id
    return payload


def _cases_to_generate() -> list[dict[str, Any]]:
    """The scenarios the suite covers.

    Chosen to exercise every rule at least once and, more importantly, the
    places where two implementations of the same rule are most likely to
    diverge: rotation handedness, rounding at a half-millimetre, the
    flood-fill that answers H6, and the integer formatting in the messages.
    """

    def sofa(
        item_id: str = "sofa1",
        *,
        x: float,
        z: float,
        price_cents: int = 0,
        price_age_days: int = 0,
        rotation: float = 180.0,
        against_wall_id: str | None = None,
    ) -> dict[str, Any]:
        """A 2 m sofa, the stand-in anchor in most of these cases.

        Rotation 180 puts its back to the window wall of `rectangular-living`
        and its front to the room, which is what the solver would produce.
        """
        return item(
            item_id,
            "sofa",
            x=x,
            z=z,
            width=2000.0,
            depth=900.0,
            height=800.0,
            rotation=rotation,
            price_cents=price_cents,
            price_age_days=price_age_days,
            against_wall_id=against_wall_id,
        )

    return [
        {
            "name": "01-empty-room",
            "room": "rectangular-living",
            "items": [],
            "budget_cents": None,
        },
        {
            "name": "02-clean-living-room",
            "room": "rectangular-living",
            "items": [
                sofa(x=-1200.0, z=-1450.0, price_cents=70_000),
                item(
                    "table1",
                    "coffee_table",
                    x=-1200.0,
                    z=-450.0,
                    width=1100.0,
                    depth=600.0,
                    height=420.0,
                    price_cents=18_000,
                ),
            ],
            "budget_cents": 200_000,
        },
        {
            "name": "03-outside-the-room",
            "room": "rectangular-living",
            "items": [sofa(x=-1200.0, z=-2200.0)],
            "budget_cents": None,
        },
        {
            "name": "04-overlapping-items",
            "room": "rectangular-living",
            "items": [
                sofa(x=-1200.0, z=-1450.0),
                item(
                    "table1",
                    "coffee_table",
                    x=-1200.0,
                    z=-1400.0,
                    width=1100.0,
                    depth=600.0,
                    height=420.0,
                ),
            ],
            "budget_cents": None,
        },
        {
            "name": "05-rug-under-sofa",
            "room": "rectangular-living",
            "items": [
                sofa(x=-1200.0, z=-1450.0),
                item(
                    "rug1",
                    "rug",
                    x=-1100.0,
                    z=-1000.0,
                    width=2200.0,
                    depth=1600.0,
                    height=10.0,
                ),
            ],
            "budget_cents": None,
        },
        {
            "name": "06-blocking-the-door",
            "room": "rectangular-living",
            "items": [
                item(
                    "shelf1",
                    "bookshelf",
                    x=1100.0,
                    z=1500.0,
                    width=900.0,
                    depth=350.0,
                    height=1800.0,
                )
            ],
            "budget_cents": None,
        },
        {
            "name": "07-too-tall",
            "room": "rectangular-living",
            "items": [
                item(
                    "shelf1",
                    "bookshelf",
                    x=-1400.0,
                    z=-1700.0,
                    width=900.0,
                    depth=300.0,
                    height=2700.0,
                )
            ],
            "budget_cents": None,
        },
        {
            "name": "08-blocking-a-window",
            "room": "rectangular-living",
            "items": [
                item(
                    "shelf1",
                    "bookshelf",
                    x=0.0,
                    z=-1600.0,
                    width=900.0,
                    depth=350.0,
                    height=1800.0,
                )
            ],
            "budget_cents": None,
        },
        {
            "name": "09-sofa-facing-the-wall",
            "room": "rectangular-living",
            "items": [
                item(
                    "sofa1",
                    "sofa",
                    x=-1200.0,
                    z=-1450.0,
                    width=2000.0,
                    depth=900.0,
                    height=800.0,
                    rotation=0.0,
                    against_wall_id="W1",
                )
            ],
            "budget_cents": None,
        },
        {
            "name": "10-over-budget",
            "room": "rectangular-living",
            "items": [sofa(x=-1200.0, z=-1450.0, price_cents=150_000)],
            "budget_cents": 100_000,
        },
        {
            "name": "11-over-budget-manual-edit",
            "room": "rectangular-living",
            "items": [sofa(x=-1200.0, z=-1450.0, price_cents=150_000)],
            "budget_cents": 100_000,
            "enforce_budget": False,
        },
        {
            "name": "12-stale-and-unavailable",
            "room": "rectangular-living",
            "items": [
                sofa(x=-1200.0, z=-1450.0, price_age_days=30),
                item(
                    "table1",
                    "coffee_table",
                    x=-1200.0,
                    z=-450.0,
                    width=1100.0,
                    depth=600.0,
                    height=420.0,
                    available=False,
                ),
            ],
            "budget_cents": None,
        },
        {
            "name": "13-circulation-blocked",
            "room": "narrow-room",
            "items": [
                item(
                    "shelf1",
                    "bookshelf",
                    x=-2000.0,
                    z=0.0,
                    width=350.0,
                    depth=1900.0,
                    height=1800.0,
                ),
                item(
                    "shelf2",
                    "bookshelf",
                    x=-1600.0,
                    z=0.0,
                    width=350.0,
                    depth=1900.0,
                    height=1800.0,
                ),
            ],
            "budget_cents": None,
        },
        {
            "name": "14-l-shaped-room",
            "room": "l-shaped-living",
            "items": [
                item(
                    "sofa1",
                    "sofa",
                    x=0.0,
                    z=-1400.0,
                    width=2000.0,
                    depth=900.0,
                    height=800.0,
                    rotation=180.0,
                ),
                item(
                    "table1",
                    "coffee_table",
                    x=0.0,
                    z=-400.0,
                    width=1100.0,
                    depth=600.0,
                    height=420.0,
                ),
            ],
            "budget_cents": None,
        },
        {
            "name": "15-rotated-items",
            "room": "rectangular-living",
            "items": [
                item(
                    "sofa1",
                    "sofa",
                    x=1700.0,
                    z=0.0,
                    width=2000.0,
                    depth=900.0,
                    height=800.0,
                    rotation=270.0,
                    against_wall_id="W2",
                ),
                item(
                    "shelf1",
                    "bookshelf",
                    x=-2050.0,
                    z=0.0,
                    width=1400.0,
                    depth=350.0,
                    height=1800.0,
                    rotation=90.0,
                    against_wall_id="W4",
                ),
            ],
            "budget_cents": None,
        },
        {
            "name": "16-elevated-shelf-over-sideboard",
            "room": "rectangular-living",
            "items": [
                item(
                    "side1",
                    "console_table",
                    x=0.0,
                    z=-1600.0,
                    width=1200.0,
                    depth=400.0,
                    height=800.0,
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
            ],
            "budget_cents": None,
        },
        {
            "name": "17-oversized-for-the-room",
            "room": "small-bedroom",
            "items": [
                item(
                    "sofa1",
                    "sofa",
                    x=0.0,
                    z=-1000.0,
                    width=2900.0,
                    depth=900.0,
                    height=800.0,
                    rotation=180.0,
                )
            ],
            "budget_cents": None,
        },
        {
            "name": "18-many-openings",
            "room": "many-openings",
            "items": [
                item(
                    "sofa1",
                    "sofa",
                    x=0.0,
                    z=-1200.0,
                    width=1800.0,
                    depth=900.0,
                    height=800.0,
                    rotation=180.0,
                )
            ],
            "budget_cents": None,
        },
        {
            "name": "19-open-plan-no-door",
            "room": "open-plan-boundary",
            "items": [
                item(
                    "sofa1",
                    "sofa",
                    x=0.0,
                    z=-1500.0,
                    width=2000.0,
                    depth=900.0,
                    height=800.0,
                    rotation=180.0,
                )
            ],
            "budget_cents": None,
        },
        {
            "name": "20-half-millimetre-overlap",
            "room": "rectangular-living",
            "items": [
                # Overlapping by exactly 12.5 mm: past the 5 mm tolerance so
                # it is a real violation, and on a half-millimetre so the
                # reported integer differs between Python's round-half-to-even
                # and JavaScript's round-half-up unless both use the same rule.
                item(
                    "a",
                    "bookshelf",
                    x=0.0,
                    z=0.0,
                    width=1000.0,
                    depth=600.0,
                    height=1800.0,
                ),
                item(
                    "b",
                    "bookshelf",
                    x=987.5,
                    z=0.0,
                    width=1000.0,
                    depth=600.0,
                    height=1800.0,
                ),
            ],
            "budget_cents": None,
        },
    ]


def regenerate() -> int:
    """Write every fixture from the current Python validator."""
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    for spec in _cases_to_generate():
        case = ParityCase(
            name=spec["name"],
            room=spec["room"],
            items=spec["items"],
            budget_cents=spec.get("budget_cents"),
            enforce_budget=spec.get("enforce_budget", True),
            expected={},
        )
        payload = {
            "room": case.room,
            "items": case.items,
            "budget_cents": case.budget_cents,
            "enforce_budget": case.enforce_budget,
            "expected": run_case(case),
        }
        path = FIXTURE_DIR / f"{spec['name']}.json"
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        written += 1
    return written


if __name__ == "__main__":
    count = regenerate()
    print(f"wrote {count} parity fixtures to {FIXTURE_DIR}")
