"""The one place in the repo that speaks to the Claude API.

Everything else in this package -- the prompts, the schemas, the post-checks,
the fallbacks -- is pure. That split is deliberate: 9.1 requires "LLM chaos
tests" in which the model returns invalid JSON, nonexistent ids and absurd
budget shares and the engine still produces a valid layout. Those tests need
to *be* the model, so the model has to be behind a seam narrow enough to
stand in for.

The seam is one method: given a system prompt, a user message and a single
tool, return the arguments the model passed to that tool, or None. Every
Claude-specific concern -- SDK types, beta flags, thinking configuration,
token accounting -- stops here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol

from .cost import Usage

# 1.2's approved stack, verbatim: "claude-sonnet-5 for layout planning,
# claude-haiku-4-5-20251001 for extraction fallback, product style/category
# tagging (vision), and room-type inference". D12 is the open question of
# whether these are the right choice; overriding them by environment is how
# that experiment gets run without a code change.
PLANNER_MODEL = os.environ.get("ANTHROPIC_LAYOUT_MODEL", "claude-sonnet-5")
PICKER_MODEL = os.environ.get("ANTHROPIC_PICK_MODEL", "claude-haiku-4-5-20251001")

# 7.1 budgets layout generation at 10-40 s end to end, and the solver may
# want several seconds of that. A call that has not answered by then has
# already lost to the template.
PLANNER_TIMEOUT_S = 30.0
PICKER_TIMEOUT_S = 15.0


class TransportError(RuntimeError):
    """The call did not produce an answer. Always recoverable by the caller.

    7.4: "LLM errors -> template fallback (never a user-visible failure)".
    Carries `kind` so the fallback can be logged with a cause rather than as
    an anonymous failure.
    """

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What the model passed to the tool, plus what the call cost."""

    arguments: dict[str, Any]
    usage: Usage


class Transport(Protocol):
    """The narrow seam. Implemented once for real and once for tests."""

    def call_tool(
        self,
        *,
        model: str,
        system: str,
        user: str,
        tool: dict[str, Any],
        max_tokens: int,
        timeout_s: float,
        think: bool,
    ) -> ToolResult: ...


class AnthropicTransport:
    """The real thing.

    Notes on the request shape, because each choice is load-bearing:

    - **`tool_choice` is `auto`, not forced.** Forcing a tool is the obvious
      way to guarantee structured output, but it is rejected on the newest
      models and it cannot be combined with thinking. `auto` plus `strict`
      plus an instruction naming the tool gets the same result everywhere,
      and a turn that answers in prose instead is already handled -- it is
      indistinguishable from an invalid schema, and 5.1 says two of those
      mean the template.
    - **`strict: true`** makes the arguments schema-valid when they arrive,
      so `schemas.py` is checking the model's *judgement* (are these real
      wall ids?) rather than its JSON.
    - **Adaptive thinking on the planner.** 5.4 L2 is a spatial reasoning
      task and D12 is explicitly about its quality. Haiku 4.5 does not take
      adaptive thinking and the pick is a preference question, so it runs
      without.
    """

    def __init__(self, client: Any | None = None) -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self._client = client

    def call_tool(
        self,
        *,
        model: str,
        system: str,
        user: str,
        tool: dict[str, Any],
        max_tokens: int,
        timeout_s: float,
        think: bool,
    ) -> ToolResult:
        import anthropic

        request: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "tools": [tool],
            "tool_choice": {"type": "auto"},
        }
        if think:
            request["thinking"] = {"type": "adaptive"}

        try:
            response = self._client.with_options(timeout=timeout_s).messages.create(**request)
        except anthropic.APITimeoutError as error:
            raise TransportError("timeout", str(error)) from error
        except anthropic.RateLimitError as error:
            raise TransportError("rate_limited", str(error)) from error
        except anthropic.AuthenticationError as error:
            raise TransportError("auth", str(error)) from error
        except anthropic.BadRequestError as error:
            raise TransportError("bad_request", str(error)) from error
        except anthropic.APIStatusError as error:
            raise TransportError("api_status", f"{error.status_code}") from error
        except anthropic.APIConnectionError as error:
            raise TransportError("connection", str(error)) from error

        usage = _usage_of(response, model)

        # Check why the turn ended before reading its content: a refusal or a
        # truncation produces content that looks plausible and is not an
        # answer. The cost is still real, so it is attached to the error's
        # caller by way of the usage having been computed first.
        stop = getattr(response, "stop_reason", None)
        if stop in {"refusal", "max_tokens"}:
            raise TransportError(f"stop_{stop}", "")

        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", None) != "tool_use":
                continue
            if getattr(block, "name", None) != tool["name"]:
                continue
            arguments = getattr(block, "input", None)
            if not isinstance(arguments, dict):
                raise TransportError("tool_input_not_object", type(arguments).__name__)
            return ToolResult(arguments=arguments, usage=usage)

        raise TransportError("no_tool_call", str(stop))


def _usage_of(response: Any, model: str) -> Usage:
    usage = getattr(response, "usage", None)
    if usage is None:
        return Usage(model=model)
    return Usage(
        model=getattr(response, "model", None) or model,
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        cache_write_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
    )


def available() -> bool:
    """Whether a real call could be made at all.

    Used to decide between the model and the template *before* spending a
    timeout finding out, and to skip the live tests rather than fail them on
    a machine with no key.
    """
    return bool(os.environ.get("ANTHROPIC_API_KEY"))
