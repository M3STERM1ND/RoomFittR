"""4.7's acceptance gate (implementation-plan.md 4.7, 9.3 R7).

The table in 4.7 is precise about which failure produces which status, and
those statuses are not interchangeable: 9.3's R7 mitigation includes a
"rising reject rate for a retailer" alarm, and 4.7's admin page sorts by
"frequency of failure reason". Both are useless if a bad price and a bad
link arrive as the same value, so every row of the table gets a test naming
the status it must produce.

The rejected-versus-pending distinction gets its own class, because it is
the one a reader is most likely to get wrong: a rejection says the product
is bad, a `pending_review` says *we* might be.
"""

from __future__ import annotations

import pytest
from roomfittr_catalog.validate import (
    MARKET_CURRENCY,
    MAX_HEIGHT_TO_WIDTH,
    MIN_CATEGORY_CONFIDENCE,
    Product,
    Status,
    bands_for,
    categories_with_bands,
    validate,
)

ALLOWED = {"example-retailer.com"}


def product(**overrides: object) -> Product:
    """A sofa that passes every check, so each test changes exactly one thing."""
    base = {
        "category": "sofa",
        "width_mm": 2130,
        "depth_mm": 910,
        "height_mm": 840,
        "price_cents": 129900,
        "currency": MARKET_CURRENCY,
        "canonical_url": "https://example-retailer.com/p/sofa-1",
        "image_url": "https://example-retailer.com/i/sofa-1.jpg",
        "category_confidence": 0.95,
        "availability": "in_stock",
    }
    base.update(overrides)
    return Product(**base)  # type: ignore[arg-type]


def check(**overrides: object) -> Status:
    return validate(product(**overrides), allowed_hosts=ALLOWED).status


class TestTheHappyPath:
    def test_a_good_product_becomes_active(self) -> None:
        verdict = validate(product(), allowed_hosts=ALLOWED)
        assert verdict.status is Status.ACTIVE
        assert verdict.accepted
        assert verdict.reasons == ()

    def test_no_allowlist_means_the_host_is_not_checked(self) -> None:
        """A caller that has not loaded the retailer yet should still be able
        to run the rest of the table."""
        assert validate(product(), allowed_hosts=None).status is Status.ACTIVE


class TestDimensions:
    @pytest.mark.parametrize("axis", ["width_mm", "depth_mm", "height_mm"])
    def test_a_missing_axis_is_rejected_for_that_reason(self, axis: str) -> None:
        assert check(**{axis: None}) is Status.REJECTED_NO_DIMENSIONS

    def test_the_reason_names_the_axis(self) -> None:
        """4.7's admin page sorts by reason; "no dimensions" on every row
        would tell nobody which part of the parser to fix."""
        verdict = validate(product(depth_mm=None), allowed_hosts=ALLOWED)
        assert "depth" in verdict.reasons[0]

    def test_a_carton_sized_sofa_is_rejected(self) -> None:
        """R7's headline: a listing that gives the packed size. 2.4 m deep is
        outside the band even though every number is individually a number."""
        assert check(depth_mm=2400) is Status.REJECTED_IMPLAUSIBLE_DIMENSIONS

    def test_inches_read_as_centimetres_is_rejected(self) -> None:
        """84 x 36 x 33, unconverted, is a doll's sofa."""
        assert (
            check(width_mm=84, depth_mm=36, height_mm=33) is Status.REJECTED_IMPLAUSIBLE_DIMENSIONS
        )

    def test_every_failing_axis_is_reported(self) -> None:
        verdict = validate(product(width_mm=10, depth_mm=10, height_mm=10), allowed_hosts=ALLOWED)
        assert verdict.status is Status.REJECTED_IMPLAUSIBLE_DIMENSIONS
        assert len(verdict.reasons) == 3

    def test_the_bands_are_the_ones_4_7_wrote_down(self) -> None:
        """4.7 gives two worked examples. If either drifts, the file has
        stopped implementing the plan and started implementing itself."""
        sofa = bands_for("sofa")
        assert sofa.width_mm == (1300, 4000)
        assert sofa.depth_mm == (700, 1200)
        assert sofa.height_mm == (600, 1100)
        assert bands_for("coffee_table").height_mm == (300, 600)

    def test_an_unknown_category_still_gets_judged(self) -> None:
        """4.2's vocabulary can grow ahead of this file. Degrading to
        generous bounds is right; degrading to no bounds would accept a
        four-metre nightstand."""
        assert check(category="wine_rack") is Status.ACTIVE
        assert (
            check(category="wine_rack", width_mm=40_000) is Status.REJECTED_IMPLAUSIBLE_DIMENSIONS
        )


