"""Loading `category_rules.yaml` (implementation-plan.md 5.3).

A thin, typed layer over the YAML. The point of the split is that the rules
are data a person can review and change without reading Python, while the
code that consumes them gets something with named fields and defaults rather
than a dict of `Any`.

A category absent from the file still works: `CategoryRule()` supplies neutral
defaults and the validator simply has less to say about it. That matters
because 4.2's vocabulary can grow ahead of the rules, and a new category
should degrade to "no special rules" rather than crash a layout.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import yaml

RULES_PATH = Path(__file__).with_name("category_rules.yaml")


@dataclass(frozen=True, slots=True)
class CategoryRule:
    """One category's placement rules. Every field has a neutral default."""

    placement: str = "float_allowed"
    layer: str = "floor"
    back_to_wall_gap_mm: tuple[float, float] = (0.0, 150.0)
    front_clearance_mm: float = 0.0
    side_clearance_mm: float = 0.0
    faces: str = "none"

    relation: str | None = None
    anchor_categories: tuple[str, ...] = ()
    gap_mm: tuple[float, float] = (0.0, 0.0)
    center_aligned: bool = False
    size_ratio_to_anchor_width: tuple[float, float] | None = None
    pullback_mm: float = 0.0
    side_access_both_above_width_mm: float | None = None

    @property
    def prefers_wall(self) -> bool:
        return self.placement in ("against_wall_preferred", "headboard_to_wall")

    @property
    def is_floor_covering(self) -> bool:
        return self.layer == "floor_covering"

    def side_clearance_for(self, width_mm: float) -> tuple[float, float]:
        """Clearance at each side, as (near, far).

        5.3's bed rule in general form: a double bed needs getting-out-of-bed
        room on both sides, a single only on one. Returning a pair rather
        than a number is what lets the solver put a twin bed in a corner,
        which is where twin beds go.
        """
        threshold = self.side_access_both_above_width_mm
        if threshold is None or width_mm >= threshold:
            return (self.side_clearance_mm, self.side_clearance_mm)
        return (self.side_clearance_mm, 0.0)


def _pair(value: Any, fallback: tuple[float, float]) -> tuple[float, float]:
    if isinstance(value, list | tuple) and len(value) == 2:
        return (float(value[0]), float(value[1]))
    return fallback


def _rule_from(raw: dict[str, Any]) -> CategoryRule:
    ratio = raw.get("size_ratio_to_anchor_width")
    return CategoryRule(
        placement=str(raw.get("placement", "float_allowed")),
        layer=str(raw.get("layer", "floor")),
        back_to_wall_gap_mm=_pair(raw.get("back_to_wall_gap_mm"), (0.0, 150.0)),
        front_clearance_mm=float(raw.get("front_clearance_mm", 0.0)),
        side_clearance_mm=float(raw.get("side_clearance_mm", 0.0)),
        faces=str(raw.get("faces", "none")),
        relation=raw.get("relation"),
        anchor_categories=tuple(raw.get("anchor_categories", ())),
        gap_mm=_pair(raw.get("gap_mm"), (0.0, 0.0)),
        center_aligned=bool(raw.get("center_aligned", False)),
        size_ratio_to_anchor_width=_pair(ratio, (0.0, 1.0)) if ratio else None,
        pullback_mm=float(raw.get("pullback_mm", 0.0)),
        side_access_both_above_width_mm=(
            float(raw["side_access_both_above_width_mm"])
            if "side_access_both_above_width_mm" in raw
            else None
        ),
    )


@cache
def load_rules(path: Path = RULES_PATH) -> dict[str, CategoryRule]:
    """Parse the YAML once per process."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {name: _rule_from(body or {}) for name, body in raw.items()}


# Loaded once at import. The file is a couple of kilobytes and every caller
# needs it, so laziness would buy nothing and a stale cache would cost a
# confusing afternoon.
CATEGORY_RULES: dict[str, CategoryRule] = load_rules()

# 4.2's closed category vocabulary. Kept beside the rules because the two
# have to agree: a category the catalog can produce but the layout engine has
# never heard of would be placed with no rules at all.
CATEGORY_VOCABULARY: frozenset[str] = frozenset(
    {
        "sofa",
        "sectional",
        "loveseat",
        "armchair",
        "accent_chair",
        "coffee_table",
        "side_table",
        "console_table",
        "tv_stand",
        "bookshelf",
        "bed_frame",
        "nightstand",
        "dresser",
        "desk",
        "office_chair",
        "dining_table",
        "dining_chair",
        "rug",
        "floor_lamp",
        "bench",
        "ottoman",
        "cabinet",
    }
)


def anchors_first(categories: list[str]) -> list[str]:
    """Order categories so anchors are placed before what depends on them.

    5.4 L5 step 1. A coffee table's position is defined relative to a sofa,
    so solving it first would mean solving it against nothing. Floor
    coverings go last: a rug is positioned under whatever ended up above it.
    """

    def key(category: str) -> tuple[int, str]:
        rule = CATEGORY_RULES.get(category, CategoryRule())
        if rule.is_floor_covering:
            return (2, category)
        if rule.relation is not None:
            return (1, category)
        return (0, category)

    return sorted(categories, key=key)
