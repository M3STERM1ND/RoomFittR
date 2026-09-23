"""The tool schemas Claude fills in, and the parsing back out (5.4).

1.2 chose "structured JSON output via tool schemas" over free-form JSON, so
the model's answer arrives already shaped. With `strict: true` the API
guarantees the arguments validate against these schemas -- which means the
parsing here is not defending against malformed JSON. It is defending
against a *well-formed* answer that is wrong: a wall id from another room,
eleven slots, a budget share of 40.

That distinction decides what belongs where. Anything a JSON Schema can
express is expressed here so the model gets it right the first time.
Everything else is `planner.post_check`, which already exists, is already
tested against exactly these failures, and is the deterministic authority
either way.

**The strict-tool subset is narrower than JSON Schema.** `maxItems`,
`minimum` and `maximum` are rejected with a 400 -- see `UNSUPPORTED_KEYWORDS`
and the test that enforces it. So 5.4's numeric bounds (ten slots, a share
in [0, 1]) are stated in `description` for the model and enforced in
`post_check` for real. That is the right place for them anyway: they have to
hold whatever the API does or does not check.
"""

from __future__ import annotations

from typing import Any

from roomfittr_layout.planner import LayoutPlan, PlanSlot
from roomfittr_layout.rules import CATEGORY_VOCABULARY

PLAN_TOOL_NAME = "submit_layout_plan"
PICK_TOOL_NAME = "submit_product_picks"

PRIORITIES = ["must", "should", "nice"]

# 5.4's intent vocabulary. The solver derives the actual relation from
# `category_rules.yaml` (5.3 puts `in_front_of(sofa)` in the rules, not in
# the plan), so these steer the model's reasoning and are recorded on the
# plan; V1's solver consumes the anchor alone. Keeping them in the schema
# means the wire format does not change when the solver grows to use them.
RELATIONS = ["against_wall", "in_front_of", "beside", "under", "around", "float"]

# Keywords the Claude API rejects on a `strict: true` tool with
# "property 'X' is not supported". Measured against the live API rather than
# recalled; `test_tool_schemas.py` walks every schema here for them, so
# adding one back fails a test instead of a production layout.
UNSUPPORTED_KEYWORDS = frozenset({"maxItems", "minimum", "maximum"})


