"""Validates every ground-truth file. CI entry point.

    uv run python -m eval.validate_ground_truth

Ground truth is measured once, by hand, in a room you may not have access to
again. A typo found months later during Phase 1 is expensive, so this checks
the files the day they are written: schema first, then the things a schema
cannot express (openings that run off the end of their wall, a polygon whose
perimeter disagrees with the measured wall lengths, duplicate ids).
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft7Validator

EVAL_DIR = Path(__file__).resolve().parent
SCHEMA = EVAL_DIR / "schema" / "ground-truth.schema.json"
GROUND_TRUTH_DIR = EVAL_DIR / "ground_truth"

# A hand-drawn plan reconciled with laser measurements will not close perfectly.
# 2% of perimeter is generous enough for honest measurement slop and tight
# enough to catch a transposed digit.
PERIMETER_TOLERANCE_PCT = 2.0


def check_document(doc: dict[str, Any], validator: Draft7Validator) -> list[str]:
    problems = [
        f"{list(e.path) or '<root>'}: {e.message}"
        for e in sorted(validator.iter_errors(doc), key=str)
    ]
    if problems:
        # Cross-field checks below assume the shape is already right.
        return problems

    wall_ids = [w["id"] for w in doc["walls"]]
    if len(wall_ids) != len(set(wall_ids)):
        problems.append("duplicate wall ids")
    lengths = {w["id"]: w["length_mm"] for w in doc["walls"]}

    opening_ids = [o["id"] for o in doc["openings"]]
    if len(opening_ids) != len(set(opening_ids)):
        problems.append("duplicate opening ids")

    for o in doc["openings"]:
        if o["wall_id"] not in lengths:
            problems.append(f"{o['id']} is on unknown wall {o['wall_id']}")
            continue
        if o["offset_mm"] + o["width_mm"] > lengths[o["wall_id"]]:
            problems.append(
                f"{o['id']} runs past the end of {o['wall_id']} "
                f"({o['offset_mm']} + {o['width_mm']} > {lengths[o['wall_id']]})"
            )
        if o["type"] == "door" and o["sill_mm"] > 100:
            problems.append(f"{o['id']} is a door with a {o['sill_mm']} mm sill")

    capture_ids = [c["capture_id"] for c in doc["captures"]]
    if len(capture_ids) != len(set(capture_ids)):
        problems.append("duplicate capture ids")
    for cid in capture_ids:
        if not cid.startswith(doc["room_id"]):
            problems.append(f"capture {cid} does not belong to {doc['room_id']}")
    # E8 needs a sloppy capture per room, and it is the experiment most likely to
    # be skipped when someone is tired of filming -- so it is enforced. But only
    # for rooms we filmed ourselves: a Tier A scene comes with whatever capture
    # the dataset shot, and 3.10 decides E8 on Tier B for exactly that reason.
    # Demanding one here would make every public scene unusable.
    tier = doc.get("source", {}).get("tier", "B")
    if tier == "B" and not any(c["style"] == "sloppy" for c in doc["captures"]):
        problems.append("no sloppy capture: E8 (capture robustness) cannot be scored")

    poly = doc.get("floor_polygon_mm")
    if poly:
        perimeter = 0.0
        for i, point in enumerate(poly):
            nxt = poly[(i + 1) % len(poly)]
            perimeter += math.dist(point, nxt)
        measured = sum(lengths.values())
        if measured > 0:
            drift = abs(perimeter - measured) / measured * 100.0
            if drift > PERIMETER_TOLERANCE_PCT:
                problems.append(
                    f"floor_polygon_mm perimeter {perimeter:.0f} mm disagrees with the "
                    f"measured walls {measured} mm by {drift:.1f}%"
                )

    return problems


def main() -> int:
    if not SCHEMA.exists():
        print(f"missing schema: {SCHEMA}")
        return 1
    validator = Draft7Validator(json.loads(SCHEMA.read_text(encoding="utf-8")))

    files = sorted(GROUND_TRUTH_DIR.glob("*.yaml"))
    if not files:
        # Not an error yet: Phase 0 builds the harness before the dataset
        # exists. It becomes one once capture starts.
        print("no ground-truth files yet (eval/ground_truth/*.yaml)")
        return 0

    failed = 0
    seen: set[str] = set()
    for path in files:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        problems = check_document(doc, validator)
        if doc.get("room_id") in seen:
            problems.append(f"duplicate room_id {doc['room_id']}")
        seen.add(doc.get("room_id", path.stem))

        if problems:
            failed += 1
            print(f"FAIL {path.name}")
            for p in problems:
                print(f"       {p}")
        else:
            captures = len(doc["captures"])
            print(f"ok   {path.name}  {len(doc['walls'])} walls, {captures} captures")

    print(f"\n{len(files) - failed}/{len(files)} ground-truth files valid")
    if len(files) < 12:
        print(f"note: 3.10 asks for 12-15 rooms; {len(files)} present so far")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
