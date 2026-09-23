"""The tool schemas, and the parsing back out (5.4).

Two separate concerns, and the tests are split the same way:

- The schema is what the API enforces. If it is not a valid JSON Schema, or
  it does not forbid what 5.4 forbids, the model is free to answer wrongly
  and `post_check` has to clean up what the schema should have prevented.
- The parsing is what happens to a *well-formed* answer. `strict: true`
  makes most of the tolerance unreachable through the real transport, but
  the transport is a seam and a schema is a promise about one model version,
  so parsing must never raise.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import jsonschema
import pytest
from planner_helpers import plan_arguments, slot
from roomfittr_layout.planner import MAX_SLOTS
from roomfittr_layout.rules import CATEGORY_VOCABULARY
from roomfittr_planner.schemas import (
    PICK_TOOL_NAME,
    PLAN_TOOL_NAME,
    UNSUPPORTED_KEYWORDS,
    parse_picks,
    parse_plan,
    pick_tool,
    plan_tool,
)

ROOM_TYPES = ["bedroom", "dining", "living", "office"]


class TestPlanSchema:
    def test_it_is_a_valid_json_schema(self) -> None:
        schema = plan_tool(ROOM_TYPES)["input_schema"]
        jsonschema.Draft202012Validator.check_schema(schema)

    def test_a_good_plan_validates(self) -> None:
        schema = plan_tool(ROOM_TYPES)["input_schema"]
        arguments = plan_arguments(
            [
                slot(
                    "S1",
                    "sofa",
                    priority="must",
                    budget_share=0.4,
                    relation="against_wall",
                    anchor={"kind": "wall", "ref": "W1"},
                ),
                slot(
                    "S2",
                    "coffee_table",
                    relation="in_front_of",
                    anchor={"kind": "slot", "ref": "S1"},
                ),
            ]
        )
        jsonschema.Draft202012Validator(schema).validate(arguments)

    def test_a_category_outside_4_2_is_rejected_by_the_schema(self) -> None:
        """5.1: the LLM is "constrained to the closed vocabulary". Doing it in
        the schema means the model never spends tokens on a chaise longue."""
        schema = plan_tool(ROOM_TYPES)["input_schema"]
        arguments = plan_arguments([slot("S1", "chaise_longue")])
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate(arguments)

    def test_the_numeric_bounds_are_told_to_the_model(self) -> None:
        """5.4 caps V1 at ten slots and a share at [0, 1]. `maxItems` and
        `minimum`/`maximum` would say so in a way the API enforces, but a
        strict tool rejects all three -- so the bounds live in `description`
        for the model and in `post_check` for real, which is where they have
        to hold anyway."""
        slots = plan_tool(ROOM_TYPES)["input_schema"]["properties"]["slots"]
        assert "ten" in slots["description"]
        assert MAX_SLOTS == 10
        share = slots["items"]["properties"]["budget_share"]["description"]
        assert "between 0 and 1" in share

    def test_coordinates_cannot_be_smuggled_in(self) -> None:
        """Decision 4: the LLM "never outputs raw coordinates that get used
        unchecked". `additionalProperties: false` is what makes that true of
        the wire format rather than only of our reading of it."""
        schema = plan_tool(ROOM_TYPES)["input_schema"]
        arguments = plan_arguments([slot("S1", "sofa")])
        arguments["slots"][0]["x_mm"] = 1200
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate(arguments)

    def test_every_category_in_the_vocabulary_is_offered(self) -> None:
        schema = plan_tool(ROOM_TYPES)["input_schema"]
        offered = schema["properties"]["slots"]["items"]["properties"]["category"]["enum"]
        assert set(offered) == set(CATEGORY_VOCABULARY)

    def test_the_tool_asks_the_api_to_enforce_it(self) -> None:
        tool = plan_tool(ROOM_TYPES)
        assert tool["name"] == PLAN_TOOL_NAME
        assert tool["strict"] is True


def walk(node: Any) -> Iterator[dict[str, Any]]:
    """Every object in a schema tree."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


@pytest.mark.parametrize("tool", [plan_tool(ROOM_TYPES), pick_tool(["S1", "S2"]), pick_tool([])])
def test_no_schema_uses_a_keyword_strict_tools_reject(tool: dict[str, Any]) -> None:
    """The Claude API rejects `maxItems`, `minimum` and `maximum` on a strict
    tool with a 400. The fallback machinery then turns that into a template
    plan -- silently, on every layout, for as long as it takes someone to
    notice the model was never called. That is exactly the shape of failure
    this project keeps hitting, so it gets a test rather than a comment.
    """
    for node in walk(tool["input_schema"]):
        offending = UNSUPPORTED_KEYWORDS & set(node)
        assert not offending, f"{sorted(offending)} in {sorted(node)[:6]}"