def plan_tool(room_types: list[str]) -> dict[str, Any]:
    """The `LayoutPlan` tool, per 5.4 L2's Output block."""
    return {
        "name": PLAN_TOOL_NAME,
        "description": (
            "Submit the furniture plan for this room. Call this exactly once. "
            "Choose which categories of furniture the room should have, how to "
            "divide the budget between them, and which wall or which other slot "
            "each one relates to. Do not give coordinates or sizes -- a solver "
            "works those out from your anchors."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["room_type", "slots", "rationale"],
            "properties": {
                "room_type": {
                    "type": "string",
                    "enum": room_types,
                    "description": "The room type this plan is for.",
                },
                "style": {
                    "type": ["string", "null"],
                    "description": (
                        "One style word for the whole room, used to rank products. "
                        "Null if the user gave no preference and the room suggests none."
                    ),
                },
                "rationale": {
                    "type": "string",
                    "description": (
                        "One or two sentences, shown to the user, explaining the "
                        "arrangement in plain language. No ids."
                    ),
                },
                "slots": {
                    "type": "array",
                    "minItems": 1,
                    "description": (
                        "At most ten items -- an eleventh is discarded, lowest "
                        "priority first. Anchors first, then what depends on them."
                    ),
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["slot_id", "category", "priority", "budget_share", "intent"],
                        "properties": {
                            "slot_id": {
                                "type": "string",
                                "pattern": "^S[0-9]+$",
                                "description": "S1, S2, ... unique within the plan.",
                            },
                            "category": {
                                "type": "string",
                                "enum": sorted(CATEGORY_VOCABULARY),
                            },
                            "priority": {
                                "type": "string",
                                "enum": PRIORITIES,
                                "description": (
                                    "'must' is never dropped to fit the budget or the room, "
                                    "so use it only for what makes the room the room."
                                ),
                            },
                            "budget_share": {
                                "type": "number",
                                "description": (
                                    "A fraction of the budget between 0 and 1. Shares "
                                    "are renormalised afterwards, so they need not sum "
                                    "to 1, but a share outside that range is clamped."
                                ),
                            },
                            "intent": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["relation"],
                                "properties": {
                                    "relation": {"type": "string", "enum": RELATIONS},
                                    "anchor": {
                                        "type": ["object", "null"],
                                        "additionalProperties": False,
                                        "required": ["kind", "ref"],
                                        "properties": {
                                            "kind": {
                                                "type": "string",
                                                "enum": ["wall", "slot"],
                                            },
                                            "ref": {
                                                "type": "string",
                                                "description": (
                                                    "A wall id (W1) or a slot_id (S1) from "
                                                    "this plan. Only ids you were given."
                                                ),
                                            },
                                        },
                                    },
                                    "facing": {
                                        "type": ["string", "null"],
                                        "description": (
                                            "A wall or opening id this item should face, or null."
                                        ),
                                    },
                                    "notes": {
                                        "type": ["string", "null"],
                                        "description": "A short reason, for debugging.",
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
    }


def parse_plan(arguments: dict[str, Any]) -> LayoutPlan:
    """Turn the tool arguments into a `LayoutPlan`.

    Tolerant on purpose. `strict: true` makes most of this unreachable, but
    the transport is a seam that tests drive directly and a schema is a
    promise about one model version -- so a missing key or a string where a
    number belongs must produce a plan that `post_check` can clean up, never
    an exception that costs the user their layout.
    """
    slots: list[PlanSlot] = []
    raw_slots = arguments.get("slots")
    if not isinstance(raw_slots, list):
        raw_slots = []

    for index, raw in enumerate(raw_slots):
        if not isinstance(raw, dict):
            continue
        category = raw.get("category")
        if not isinstance(category, str):
            continue

        intent = raw.get("intent")
        intent = intent if isinstance(intent, dict) else {}
        anchor = intent.get("anchor")
        anchor = anchor if isinstance(anchor, dict) else {}
        kind = anchor.get("kind")
        ref = anchor.get("ref")
        ref = ref if isinstance(ref, str) else None

        notes = intent.get("notes")

        slots.append(
            PlanSlot(
                slot_id=_slot_id(raw.get("slot_id"), index),
                category=category,
                priority=_priority(raw.get("priority")),
                budget_share=_share(raw.get("budget_share")),
                anchor_wall=ref if kind == "wall" else None,
                anchor_slot=ref if kind == "slot" else None,
                notes=notes if isinstance(notes, str) else "",
            )
        )

    room_type = arguments.get("room_type")
    style = arguments.get("style")
    rationale = arguments.get("rationale")

    return LayoutPlan(
        room_type=room_type if isinstance(room_type, str) else "",
        slots=slots,
        style=style if isinstance(style, str) and style else None,
        rationale=rationale if isinstance(rationale, str) else "",
        from_template=False,
    )


def _slot_id(value: Any, index: int) -> str:
    return value if isinstance(value, str) and value else f"S{index + 1}"


def _priority(value: Any) -> str:
    return value if value in PRIORITIES else "should"


def _share(value: Any) -> float:
    # A share that is not a number becomes zero rather than a guess:
    # `post_check` floors it at MIN_SHARE and renormalises, which spreads the
    # money over the slots that did parse instead of inventing an allocation.
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(value)


def pick_tool(slot_ids: list[str]) -> dict[str, Any]:
    """The `ProductPicks` tool, per 5.4 L4.

    One call covers every slot. The schema cannot express "this id is in
    *that* slot's shortlist" -- cross-field constraints are beyond JSON
    Schema -- so `parse_picks` checks membership and the caller substitutes
    the top-ranked item, exactly as 5.4 requires.
    """
    return {
        "name": PICK_TOOL_NAME,
        "description": (
            "Choose one product for each slot. Pick the combination that looks "
            "most like it was furnished by one person: consistent materials, "
            "colours that sit together, a coherent style. Every id must come "
            "from that slot's own options."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["picks"],
            "properties": {
                "picks": {
                    "type": "array",
                    "minItems": 0,
                    "description": "One entry per slot. An empty list means no preference.",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["slot_id", "product_id"],
                        "properties": {
                            "slot_id": {
                                "type": "string",
                                "enum": slot_ids or ["S1"],
                            },
                            "product_id": {"type": "string"},
                        },
                    },
                },
                "note": {
                    "type": ["string", "null"],
                    "description": "One short sentence on what ties the choices together.",
                },
            },
        },
    }


def parse_picks(arguments: dict[str, Any]) -> dict[str, str]:
    """Slot id -> product id, keeping the first answer for a repeated slot."""
    picks: dict[str, str] = {}
    raw = arguments.get("picks")
    if not isinstance(raw, list):
        return picks
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        slot_id = entry.get("slot_id")
        product_id = entry.get("product_id")
        if not isinstance(slot_id, str) or not isinstance(product_id, str):
            continue
        picks.setdefault(slot_id, product_id)
    return picks