class TestPrice:
    def test_a_missing_price_is_rejected(self) -> None:
        assert check(price_cents=None) is Status.REJECTED_NO_PRICE

    @pytest.mark.parametrize("price", [0, -100])
    def test_a_nonpositive_price_is_rejected(self, price: int) -> None:
        assert check(price_cents=price) is Status.REJECTED_NO_PRICE

    def test_a_price_outside_the_category_band_is_rejected(self) -> None:
        """4.7: "within category band ($10-$15,000)". A $12 sofa is a parts
        listing or a scraping error, not a sofa."""
        assert check(price_cents=1200) is Status.REJECTED_NO_PRICE
        assert check(price_cents=9_000_000) is Status.REJECTED_NO_PRICE

    def test_a_non_usd_product_waits_rather_than_being_rejected(self) -> None:
        """4.6: "V1 is single-market (USD); non-USD products are excluded".
        Excluded, not wrong -- it is a perfectly good sofa in the wrong
        market, and marking it rejected would lose it when V1 stops being
        single-market."""
        verdict = validate(product(currency="EUR"), allowed_hosts=ALLOWED)
        assert verdict.status is Status.PENDING_REVIEW
        assert "EUR" in verdict.reasons[0]


class TestLinkSafety:
    def test_http_is_rejected(self) -> None:
        assert check(canonical_url="http://example-retailer.com/p/1") is Status.REJECTED_BAD_LINK

    def test_a_host_outside_the_allowlist_is_rejected(self) -> None:
        """9.4 makes the allowlist the boundary of what we may crawl at all,
        so a link outside it is not one to keep."""
        assert check(canonical_url="https://somewhere-else.com/p/1") is Status.REJECTED_BAD_LINK

    def test_an_off_domain_redirect_is_rejected(self) -> None:
        """The affiliate-parking failure: the link still resolves, and it no
        longer goes to the product."""
        assert check(url_redirected_off_domain=True) is Status.REJECTED_BAD_LINK

    def test_a_dead_link_is_rejected(self) -> None:
        assert check(url_resolves=False) is Status.REJECTED_BAD_LINK

    def test_a_missing_url_is_rejected(self) -> None:
        assert check(canonical_url="") is Status.REJECTED_BAD_LINK

    def test_the_host_check_is_case_insensitive(self) -> None:
        assert check(canonical_url="https://Example-Retailer.COM/p/1") is Status.ACTIVE


class TestImage:
    def test_a_missing_image_is_rejected(self) -> None:
        assert check(image_url="") is Status.REJECTED_NO_IMAGE

    def test_a_non_image_response_is_rejected(self) -> None:
        """4.7: ">=1 image URL that returns image/*". A URL that returns an
        HTML error page is the common case and looks fine in the database."""
        assert check(image_content_type="text/html") is Status.REJECTED_NO_IMAGE


class TestAvailability:
    def test_out_of_stock_is_unavailable_not_rejected(self) -> None:
        """It will come back. 6.3 keeps the row so the price history and any
        layout referencing it survive."""
        assert check(availability="out_of_stock") is Status.UNAVAILABLE

    def test_preorder_is_fine(self) -> None:
        assert check(availability="preorder") is Status.ACTIVE


class TestRejectedVersusPending:
    """4.7 routes `pending_review` to an admin page whose purpose is to "fix
    parser, re-run". The difference is whether the product is bad or we are.
    """

    def test_a_swapped_axis_is_reviewed_not_rejected(self) -> None:
        """4.7: "Height not > 3x width for non-tall categories" is a
        `pending_review`, and rightly: every individual number here is inside
        its band, which is exactly what a transposition looks like."""
        # Every axis is inside the nightstand band (250-800, 250-600,
        # 350-900) and 800 is more than 3x 250. That is the point: a
        # transposition passes each bound individually, which is why 4.7
        # gives the ratio its own row.
        verdict = validate(
            product(category="nightstand", width_mm=250, depth_mm=400, height_mm=800),
            allowed_hosts=ALLOWED,
        )
        assert verdict.status is Status.PENDING_REVIEW
        assert "3x width" in verdict.reasons[0]

    def test_a_tall_category_is_exempt(self) -> None:
        """A bookshelf is the shape that rule would otherwise reject."""
        assert (
            check(category="bookshelf", width_mm=800, depth_mm=320, height_mm=2000) is Status.ACTIVE
        )

    def test_identical_triples_are_reviewed(self) -> None:
        """4.7: "no W=D=H identical triples unless category allows". Three
        identical numbers is usually one figure copied into three fields."""
        verdict = validate(
            product(category="side_table", width_mm=500, depth_mm=500, height_mm=500),
            allowed_hosts=ALLOWED,
        )
        assert verdict.status is Status.PENDING_REVIEW
        assert "500" in verdict.reasons[0]

    def test_a_cube_ottoman_is_allowed_to_be_a_cube(self) -> None:
        assert check(category="ottoman", width_mm=450, depth_mm=450, height_mm=450) is Status.ACTIVE

    def test_a_low_confidence_category_is_reviewed(self) -> None:
        """4.7: "In closed vocabulary with confidence >= 0.7"."""
        assert check(category_confidence=0.5) is Status.PENDING_REVIEW
        assert check(category_confidence=MIN_CATEGORY_CONFIDENCE) is Status.ACTIVE

    def test_an_unknown_confidence_is_not_held_against_it(self) -> None:
        """A product whose category came from the breadcrumb mapping has no
        classifier score, and 4.6 puts that path first."""
        assert check(category_confidence=None) is Status.ACTIVE

    def test_every_soft_failure_is_reported_together(self) -> None:
        """The admin page sorts by reason, so a row failing two checks should
        appear under both rather than under whichever was noticed first."""
        verdict = validate(
            product(
                category="side_table",
                width_mm=500,
                depth_mm=500,
                height_mm=500,
                category_confidence=0.4,
            ),
            allowed_hosts=ALLOWED,
        )
        assert verdict.status is Status.PENDING_REVIEW
        assert len(verdict.reasons) == 2


