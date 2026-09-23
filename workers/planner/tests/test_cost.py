"""Phase 4's cost clause: "LLM cost per layout measured and <= $0.10"."""

from __future__ import annotations

import pytest
from roomfittr_planner.cost import (
    BUDGET_MICRO_DOLLARS,
    MICRO_DOLLARS_PER_MTOK,
    Spend,
    Usage,
    format_dollars,
)


class TestPricing:
    def test_a_million_input_tokens_on_sonnet_costs_two_dollars(self) -> None:
        """1.2's table: Sonnet is $2.00 per million input tokens."""
        usage = Usage(model="claude-sonnet-5", input_tokens=1_000_000)
        assert usage.micro_dollars == 2_000_000

    def test_a_million_output_tokens_on_sonnet_costs_ten_dollars(self) -> None:
        usage = Usage(model="claude-sonnet-5", output_tokens=1_000_000)
        assert usage.micro_dollars == 10_000_000

    def test_haiku_is_half_the_input_price_of_sonnet(self) -> None:
        tokens = 1_000_000
        sonnet = Usage(model="claude-sonnet-5", input_tokens=tokens).micro_dollars
        haiku = Usage(model="claude-haiku-4-5-20251001", input_tokens=tokens).micro_dollars
        assert haiku * 2 == sonnet

    def test_the_dated_and_undated_haiku_ids_price_alike(self) -> None:
        """1.2 names the dated id; the API reports the same model either way."""
        tokens = 12_345
        assert (
            Usage(model="claude-haiku-4-5", input_tokens=tokens).micro_dollars
            == Usage(model="claude-haiku-4-5-20251001", input_tokens=tokens).micro_dollars
        )

    def test_cache_reads_are_a_tenth_of_input(self) -> None:
        tokens = 1_000_000
        full = Usage(model="claude-sonnet-5", input_tokens=tokens).micro_dollars
        cached = Usage(model="claude-sonnet-5", cache_read_tokens=tokens).micro_dollars
        assert cached * 10 == full

    def test_a_single_token_is_not_rounded_to_free(self) -> None:
        """Why micro-dollars: in cents, every realistic call rounds to zero and
        the measurement stops measuring."""
        assert Usage(model="claude-haiku-4-5", input_tokens=1).micro_dollars == 1

    def test_an_unknown_model_is_priced_at_the_dearest_rate_not_free(self) -> None:
        """A pricing gap must not be able to make a layout look cheap enough
        to pass Phase 4's clause."""
        unknown = Usage(model="claude-something-new", output_tokens=1_000).micro_dollars
        dearest = max(entry["output"] for entry in MICRO_DOLLARS_PER_MTOK.values())
        assert unknown == 1_000 * dearest // 1_000_000
        assert unknown > 0

    @pytest.mark.parametrize("field", ["input_tokens", "output_tokens"])
    def test_cost_is_linear_in_tokens(self, field: str) -> None:
        one = Usage(model="claude-sonnet-5", **{field: 1_000}).micro_dollars
        ten = Usage(model="claude-sonnet-5", **{field: 10_000}).micro_dollars
        assert ten == one * 10


class TestSpend:
    def test_it_sums_its_calls(self) -> None:
        spend = (
            Spend()
            .plus(Usage(model="claude-sonnet-5", input_tokens=2_000, output_tokens=500))
            .plus(Usage(model="claude-haiku-4-5", input_tokens=3_000, output_tokens=200))
        )
        assert spend.micro_dollars == 4_000 + 5_000 + 3_000 + 1_000
        assert len(spend.calls) == 2

    def test_a_failed_call_adds_nothing(self) -> None:
        assert Spend().plus(None).micro_dollars == 0

    def test_a_typical_layout_is_inside_the_phase_4_budget(self) -> None:
        """A plan call of ~3k in / 1.5k out plus a pick of ~4k in / 300 out.
        The real numbers come from `test_live.py`; this pins the arithmetic so
        a pricing edit that breaks the clause fails here rather than in a bill.
        """
        spend = (
            Spend()
            .plus(Usage(model="claude-sonnet-5", input_tokens=3_000, output_tokens=1_500))
            .plus(Usage(model="claude-haiku-4-5", input_tokens=4_000, output_tokens=300))
        )
        assert spend.within_budget
        assert spend.micro_dollars == 6_000 + 15_000 + 4_000 + 1_500

    def test_an_extravagant_layout_is_reported_as_over(self) -> None:
        spend = Spend().plus(Usage(model="claude-sonnet-5", output_tokens=20_000))
        assert not spend.within_budget
        assert spend.micro_dollars > BUDGET_MICRO_DOLLARS

    def test_meta_records_every_call_for_6_2(self) -> None:
        spend = Spend().plus(Usage(model="claude-sonnet-5", input_tokens=10, output_tokens=2))
        meta = spend.as_meta()
        assert meta["llm_calls"] == 1
        assert meta["llm_micro_dollars"] == 40
        assert meta["llm_usage"][0]["model"] == "claude-sonnet-5"  # type: ignore[index]


def test_format_dollars_keeps_a_tenth_of_a_cent_visible() -> None:
    assert format_dollars(26_500) == "$0.0265"