class TestParsePlan:
    def test_it_reads_a_well_formed_answer(self) -> None:
        plan = parse_plan(
            plan_arguments(
                [
                    slot(
                        "S1",
                        "sofa",
                        priority="must",
                        budget_share=0.4,
                        anchor={"kind": "wall", "ref": "W1"},
                    ),
                    slot("S2", "rug", anchor={"kind": "slot", "ref": "S1"}),
                ]
            )
        )
        assert plan.room_type == "living"
        assert plan.style == "modern"
        assert not plan.from_template
        assert [s.category for s in plan.slots] == ["sofa", "rug"]
        assert plan.slots[0].anchor_wall == "W1"
        assert plan.slots[0].anchor_slot is None
        assert plan.slots[1].anchor_slot == "S1"
        assert plan.slots[1].anchor_wall is None

    def test_a_wall_anchor_is_never_read_as_a_slot_anchor(self) -> None:
        """They are different fields on `PlanSlot` and the solver treats them
        differently; confusing them puts an item against a wall that is
        actually another item."""
        plan = parse_plan(
            plan_arguments([slot("S1", "sofa", anchor={"kind": "wall", "ref": "S2"})])
        )
        assert plan.slots[0].anchor_wall == "S2"
        assert plan.slots[0].anchor_slot is None

    def test_notes_survive_for_debugging(self) -> None:
        plan = parse_plan(plan_arguments([slot("S1", "sofa", notes="faces the window")]))
        assert plan.slots[0].notes == "faces the window"

    @pytest.mark.parametrize(
        "arguments",
        [
            {},
            {"slots": None},
            {"slots": ["not a dict"]},
            {"slots": [{}]},
            {"slots": [{"category": 7}]},
            {"room_type": None, "slots": [], "style": 12, "rationale": None},
        ],
    )
    def test_it_never_raises(self, arguments: dict[str, Any]) -> None:
        """A parse error would cost the user their layout for a field nobody
        reads. Whatever comes back, `post_check` gets something to clean."""
        parse_plan(arguments)

    def test_a_malformed_slot_is_dropped_and_the_rest_survive(self) -> None:
        plan = parse_plan(
            {
                "room_type": "living",
                "rationale": "",
                "slots": [
                    {"category": 7},
                    slot("S2", "sofa"),
                    "nonsense",
                ],
            }
        )
        assert [s.category for s in plan.slots] == ["sofa"]

    def test_a_missing_slot_id_is_given_one(self) -> None:
        plan = parse_plan({"slots": [{"category": "sofa"}, {"category": "rug"}]})
        assert [s.slot_id for s in plan.slots] == ["S1", "S2"]

    def test_an_unparseable_share_becomes_zero_not_a_guess(self) -> None:
        """`post_check` floors it and renormalises, which spreads the money
        over the slots that did parse. Inventing an allocation here would
        instead spend it on the slot we understood least."""
        plan = parse_plan({"slots": [{"category": "sofa", "budget_share": "lots"}]})
        assert plan.slots[0].budget_share == 0.0

    def test_a_boolean_share_is_not_read_as_one(self) -> None:
        """`isinstance(True, int)` is True in Python, so a bare numeric check
        turns `true` into a 100% budget share."""
        plan = parse_plan({"slots": [{"category": "sofa", "budget_share": True}]})
        assert plan.slots[0].budget_share == 0.0

    def test_an_unknown_priority_becomes_should_not_must(self) -> None:
        """A `must` is never dropped, so defaulting upwards turns a typo into
        a layout that fails instead of one that is merely smaller."""
        plan = parse_plan({"slots": [{"category": "sofa", "priority": "critical"}]})
        assert plan.slots[0].priority == "should"


class TestPickSchema:
    def test_it_is_a_valid_json_schema(self) -> None:
        jsonschema.Draft202012Validator.check_schema(pick_tool(["S1", "S2"])["input_schema"])

    def test_it_only_offers_slots_that_exist(self) -> None:
        schema = pick_tool(["S1", "S2"])["input_schema"]
        items = schema["properties"]["picks"]["items"]
        assert items["properties"]["slot_id"]["enum"] == ["S1", "S2"]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate(
                {"picks": [{"slot_id": "S9", "product_id": "sofa-0"}]}
            )

    def test_no_slots_still_produces_a_usable_schema(self) -> None:
        """An empty enum is invalid JSON Schema, and a room with nothing to
        choose between is a real state -- 7.4's "catalog too thin"."""
        jsonschema.Draft202012Validator.check_schema(pick_tool([])["input_schema"])

    def test_the_tool_asks_the_api_to_enforce_it(self) -> None:
        tool = pick_tool(["S1"])
        assert tool["name"] == PICK_TOOL_NAME
        assert tool["strict"] is True


class TestParsePicks:
    def test_it_reads_a_well_formed_answer(self) -> None:
        picks = parse_picks(
            {
                "picks": [
                    {"slot_id": "S1", "product_id": "sofa-1"},
                    {"slot_id": "S2", "product_id": "rug-0"},
                ]
            }
        )
        assert picks == {"S1": "sofa-1", "S2": "rug-0"}

    def test_a_repeated_slot_keeps_the_first_answer(self) -> None:
        picks = parse_picks(
            {
                "picks": [
                    {"slot_id": "S1", "product_id": "sofa-1"},
                    {"slot_id": "S1", "product_id": "sofa-2"},
                ]
            }
        )
        assert picks == {"S1": "sofa-1"}

    @pytest.mark.parametrize(
        "arguments",
        [{}, {"picks": None}, {"picks": ["x"]}, {"picks": [{"slot_id": 1, "product_id": 2}]}],
    )
    def test_it_never_raises(self, arguments: dict[str, Any]) -> None:
        assert parse_picks(arguments) == {}
