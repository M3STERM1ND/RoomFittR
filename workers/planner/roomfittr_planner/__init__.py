"""The Claude half of the layout engine (implementation-plan.md 5.4 L2, L4).

`roomfittr_layout` is deliberately network-free: everything that decides
whether a layout is legal runs without an API key, and so do its tests. This
package is the other half -- the two model calls Decision 4 allows, each
wrapped so that failing is indistinguishable, to the caller, from never
having been configured.
"""

from .client import AnthropicTransport, Transport, TransportError, available
from .cost import BUDGET_MICRO_DOLLARS, Spend, Usage, format_dollars
from .generate import PlannedLayout, generate_layout
from .pick import PickAttempt, pick_products
from .plan import PlanAttempt, propose_plan

__all__ = [
    "BUDGET_MICRO_DOLLARS",
    "AnthropicTransport",
    "PickAttempt",
    "PlanAttempt",
    "PlannedLayout",
    "Spend",
    "Transport",
    "TransportError",
    "Usage",
    "available",
    "format_dollars",
    "generate_layout",
    "pick_products",
    "propose_plan",
]
