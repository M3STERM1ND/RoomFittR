"""Helpers shared by the layout tests.

Separate from `conftest.py` because the test directories deliberately have no
`__init__.py`: two packages both named `tests` collide in mypy, and pytest
only needs a conftest for fixtures. A distinctly named module can then be
imported directly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from roomfittr_layout.geometry import Footprint
from roomfittr_layout.validator import PlacedItem

FIXTURE_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "rooms"


def load_room(name: str) -> dict[str, Any]:
    """One of Phase 0's hand-authored RoomModels."""
    room: dict[str, Any] = json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))
    return room


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
) -> PlacedItem:
    return PlacedItem(
        id=item_id,
        category=category,
        footprint=Footprint(
            center_x_mm=x,
            center_z_mm=z,
            width_mm=width,
            depth_mm=depth,
            height_mm=height,
            rotation_deg=rotation,
            elevation_mm=elevation,
        ),
        price_cents=price_cents,
        against_wall_id=against_wall_id,
        price_age_days=price_age_days,
        available=available,
    )


def codes(report: Any) -> set[str]:
    """Every violation code in a report, global and per-item."""
    found = {v.code for v in report.violations}
    for violations in report.items.values():
        found |= {v.code for v in violations}
    return found
