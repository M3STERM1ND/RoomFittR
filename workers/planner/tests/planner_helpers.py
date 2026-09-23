"""Helpers for the planner tests, including the transport that stands in for Claude.

Separate from `conftest.py` for the same reason as the layout package's
`layout_helpers.py`: two packages both named `tests` collide in mypy, so the
test directories have no `__init__.py` and a distinctly named module is the
one that can be imported.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from roomfittr_layout.planner import Product
from roomfittr_planner.client import ToolResult, TransportError
from roomfittr_planner.cost import Usage

FIXTURE_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "rooms"


def load_room(name: str) -> dict[str, Any]:
    room: dict[str, Any] = json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))
    return room


@dataclass
class ScriptedTransport:
    """A Claude that says exactly what the test tells it to.

    9.1 requires chaos tests in which "the LLM returns invalid JSON /
    nonexistent IDs / absurd budget shares" and the engine still returns a
    valid layout. Those tests have to *be* the model, so the script is a
    list of one response per call: a dict becomes tool arguments, a
    `TransportError` is raised, and anything else stands in for a model that
    answered in prose.

    Records every call so a test can assert the prompt carried what 5.4 says
    it must -- the room summary, the budget, the catalog percentiles.
    """

    script: list[Any] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    usage: Usage = Usage(model="claude-sonnet-5", input_tokens=1200, output_tokens=300)

    def call_tool(
        self,
        *,
        model: str,
        system: str,
        user: str,
        tool: dict[str, Any],
        max_tokens: int,
        timeout_s: float,
        think: bool,
    ) -> ToolResult:
        self.calls.append(
            {
                "model": model,
                "system": system,
                "user": user,
                "tool": tool,
                "max_tokens": max_tokens,
                "timeout_s": timeout_s,
                "think": think,
            }
        )
        if not self.script:
            raise TransportError("script_exhausted")
        response = self.script.pop(0)
        if isinstance(response, TransportError):
            raise response
        if isinstance(response, dict):
            return ToolResult(
                arguments=response,
                usage=Usage(
                    model=model,
                    input_tokens=self.usage.input_tokens,
                    output_tokens=self.usage.output_tokens,
                ),
            )
        raise TransportError("no_tool_call", repr(response))


def catalog() -> list[Product]:
    """A stand-in for `layout_candidates` (4.11), matching the layout tests'.

    Deliberately the same shape and the same price points as
    `workers/layout/tests/test_planner.py`: a layout that differs between the
    two suites should differ because of the model, not because the catalog
    did.
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


def plan_arguments(
    slots: list[dict[str, Any]],
    *,
    room_type: str = "living",
    style: str | None = "modern",
    rationale: str = "A sofa on the long wall with the seating around it.",
) -> dict[str, Any]:
    """Well-formed tool arguments, as `strict: true` would deliver them."""
    return {
        "room_type": room_type,
        "style": style,
        "rationale": rationale,
        "slots": slots,
    }


def slot(
    slot_id: str,
    category: str,
    *,
    priority: str = "should",
    budget_share: float = 0.2,
    relation: str = "float",
    anchor: dict[str, str] | None = None,
    facing: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    return {
        "slot_id": slot_id,
        "category": category,
        "priority": priority,
        "budget_share": budget_share,
        "intent": {
            "relation": relation,
            "anchor": anchor,
            "facing": facing,
            "notes": notes,
        },
    }