class TestOrdering:
    def test_a_hard_failure_beats_a_soft_one(self) -> None:
        """A product with no price and a suspicious shape is rejected for the
        price. Reporting the shape instead would send a hopeless row to the
        review queue."""
        assert (
            check(
                category="side_table", price_cents=None, width_mm=500, depth_mm=500, height_mm=500
            )
            is Status.REJECTED_NO_PRICE
        )

    def test_missing_dimensions_are_not_also_judged_on_their_bands(self) -> None:
        verdict = validate(product(width_mm=None), allowed_hosts=ALLOWED)
        assert verdict.status is Status.REJECTED_NO_DIMENSIONS
        assert len(verdict.reasons) == 1


class TestTheRulesFile:
    def test_every_category_is_in_the_closed_vocabulary(self) -> None:
        """A category here that the layout engine has never heard of would
        accept products nothing can ever place. The two files are pinned
        together rather than merely intended to agree."""
        from roomfittr_layout.rules import CATEGORY_VOCABULARY

        unknown = categories_with_bands() - set(CATEGORY_VOCABULARY)
        assert not unknown, f"not in 4.2's vocabulary: {sorted(unknown)}"

    def test_every_vocabulary_category_has_bands(self) -> None:
        """The defaults exist so nothing crashes, not so this file can be
        incomplete. 4.7's plausibility check is per-category and a category
        falling back to the generous defaults is not really being checked."""
        from roomfittr_layout.rules import CATEGORY_VOCABULARY

        missing = set(CATEGORY_VOCABULARY) - categories_with_bands()
        assert not missing, f"no bands for: {sorted(missing)}"

    def test_every_band_is_ordered_and_positive(self) -> None:
        from roomfittr_layout.rules import CATEGORY_VOCABULARY

        for category in sorted(CATEGORY_VOCABULARY):
            bands = bands_for(category)
            for name in ("width_mm", "depth_mm", "height_mm", "price_cents"):
                low, high = getattr(bands, name)
                assert 0 < low < high, f"{category}.{name} = {low}-{high}"

    def test_a_rugs_height_band_admits_the_parsers_default(self) -> None:
        """4.5 defaults a rug to 10 mm tall. If this band excluded it, every
        rug the parser handled correctly would be rejected here."""
        from roomfittr_catalog.dimensions import RUG_HEIGHT_MM

        low, high = bands_for("rug").height_mm
        assert low <= RUG_HEIGHT_MM <= high

    def test_the_statuses_match_the_database_enum(self) -> None:
        """`public.product_status` in the catalog migration. A status this
        module produces that the enum does not hold is a crawl that fails at
        the insert, after the work."""
        import pathlib
        import re

        migration = (
            pathlib.Path(__file__).resolve().parents[3]
            / "supabase"
            / "migrations"
            / "20260922000100_extensions_and_helpers.sql"
        )
        body = migration.read_text(encoding="utf-8")
        block = re.search(
            r"create type public\.product_status as enum \((?P<values>.*?)\);",
            body,
            re.S,
        )
        assert block, "product_status enum not found"
        in_db = set(re.findall(r"'([a-z_]+)'", block.group("values")))
        assert {s.value for s in Status} <= in_db, {s.value for s in Status} - in_db


def test_max_height_ratio_is_the_plans() -> None:
    """4.7: "Height not > 3x width for non-tall categories"."""
    assert MAX_HEIGHT_TO_WIDTH == 3.0
