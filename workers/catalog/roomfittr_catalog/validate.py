"""4.7's acceptance gate: does this product become `active`?

4.7 is a table of hard checks and it is unusually precise about the outcome
of each: a product "becomes `active` only if **all** hard checks pass.
Otherwise it gets a `rejected_*` status with a reason." This module is that
table, and the statuses it returns are exactly the ones the `product_status`
enum holds.

The distinction that carries the most weight is **rejected versus
pending_review**. A rejection is a judgement about the product: there is no
price, the link is off-domain, the dimensions are impossible. A
`pending_review` is a judgement about *us*: the numbers are strange in a way
that is usually a parser bug rather than a bad listing, and 4.7 routes those
to "a simple internal admin page (sort by frequency of failure reason, fix
parser, re-run)". Marking a parser bug `rejected` would throw away the
evidence that the parser is wrong.

Nothing here touches the network. The two checks that need one -- that the
URL resolves without an off-domain redirect, and that the image returns
`image/*` -- take their answers as arguments, so the whole gate is testable
without either, and so a crawl that cannot reach a retailer does not quietly
reclassify its whole catalog.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

RULES_PATH = Path(__file__).with_name("product_rules.yaml")


class Status(StrEnum):
    """`public.product_status`, minus the states nothing here produces.

    `archived` is a lifecycle decision made elsewhere (6.3: products are
    "archived, never hard-deleted while referenced") and is not an outcome of
    validation.
    """

    ACTIVE = "active"
    PENDING_REVIEW = "pending_review"
    UNAVAILABLE = "unavailable"
    REJECTED_NO_DIMENSIONS = "rejected_no_dimensions"
    REJECTED_IMPLAUSIBLE_DIMENSIONS = "rejected_implausible_dimensions"
    REJECTED_NO_PRICE = "rejected_no_price"
    REJECTED_BAD_LINK = "rejected_bad_link"
    REJECTED_NO_IMAGE = "rejected_no_image"


# 4.7: "Height not > 3x width for non-tall categories".
MAX_HEIGHT_TO_WIDTH = 3.0

# 4.7: "Category mapped: In closed vocabulary with confidence >= 0.7".
MIN_CATEGORY_CONFIDENCE = 0.7

# 4.6: "V1 is single-market (USD); non-USD products are excluded".
MARKET_CURRENCY = "USD"


@dataclass(frozen=True, slots=True)
class Bands:
    """One category's acceptance ranges."""

    width_mm: tuple[int, int]
    depth_mm: tuple[int, int]
    height_mm: tuple[int, int]
    price_cents: tuple[int, int]
    tall: bool = False
    allow_cube: bool = False


@cache
def _rules() -> tuple[Bands, dict[str, Bands]]:
    raw: dict[str, Any] = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    defaults = _bands(raw["defaults"], None)
    categories = {
        name: _bands(entry, defaults) for name, entry in (raw.get("categories") or {}).items()
    }
    return defaults, categories


def _bands(entry: dict[str, Any], fallback: Bands | None) -> Bands:
    def band(key: str) -> tuple[int, int]:
        if key in entry:
            low, high = entry[key]
            return int(low), int(high)
        if fallback is None:
            raise KeyError(f"product_rules.yaml: defaults must define {key}")
        return getattr(fallback, key)  # type: ignore[no-any-return]

    return Bands(
        width_mm=band("width_mm"),
        depth_mm=band("depth_mm"),
        height_mm=band("height_mm"),
        price_cents=band("price_cents"),
        tall=bool(entry.get("tall", False)),
        allow_cube=bool(entry.get("allow_cube", False)),
    )


def bands_for(category: str) -> Bands:
    """The bands a category is judged against, defaults if it has none.

    A category with no entry still gets judged. 4.2's vocabulary can grow
    ahead of this file, and a new category should degrade to "generous
    bounds" rather than to "everything is accepted" -- which is what
    returning None here would amount to.
    """
    defaults, categories = _rules()
    return categories.get(category, defaults)


def categories_with_bands() -> set[str]:
    return set(_rules()[1])


@dataclass(frozen=True, slots=True)
class Product:
    """A candidate row, as 4.7 sees it just before it is accepted.

    Deliberately not the `products` table: the columns that matter to this
    decision are few, and taking the whole row would mean the gate could not
    be run on something the crawler has not finished assembling.
    """

    category: str
    width_mm: int | None = None
    depth_mm: int | None = None
    height_mm: int | None = None
    price_cents: int | None = None
    currency: str = MARKET_CURRENCY
    canonical_url: str = ""
    image_url: str = ""
    category_confidence: float | None = None
    availability: str = "unknown"
    # Answers the crawler already has. Taken as arguments rather than
    # fetched, so this module needs no network and a retailer being briefly
    # unreachable cannot reclassify its whole catalog.
    url_resolves: bool = True
    url_redirected_off_domain: bool = False
    image_content_type: str = "image/jpeg"


