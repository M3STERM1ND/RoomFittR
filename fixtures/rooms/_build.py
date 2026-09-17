"""Builds the six golden RoomModel fixtures.

These unblock Track Product (catalog, layout engine, viewer) before the CV
pipeline produces anything real, so they have to be correct in the ways the
layout engine cares about: closed simple polygons, walls that match the polygon
edges, openings that lie inside their wall, and the coordinate conventions from
implementation-plan.md 2.4.

Derived rather than hand-typed on purpose. Wall lengths, the centroid shift and
the 2.4 "+X along the longest wall" rule are all things that are easy to get
subtly wrong by hand and hard to spot by eye, and a fixture that is quietly
wrong teaches the code the wrong lesson.

Dimensions are realistic but synthetic. The laser-measured ground truth lives in
eval/ground_truth/ and is a different dataset with a different job: these six
exist to exercise shape variety, not to measure accuracy.

Run:  uv run python fixtures/rooms/_build.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

OUT = Path(__file__).parent
SCHEMA_VERSION = 0
PIPELINE_VERSION = "0.0.0"


def polygon_area_centroid(poly: list[tuple[int, int]]) -> tuple[float, float, float]:
    """Signed area and centroid of a closed polygon given as [x, z] vertices."""
    a2 = 0.0
    cx = 0.0
    cz = 0.0
    n = len(poly)
    for i in range(n):
        x0, z0 = poly[i]
        x1, z1 = poly[(i + 1) % n]
        cross = x0 * z1 - x1 * z0
        a2 += cross
        cx += (x0 + x1) * cross
        cz += (z0 + z1) * cross
    area = a2 / 2.0
    if abs(area) < 1e-9:
        raise ValueError("degenerate polygon")
    return area, cx / (3.0 * a2), cz / (3.0 * a2)


def normalise(poly: list[tuple[int, int]]) -> list[list[int]]:
    """Make CCW and move the centroid to the origin, per 2.4."""
    area, cx, cz = polygon_area_centroid(poly)
    if area < 0:  # clockwise
        poly = list(reversed(poly))
        _, cx, cz = polygon_area_centroid(poly)
    return [[round(x - cx), round(z - cz)] for x, z in poly]


def walls_from_polygon(poly: list[list[int]], open_edges: set[int] | None = None) -> list[dict]:
    """One wall per polygon edge. Edge i runs from vertex i to vertex i+1."""
    open_edges = open_edges or set()
    walls = []
    n = len(poly)
    for i in range(n):
        x0, z0 = poly[i]
        x1, z1 = poly[(i + 1) % n]
        length = round(((x1 - x0) ** 2 + (z1 - z0) ** 2) ** 0.5)
        walls.append(
            {
                "id": f"W{i + 1}",
                "start": [x0, z0],
                "end": [x1, z1],
                "thickness_mm": 100,
                "kind": "open" if i in open_edges else "solid",
                "length_mm": length,
            }
        )
    return walls


def check_openings(walls: list[dict], openings: list[dict]) -> None:
    """Every opening must fit inside the wall it claims to be on (3.6 sanity)."""
    by_id = {w["id"]: w for w in walls}
    for o in openings:
        wall = by_id.get(o["wall_id"])
        if wall is None:
            raise ValueError(f"{o['id']} references missing wall {o['wall_id']}")
        if o["offset_mm"] + o["width_mm"] > wall["length_mm"]:
            raise ValueError(
                f"{o['id']} runs past the end of {wall['id']}: "
                f"{o['offset_mm']} + {o['width_mm']} > {wall['length_mm']}"
            )


def door(oid: str, wall: str, offset: int, width: int = 830) -> dict:
    return {
        "id": oid,
        "wall_id": wall,
        "type": "door",
        "offset_mm": offset,
        "width_mm": width,
        "sill_mm": 0,
        "height_mm": 2040,
        "swing": "unknown",
        "confidence": 0.9,
    }


def window(oid: str, wall: str, offset: int, width: int, sill: int = 900) -> dict:
    return {
        "id": oid,
        "wall_id": wall,
        "type": "window",
        "offset_mm": offset,
        "width_mm": width,
        "sill_mm": sill,
        "height_mm": 1200,
        "swing": "unknown",
        "confidence": 0.85,
    }


def room(
    name: str,
    poly: list[tuple[int, int]],
    ceiling: int,
    openings: list[dict],
    *,
    open_edges: set[int] | None = None,
    obstacles: list[dict] | None = None,
    warnings: list[str] | None = None,
    confidence: str = "high",
    user_calibrated: bool = True,
    ceiling_observed: bool = True,
    coverage: float = 96.0,
) -> tuple[str, dict[str, Any]]:
    floor = normalise(poly)
    walls = walls_from_polygon(floor, open_edges)
    check_openings(walls, openings)

    area_m2 = abs(polygon_area_centroid([(x, z) for x, z in floor])[0]) / 1e6
    if not 3.0 <= area_m2 <= 150.0:
        raise ValueError(f"{name}: area {area_m2:.1f} m2 outside the 3-150 sanity range")

    return name, {
        "schema_version": SCHEMA_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        "units": "mm",
        "up": "+Y",
        "floor_polygon": floor,
        "ceiling_height_mm": ceiling,
        "ceiling_observed": ceiling_observed,
        "walls": walls,
        "openings": openings,
        "fixed_obstacles": obstacles or [],
        "appearance": {
            "floor": {"color": "#efecea", "material": "oak"},
            "ceiling": {"color": "#f6f9fc", "material": "paint"},
            "tier": 0,
        },
        "scale": {
            "factor": 1.0,
            "sources": [{"name": "fixture", "factor": 1.0, "weight": 1.0}],
            "confidence": confidence,
            "user_calibrated": user_calibrated,
        },
        "quality": {
            "coverage_pct": coverage,
            "frames_used": 112,
            "warnings": warnings or [],
        },
    }


def radiator(oid: str, cx: int, cz: int, yaw: float = 0.0) -> dict:
    return {
        "id": oid,
        "label": "radiator",
        "center": [cx, cz],
        "size": [1200, 120, 600],
        "yaw_deg": yaw,
        "source": "detected",
    }


def build() -> list[tuple[str, dict]]:
    return [
        # 1. The baseline. Everything else is a deviation from this.
        room(
            "rectangular-living",
            [(0, 0), (4600, 0), (4600, 3800), (0, 3800)],
            2700,
            [door("O1", "W3", 1200), window("O2", "W1", 1600, 1800)],
        ),
        # 2. L-shaped: the solver cannot assume a convex room or one focal wall.
        room(
            "l-shaped-living",
            [(0, 0), (5400, 0), (5400, 2400), (3200, 2400), (3200, 4800), (0, 4800)],
            2650,
            [door("O1", "W6", 1800), window("O2", "W1", 2000, 1600), window("O3", "W4", 600, 1200)],
        ),
        # 3. Small bedroom: clearances bind here, and most layouts will not fit.
        room(
            "small-bedroom",
            [(0, 0), (3000, 0), (3000, 2600), (0, 2600)],
            2450,
            [door("O1", "W4", 400, 760), window("O2", "W2", 800, 1000)],
        ),
        # 4. Open boundary: W2 is an open-plan edge, not a wall. Nothing may be
        #    placed against it, and it still bounds the room (3.6 step 5).
        room(
            "open-plan-boundary",
            [(0, 0), (4200, 0), (4200, 4000), (0, 4000)],
            2800,
            [window("O1", "W1", 1400, 2000)],
            open_edges={1},
            warnings=["OPEN_BOUNDARY_PRESENT"],
        ),
        # 5. Many openings: very little usable wall run left. Tests the
        #    free-run computation and the door keep-out stacking (5.2).
        room(
            "many-openings",
            [(0, 0), (5000, 0), (5000, 3400), (0, 3400)],
            2700,
            [
                window("O1", "W1", 500, 1200),
                window("O2", "W1", 2200, 1200),
                window("O3", "W1", 3600, 1000),
                door("O4", "W2", 400),
                door("O5", "W3", 1000),
                door("O6", "W4", 1400, 760),
            ],
            obstacles=[radiator("F1", 0, 0)],
        ),
        # 6. Narrow: width below the comfortable walkway once anything is
        #    against a wall. Exercises H6 and S1 hardest.
        room(
            "narrow-room",
            [(0, 0), (5200, 0), (5200, 1900), (0, 1900)],
            2400,
            [door("O1", "W4", 300, 760), window("O2", "W2", 500, 900)],
            ceiling_observed=False,
            confidence="low",
            user_calibrated=False,
            coverage=71.0,
            warnings=["CEILING_NOT_OBSERVED", "SCALE_UNCALIBRATED", "LOW_COVERAGE"],
        ),
    ]


if __name__ == "__main__":
    for name, model in build():
        path = OUT / f"{name}.json"
        path.write_text(json.dumps(model, indent=2) + "\n", encoding="utf-8")
        area = abs(polygon_area_centroid([(x, z) for x, z in model["floor_polygon"]])[0]) / 1e6
        walls = len(model["walls"])
        openings = len(model["openings"])
        print(f"{name:24s} {area:6.2f} m2  {walls} walls  {openings} openings")
