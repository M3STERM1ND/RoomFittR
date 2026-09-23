"""The real Claude calls (implementation-plan.md 8, Phase 4).

Phase 4's definition of done ends with "LLM cost per layout **measured** and
<= $0.10". Nothing scripted can discharge that clause: the token counts come
from the API, the prompt length is whatever the real room summary and
catalog make it, and what the model does with a tool schema is exactly the
thing under test.

So this file spends money, and it is opt-in twice over -- an API key **and**
`ROOMFITTR_LIVE_LLM=1`. 9.1 gates the chaos tests on every CI run; gating
these there too would bill the project for every push, and a rate limit or
an outage would turn a red build into a question about Anthropic rather than
about the commit.

Run it deliberately:

    ROOMFITTR_LIVE_LLM=1 uv run pytest workers/planner/tests/test_live.py -v -s

The measurement it prints is the number that belongs in
`docs/phase-4-progress.md`.
"""

from __future__ import annotations

import os

import pytest
from planner_helpers import catalog
from roomfittr_layout.room import RoomAnalysis
from roomfittr_planner.client import AnthropicTransport, available
from roomfittr_planner.cost import BUDGET_MICRO_DOLLARS, format_dollars
from roomfittr_planner.generate import PlannedLayout, generate_layout
from roomfittr_planner.plan import propose_plan

BUDGET = 300_000

pytestmark = [
    pytest.mark.skipif(not available(), reason="no ANTHROPIC_API_KEY"),
    pytest.mark.skipif(
        os.environ.get("ROOMFITTR_LIVE_LLM") != "1",
        reason="live LLM tests are opt-in: set ROOMFITTR_LIVE_LLM=1",
    ),
]


@pytest.fixture(scope="module")
def transport() -> AnthropicTransport:
    return AnthropicTransport()


def assert_legal(planned: PlannedLayout, *, budget_cents: int = BUDGET) -> None:
    report = planned.layout.report
    hard = [v for v in report.violations if v.severity == "hard"]
    for violations in report.items.values():
        hard.extend(v for v in violations if v.severity == "hard")
    assert not hard, "; ".join(v.message for v in hard)
    assert report.fits
    assert sum(item.price_cents for item in planned.layout.items) <= budget_cents


class TestTheRealModel:
    def test_the_planner_answers_with_a_usable_plan(
        self, transport: AnthropicTransport, living: RoomAnalysis
    ) -> None:
        """`tool_choice` is `auto`, so this is also the test that the prompt
        actually gets the tool called."""
        attempt = propose_plan(
            transport,
            living,
            room_type="living",
            budget_cents=BUDGET,
            catalog=catalog(),
            style="scandinavian",
        )
        print(
            f"\n  plan: {format_dollars(attempt.spend.micro_dollars)}, "
            f"{len(attempt.plan.slots)} slots, failure={attempt.failure!r}"
        )
        assert not attempt.plan.from_template, f"fell back: {attempt.failure}"
        assert attempt.plan.slots
        assert attempt.plan.rationale

    def test_it_anchors_the_big_thing_to_a_real_wall(
        self, transport: AnthropicTransport, living: RoomAnalysis
    ) -> None:
        """The judgement D12 is about. A plan whose anchors are all None is
        schema-valid, costs the same, and tells the solver nothing."""
        attempt = propose_plan(
            transport, living, room_type="living", budget_cents=BUDGET, catalog=catalog()
        )
        assert not attempt.plan.from_template
        wall_ids = {wall.id for wall in living.walls}
        anchored = [s for s in attempt.plan.slots if s.anchor_wall in wall_ids or s.anchor_slot]
        print(f"\n  {len(anchored)}/{len(attempt.plan.slots)} slots anchored")
        assert anchored, "the model anchored nothing to anything"

    @pytest.mark.parametrize("room_type", ["living", "bedroom"])
    def test_a_whole_layout_end_to_end(
        self, transport: AnthropicTransport, living: RoomAnalysis, room_type: str
    ) -> None:
        planned = generate_layout(
            living,
            catalog(),
            room_type=room_type,
            budget_cents=BUDGET,
            transport=transport,
            style="modern",
        )
        cost = planned.spend.micro_dollars
        print(
            f"\n  {room_type}: {format_dollars(cost)} over "
            f"{len(planned.spend.calls)} calls, "
            f"{len(planned.layout.items)} items placed, "
            f"plan={'template' if planned.plan.from_template else 'llm'}"
        )
        assert_legal(planned)

        # Phase 4's clause. Asserted rather than printed, because a prompt
        # that grows until a layout costs a dollar should fail a test and not
        # merely appear in a log nobody reads.
        assert cost <= BUDGET_MICRO_DOLLARS, (
            f"{format_dollars(cost)} per layout exceeds {format_dollars(BUDGET_MICRO_DOLLARS)}"
        )

    def test_the_picker_chooses_from_the_shortlist(
        self, transport: AnthropicTransport, living: RoomAnalysis
    ) -> None:
        """5.4 L4's rule, against the real Haiku rather than a script."""
        planned = generate_layout(
            living,
            catalog(),
            room_type="living",
            budget_cents=BUDGET,
            transport=transport,
        )
        print(f"\n  rejected picks: {planned.rejected_picks or 'none'}")
        assert_legal(planned)
        placed = {p.slot_id: p.product_id for p in planned.layout.result.placements}
        known = {p.id for p in catalog()}
        assert all(product_id in known for product_id in placed.values())