@dataclass(frozen=True, slots=True)
class Verdict:
    """What 4.7 decided, and every reason behind it."""

    status: Status
    reasons: tuple[str, ...] = field(default=())

    @property
    def accepted(self) -> bool:
        return self.status is Status.ACTIVE


def validate(product: Product, *, allowed_hosts: set[str] | None = None) -> Verdict:
    """4.7's table, in its own order. Returns the first failing status.

    The order matters and is the plan's: a product with no dimensions is
    rejected for that and not also for a price band it was never measured
    against. The reasons list carries everything found, so the admin page
    4.7 asks for can sort by frequency without re-running anything.
    """
    reasons: list[str] = []
    bands = bands_for(product.category)

    # -- Dimensions present -------------------------------------------------
    missing = [
        axis
        for axis, value in (
            ("width", product.width_mm),
            ("depth", product.depth_mm),
            ("height", product.height_mm),
        )
        if value is None
    ]
    if missing:
        return Verdict(
            Status.REJECTED_NO_DIMENSIONS,
            (f"no {', '.join(missing)}",),
        )

    width = product.width_mm
    depth = product.depth_mm
    height = product.height_mm
    assert width is not None and depth is not None and height is not None

    # -- Dimension plausibility ---------------------------------------------
    for axis, value, (low, high) in (
        ("width", width, bands.width_mm),
        ("depth", depth, bands.depth_mm),
        ("height", height, bands.height_mm),
    ):
        if not low <= value <= high:
            reasons.append(f"{axis} {value} mm outside {low}-{high} for {product.category}")
    if reasons:
        return Verdict(Status.REJECTED_IMPLAUSIBLE_DIMENSIONS, tuple(reasons))

    # -- Price present and plausible ----------------------------------------
    if product.price_cents is None or product.price_cents <= 0:
        return Verdict(Status.REJECTED_NO_PRICE, ("no price",))
    if product.currency != MARKET_CURRENCY:
        # 4.6 excludes non-USD in V1. Not a rejection of the product -- it is
        # a perfectly good sofa in the wrong market -- so it waits rather
        # than being marked wrong.
        return Verdict(
            Status.PENDING_REVIEW,
            (f"currency {product.currency} is outside V1's single market",),
        )
    low, high = bands.price_cents
    if not low <= product.price_cents <= high:
        return Verdict(
            Status.REJECTED_NO_PRICE,
            (f"price {product.price_cents} outside {low}-{high} for {product.category}",),
        )

    # -- URL safety ---------------------------------------------------------
    link = _check_link(product, allowed_hosts)
    if link is not None:
        return Verdict(Status.REJECTED_BAD_LINK, (link,))

    # -- Image present ------------------------------------------------------
    if not product.image_url:
        return Verdict(Status.REJECTED_NO_IMAGE, ("no image url",))
    if not product.image_content_type.startswith("image/"):
        return Verdict(
            Status.REJECTED_NO_IMAGE,
            (f"image url returned {product.image_content_type}",),
        )

    # -- Availability -------------------------------------------------------
    if product.availability == "out_of_stock":
        return Verdict(Status.UNAVAILABLE, ("out of stock",))

    # -- The two soft checks, which mean "look at this", not "throw it away" -
    soft: list[str] = []

    # 4.7: "Height not > 3x width for non-tall categories". This catches a
    # swapped axis, which is R7's headline example and which every bound
    # above can pass individually.
    if not bands.tall and height > width * MAX_HEIGHT_TO_WIDTH:
        soft.append(f"height {height} mm is more than 3x width {width} mm")

    # 4.7: "no W=D=H identical triples unless category allows (cube ottoman)".
    # Three identical numbers usually means the same figure copied into all
    # three fields rather than a cube.
    if not bands.allow_cube and width == depth == height:
        soft.append(f"width, depth and height are all {width} mm")

    if (
        product.category_confidence is not None
        and product.category_confidence < MIN_CATEGORY_CONFIDENCE
    ):
        soft.append(
            f"category confidence {product.category_confidence:.2f} below {MIN_CATEGORY_CONFIDENCE}"
        )

    if soft:
        return Verdict(Status.PENDING_REVIEW, tuple(soft))

    return Verdict(Status.ACTIVE)


def _check_link(product: Product, allowed_hosts: set[str] | None) -> str | None:
    """4.7: "Domain in retailer allowlist, https, resolves to 200 ... (no
    off-domain redirect)"."""
    if not product.canonical_url:
        return "no canonical url"

    parts = urlsplit(product.canonical_url)
    if parts.scheme != "https":
        return f"scheme {parts.scheme or 'missing'} is not https"
    host = parts.hostname
    if not host:
        return "no host"
    if allowed_hosts is not None and host.lower() not in {h.lower() for h in allowed_hosts}:
        # 9.4 treats the allowlist as the boundary of what we are permitted to
        # crawl at all, so a host outside it is not a link to keep.
        return f"host {host} is not in the retailer allowlist"
    if not product.url_resolves:
        return "url did not resolve"
    if product.url_redirected_off_domain:
        return "url redirected off the product domain"
    return None
