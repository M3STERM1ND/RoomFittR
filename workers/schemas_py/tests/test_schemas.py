"""Schema round-trip and fixture validation (Phase 0 "Tests").

Three things are checked, and they fail for different reasons:

1. Every fixture validates against the raw JSON Schema. Catches a fixture that
   drifted from the spec.
2. Every fixture round-trips JSON -> Pydantic -> JSON unchanged. Catches the
   generated models being lossy: a field the generator dropped or renamed would
   silently disappear here rather than in production.
3. Documents that should be rejected are rejected. A schema that accepts
   everything passes tests 1 and 2 perfectly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft7Validator
from pydantic import ValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT7
from roomfittr_schemas import RoomModel

REPO = Path(__file__).resolve().parents[3]
SCHEMA_DIR = REPO / "packages" / "schemas" / "src"
FIXTURE_DIR = REPO / "fixtures" / "rooms"

FIXTURES = sorted(FIXTURE_DIR.glob("*.json"))


def load_schema(name: str) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))
    return doc


def validator_for(name: str) -> Draft7Validator:
    """Resolve $refs against the schema directory so cross-file refs work.

    The schemas reference each other by bare filename, so both that and the
    canonical $id are registered.
    """
    resources = []
    for path in SCHEMA_DIR.glob("*.schema.json"):
        doc = json.loads(path.read_text(encoding="utf-8"))
        resource = Resource.from_contents(doc, default_specification=DRAFT7)
        resources.append((path.name, resource))
        resources.append((doc["$id"], resource))
    registry = Registry().with_resources(resources)
    return Draft7Validator(load_schema(name), registry=registry)


def test_fixtures_exist() -> None:
    """The plan asks for six. If one goes missing the layout engine loses a case."""
    names = [f.name for f in FIXTURES]
    assert len(FIXTURES) == 6, f"expected 6 room fixtures, found {names}"


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_fixture_matches_json_schema(path: Path) -> None:
    doc = json.loads(path.read_text(encoding="utf-8"))
    errors = sorted(validator_for("room-model.schema.json").iter_errors(doc), key=str)
    assert not errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors)


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_fixture_round_trips_through_pydantic(path: Path) -> None:
    """JSON -> model -> JSON must be byte-identical in content.

    exclude_none matters: optional fields absent from the source must stay
    absent, not come back as explicit nulls.
    """
    original = json.loads(path.read_text(encoding="utf-8"))
    model = RoomModel.model_validate(original)
    assert model.model_dump(mode="json", exclude_none=True) == original


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_fixture_geometry_is_self_consistent(path: Path) -> None:
    """Wall lengths and opening extents must agree with the polygon.

    The schema cannot express this, and a layout engine trusting a wrong
    length would place furniture through a wall.
    """
    doc = json.loads(path.read_text(encoding="utf-8"))
    poly = doc["floor_polygon"]
    walls = doc["walls"]
    assert len(walls) == len(poly), "one wall per polygon edge"

    for i, wall in enumerate(walls):
        x0, z0 = poly[i]
        x1, z1 = poly[(i + 1) % len(poly)]
        assert wall["start"] == [x0, z0], f"{wall['id']} start does not match edge {i}"
        assert wall["end"] == [x1, z1], f"{wall['id']} end does not match edge {i}"
        expected = round(((x1 - x0) ** 2 + (z1 - z0) ** 2) ** 0.5)
        assert wall["length_mm"] == expected, (
            f"{wall['id']} length {wall['length_mm']} != {expected}"
        )

    lengths = {w["id"]: w["length_mm"] for w in walls}
    for o in doc["openings"]:
        assert o["wall_id"] in lengths, f"{o['id']} references missing {o['wall_id']}"
        assert o["offset_mm"] + o["width_mm"] <= lengths[o["wall_id"]], (
            f"{o['id']} runs past the end of {o['wall_id']}"
        )


def _valid_doc() -> dict[str, Any]:
    text = (FIXTURE_DIR / "rectangular-living.json").read_text(encoding="utf-8")
    doc: dict[str, Any] = json.loads(text)
    return doc


def test_unknown_field_is_rejected() -> None:
    """additionalProperties: false has to actually bite, in both layers."""
    doc = _valid_doc()
    doc["surprise"] = 1
    assert list(validator_for("room-model.schema.json").iter_errors(doc))
    with pytest.raises(ValidationError):
        RoomModel.model_validate(doc)


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda d: d.update(units="m"), "units are millimetres everywhere (2.4)"),
        (lambda d: d.update(ceiling_height_mm=1200), "below the 2000mm sanity floor (3.6)"),
        (lambda d: d.update(ceiling_height_mm=9000), "above the 4500mm sanity ceiling (3.6)"),
        (lambda d: d.update(floor_polygon=[[0, 0], [1, 1]]), "a polygon needs three points"),
        (lambda d: d["scale"].update(confidence="probably"), "confidence is a closed set"),
        (lambda d: d["quality"]["warnings"].append("SOMETHING_ELSE"), "warning codes are closed"),
        (lambda d: d["walls"][0].update(kind="half"), "wall kind is solid or open"),
        (lambda d: d["openings"][0].update(type="hatch"), "opening type is closed"),
    ],
)
def test_invalid_documents_are_rejected(mutate: Any, reason: str) -> None:
    doc = _valid_doc()
    mutate(doc)
    errors = list(validator_for("room-model.schema.json").iter_errors(doc))
    assert errors, f"should reject: {reason}"
