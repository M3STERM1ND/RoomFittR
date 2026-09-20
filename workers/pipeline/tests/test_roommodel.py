"""S9: assembling the RoomModel document.

Decision 3 of the plan makes this JSON the contract between the pipeline, the
layout engine, the database and the viewer. So these tests check it against
the published schema rather than against itself, and they check the 2.4
conventions that every consumer assumes but none of them verify.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from roomfittr_pipeline import geometry, roommodel, scale, shell
from roomfittr_pipeline.geometry import HeightProfile, Opening, RoomGeometry, WallSegment
from shapely.geometry import Polygon

SCHEMA_DIR = Path(__file__).resolve().parents[3] / "packages" / "schemas" / "src"


@pytest.fixture(scope="module")
def validator() -> jsonschema.protocols.Validator:
    """A validator that can resolve the schemas' cross-references."""
    store = {}
    for path in SCHEMA_DIR.glob("*.schema.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        store[path.name] = document
        store[document["$id"]] = document

    room_model = store["room-model.schema.json"]
    registry_resolver = jsonschema.RefResolver(
        base_uri=room_model["$id"], referrer=room_model, store=store
    )
    return jsonschema.Draft7Validator(room_model, resolver=registry_resolver)


def room(
    width: float = 4000.0,
    depth: float = 3000.0,
    *,
    openings: list[Opening] | None = None,
    ceiling_mm: float = 2500.0,
    ceiling_observed: bool = True,
    warnings: list[str] | None = None,
) -> RoomGeometry:
    corners = [(0.0, 0.0), (width, 0.0), (width, depth), (0.0, depth)]
    walls = [
        WallSegment(
            id=f"W{i + 1}",
            start=corners[i],
            end=corners[(i + 1) % 4],
            kind="solid",
            evidence_coverage=0.9,
        )
        for i in range(4)
    ]
    return RoomGeometry(
        polygon=Polygon(corners),
        walls=walls,
        openings=openings or [],
        profile=HeightProfile(0.0, ceiling_mm, ceiling_observed),
        warnings=warnings or [],
    )


def unit_scale() -> scale.ScaleResult:
    """One reconstruction unit is one millimetre, so the maths is readable."""
    return scale.fuse([scale.ScaleSource("model", 1.0), scale.ScaleSource("mono", 1.0)])


def build(geometry: RoomGeometry, result: scale.ScaleResult | None = None) -> dict[str, Any]:
    return roommodel.build(
        geometry,
        result or unit_scale(),
        pipeline_version="0.1.0",
        appearance=shell.appearance(geometry),
        frames_used=100,
        coverage_pct=87.5,
    )


class TestSchemaConformance:
    def test_a_plain_room_validates(self, validator: jsonschema.protocols.Validator) -> None:
        validator.validate(build(room()))

    def test_a_room_with_openings_validates(
        self, validator: jsonschema.protocols.Validator
    ) -> None:
        document = build(
            room(
                openings=[
                    Opening("O1", "W1", "door", 1200, 900, 0, 2030),
                    Opening("O2", "W3", "window", 1000, 1200, 900, 1200),
                ]
            )
        )
        validator.validate(document)
        assert len(document["openings"]) == 2

    def test_every_coordinate_is_an_integer(self) -> None:
        """2.4: integer millimetres in the JSON, the DB and the layout engine.
        A float here reaches Postgres and the TypeScript validator, both of
        which are typed for integers."""
        document = build(room(openings=[Opening("O1", "W1", "door", 1200, 900, 0, 2030)]))
        for x, z in document["floor_polygon"]:
            assert isinstance(x, int) and isinstance(z, int)
        for wall in document["walls"]:
            assert all(isinstance(v, int) for v in wall["start"] + wall["end"])
            assert isinstance(wall["length_mm"], int)
        for opening in document["openings"]:
            for key in ("offset_mm", "width_mm", "sill_mm", "height_mm"):
                assert isinstance(opening[key], int)

    def test_a_scaled_room_still_validates(self, validator: jsonschema.protocols.Validator) -> None:
        """The realistic case: the reconstruction is in its own units and the
        factor is whatever S7 fused."""
        metres = room(4.0, 3.0, ceiling_mm=2.5)
        result = scale.fuse([scale.ScaleSource("model", 1000.0), scale.ScaleSource("mono", 1000.0)])
        document = roommodel.build(metres, result, pipeline_version="0.1.0")
        validator.validate(document)
        assert document["ceiling_height_mm"] == 2500


class TestConventions:
    def test_the_origin_is_the_floor_centroid(self) -> None:
        """2.4. The viewer's camera and the solver's coordinates both assume
        it, and a room centred elsewhere loads off-screen."""
        assert roommodel.centroid_is_origin(build(room()))

    def test_an_offset_room_is_recentred(self) -> None:
        shifted = room()
        moved = RoomGeometry(
            polygon=Polygon(
                [(x + 12000, z - 5000) for x, z in shifted.polygon.exterior.coords[:-1]]
            ),
            walls=shifted.walls,
            openings=[],
            profile=shifted.profile,
        )
        assert roommodel.centroid_is_origin(build(moved))

    def test_the_longest_wall_lands_on_the_x_axis(self) -> None:
        """2.4 again. Fixed rather than arbitrary: the solver's 90-degree
        rotations and the viewer's default camera both depend on it."""
        assert roommodel.longest_wall_is_x_aligned(build(room(5000.0, 2500.0)))

    def test_a_rotated_room_is_squared_up(self) -> None:
        """The case the convention exists for. Without it, regenerating a
        layout could face the furniture a different way each time."""
        import math

        angle = math.radians(37.0)
        cos, sin = math.cos(angle), math.sin(angle)
        corners = [(0.0, 0.0), (5000.0, 0.0), (5000.0, 2500.0), (0.0, 2500.0)]
        turned = [(x * cos - z * sin, x * sin + z * cos) for x, z in corners]
        rotated = RoomGeometry(
            polygon=Polygon(turned),
            walls=[
                WallSegment(f"W{i + 1}", turned[i], turned[(i + 1) % 4], "solid", 0.9)
                for i in range(4)
            ],
            openings=[],
            profile=HeightProfile(0.0, 2500.0, True),
        )
        assert roommodel.longest_wall_is_x_aligned(build(rotated))

    def test_the_polygon_is_counter_clockwise(self) -> None:
        assert Polygon(build(room())["floor_polygon"]).exterior.is_ccw

    def test_area_survives_the_transform(self) -> None:
        """Canonicalisation is a rotation and a translation, so it must not
        change the room's size. A reflection would also 'work' and would
        mirror the floor plan."""
        document = build(room(4000.0, 3000.0))
        assert roommodel.floor_area_mm2(document) == pytest.approx(12_000_000, rel=0.001)


class TestScaleAndWarnings:
    def test_scale_multiplies_every_length(self) -> None:
        result = scale.fuse([scale.ScaleSource("model", 2.0), scale.ScaleSource("mono", 2.0)])
        document = roommodel.build(room(4000.0, 3000.0), result, pipeline_version="0.1.0")
        assert document["ceiling_height_mm"] == 5000
        assert max(w["length_mm"] for w in document["walls"]) == 8000

    def test_an_uncalibrated_scale_is_flagged_to_the_user(self) -> None:
        """3.11: a scale this uncertain must reach the user as a best guess,
        not as a measurement. The caveat travels in the document so it reaches
        the database and the layout engine too, not only the UI."""
        uncalibrated = scale.fuse(
            [
                scale.ScaleSource("model", 1.0),
                scale.ScaleSource("mono", 1.25),
                scale.ScaleSource("door", 1.35),
            ]
        )
        document = roommodel.build(room(), uncalibrated, pipeline_version="0.1.0")
        assert roommodel.WARN_SCALE_UNCALIBRATED in document["quality"]["warnings"]
        assert document["scale"]["confidence"] == "low"

    def test_a_calibrated_room_carries_no_scale_caveat(self) -> None:
        calibrated = scale.fuse([scale.ScaleSource("model", 1.0), scale.ScaleSource("user", 1.0)])
        document = roommodel.build(room(), calibrated, pipeline_version="0.1.0")
        assert roommodel.WARN_SCALE_UNCALIBRATED not in document["quality"]["warnings"]
        assert document["scale"]["user_calibrated"] is True

    def test_low_coverage_is_flagged(self) -> None:
        document = roommodel.build(
            room(), unit_scale(), pipeline_version="0.1.0", coverage_pct=41.0
        )
        assert roommodel.WARN_LOW_COVERAGE in document["quality"]["warnings"]

    def test_geometry_warnings_are_carried_through(
        self, validator: jsonschema.protocols.Validator
    ) -> None:
        document = build(
            room(ceiling_observed=False, warnings=[geometry.WARN_CEILING_NOT_OBSERVED])
        )
        validator.validate(document)
        assert geometry.WARN_CEILING_NOT_OBSERVED in document["quality"]["warnings"]
        assert document["ceiling_observed"] is False

    def test_warnings_are_deduplicated(self) -> None:
        document = build(room(warnings=[geometry.WARN_OPEN_BOUNDARY, geometry.WARN_OPEN_BOUNDARY]))
        assert document["quality"]["warnings"].count(geometry.WARN_OPEN_BOUNDARY) == 1

    def test_every_warning_comes_from_the_schema_vocabulary(self) -> None:
        """The enum is closed on purpose: a free-text warning cannot be
        counted, tested or translated. This is the guard that an invented
        code fails here rather than at publish time on a real scan."""
        import json

        common = json.loads((SCHEMA_DIR / "common.schema.json").read_text(encoding="utf-8"))
        allowed = set(common["definitions"]["warning_code"]["enum"])

        emitted = {
            value
            for module in (geometry, roommodel)
            for name, value in vars(module).items()
            if name.startswith("WARN_") and isinstance(value, str)
        }
        assert emitted, "no warning constants found; has the naming changed?"
        assert emitted <= allowed, f"not in the schema: {sorted(emitted - allowed)}"


class TestOpeningClamping:
    def test_rounding_never_pushes_an_opening_past_its_wall(
        self, validator: jsonschema.protocols.Validator
    ) -> None:
        """The schema forbids it and the shell's cutout would punch through
        the corner into the next wall. Scaling by an awkward factor is what
        provokes the off-by-one."""
        awkward = scale.fuse(
            [scale.ScaleSource("model", 1000.0 / 3.0), scale.ScaleSource("mono", 1000.0 / 3.0)]
        )
        geometry = room(
            12.0, 9.0, ceiling_mm=7.5, openings=[Opening("O1", "W1", "door", 11, 1, 0, 6)]
        )
        document = roommodel.build(geometry, awkward, pipeline_version="0.1.0")
        validator.validate(document)

        lengths = {w["id"]: w["length_mm"] for w in document["walls"]}
        for opening in document["openings"]:
            assert opening["offset_mm"] + opening["width_mm"] <= lengths[opening["wall_id"]]

    def test_an_opening_on_an_unknown_wall_is_dropped_not_raised(self) -> None:
        """S6's sanity check already rejects this. `build` stays a pure
        serialiser rather than a second place that can fail a scan."""
        geometry = room(openings=[Opening("O1", "W99", "door", 100, 900, 0, 2030)])
        assert roommodel.build(geometry, unit_scale(), pipeline_version="0.1.0")["openings"] == []
