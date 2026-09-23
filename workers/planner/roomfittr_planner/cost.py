"""What a layout costs to plan (implementation-plan.md 8, Phase 4).

Phase 4's definition of done includes "LLM cost per layout measured and
<= $0.10". Measured, not estimated -- so every call records its real token
counts from `response.usage` and this module turns them into money, rather
than anyone multiplying out a guess afterwards.

Money is integer micro-dollars throughout, for the same reason 5.5 H7 uses
integer cents: a budget check that disagrees with itself by a rounding error
is worse than no check. A micro-dollar is small enough that a single call's
cost is never rounded to zero -- one input token on Haiku is 1 micro-dollar.
"""

from __future__ import annotations

from dataclasses import dataclass

# Micro-dollars per million tokens. Every entry is exact, so
# `tokens * price // 1_000_000` never rounds a price into existence.
#
# Cache reads are 0.1x input and cache writes 1.25x input, per the Claude
# API's ephemeral-cache pricing. They are here because 5.4's planner prompt
# has a large stable prefix (the category vocabulary and the rules) and is a
# caching candidate; pricing it correctly now means the measurement stays
# honest if that lands later.
MICRO_DOLLARS_PER_MTOK: dict[str, dict[str, int]] = {
    "claude-sonnet-5": {
        "input": 2_000_000,
        "output": 10_000_000,
        "cache_read": 200_000,
        "cache_write": 2_500_000,
    },
    "claude-haiku-4-5-20251001": {
        "input": 1_000_000,
        "output": 5_000_000,
        "cache_read": 100_000,
        "cache_write": 1_250_000,
    },
}

# Aliases for the undated ids, which name the same models and the same prices.
MICRO_DOLLARS_PER_MTOK["claude-haiku-4-5"] = MICRO_DOLLARS_PER_MTOK["claude-haiku-4-5-20251001"]

# Phase 4's clause, in the units used here.
BUDGET_MICRO_DOLLARS = 100_000  # $0.10


@dataclass(frozen=True, slots=True)
class Usage:
    """Token counts for one call, plus the model that produced them.

    Kept separate from the cost so a caller can log what was actually spent
    in tokens -- which is what tells you *why* a layout was expensive --
    rather than only the total.
    """

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def micro_dollars(self) -> int:
        prices = MICRO_DOLLARS_PER_MTOK.get(self.model)
        if prices is None:
            # An unknown model is a pricing gap, not a free call. Charging the
            # most expensive rate we know keeps the <= $0.10 check
            # conservative: it can only ever report too much.
            prices = max(
                MICRO_DOLLARS_PER_MTOK.values(),
                key=lambda entry: entry["output"],
            )
        return (
            self.input_tokens * prices["input"]
            + self.output_tokens * prices["output"]
            + self.cache_read_tokens * prices["cache_read"]
            + self.cache_write_tokens * prices["cache_write"]
        ) // 1_000_000


@dataclass(frozen=True, slots=True)
class Spend:
    """Everything one layout cost, call by call."""

    calls: tuple[Usage, ...] = ()

    def plus(self, usage: Usage | None) -> Spend:
        if usage is None:
            return self
        return Spend(calls=(*self.calls, usage))

    @property
    def micro_dollars(self) -> int:
        return sum(usage.micro_dollars for usage in self.calls)

    @property
    def within_budget(self) -> bool:
        return self.micro_dollars <= BUDGET_MICRO_DOLLARS

    def as_meta(self) -> dict[str, object]:
        """6.2's `generation_meta`, so a layout records what it cost to make."""
        return {
            "llm_calls": len(self.calls),
            "llm_micro_dollars": self.micro_dollars,
            "llm_usage": [
                {
                    "model": usage.model,
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "cache_read_tokens": usage.cache_read_tokens,
                    "cache_write_tokens": usage.cache_write_tokens,
                    "micro_dollars": usage.micro_dollars,
                }
                for usage in self.calls
            ],
        }


def format_dollars(micro_dollars: int) -> str:
    return f"${micro_dollars / 1_000_000:.4f}"
