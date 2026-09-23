"""The dimension parser against its corpus (implementation-plan.md 4.5, 9.1).

9.1 lists "Dimension parser corpus | Unit | Wrong dimensions are the worst
catalog failure (R7) | Every CI run". This is that gate.

The corpus lives in `fixtures/dimensions/corpus.json` and is built by
`fixtures/dimensions/_build.py`, which is also where its provenance is
written down: these are strings authored from 4.5's own examples, not the
300+ real ones 4.5 asks for. That distinction is the point of keeping the
corpus as data rather than as a list inside this file -- real strings get
appended to it, and the count in the progress doc goes up honestly.

The `None` cases matter at least as much as the parses. 4.5's rule is that an
ambiguous string goes to the LLM (step 3) rather than to a guess, so a parser
that confidently returns something for `84 x 36` has failed the test even
though it produced a number.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest
from roomfittr_catalog.dimensions import (
    MAX_HEIGHT_MM,
    MAX_PLAN_MM,
    MIN_MM,
    RUG_HEIGHT_MM,
    Convention,
    Dimensions,
    parse,
)

CORPUS_PATH = (
    pathlib.Path(__file__).resolve().parents[3] / "fixtures" / "dimensions" / "corpus.json"
)


def load_corpus() -> list[dict[str, Any]]:
    payload = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    cases: list[dict[str, Any]] = payload["cases"]
    return cases


CORPUS = load_corpus()


def ident(case: dict[str, Any]) -> str:
    return f"{case['text'][:44]!r}"


@pytest.mark.parametrize("case", CORPUS, ids=ident)
def test_corpus(case: dict[str, Any]) -> None:
    result = parse(
        case["text"],
        convention=Convention(case["convention"]),
        is_rug=bool(case.get("is_rug", False)),
    )

    if case["expect"] is None:
        assert result is None, (
            f"{case['text']!r} should not parse ({case['why']}), got "
            f"{result.width_mm}x{result.depth_mm}x{result.height_mm}"
            if result
            else ""
        )
        return

    assert result is not None, f"{case['text']!r} failed to parse ({case['why']})"
    assert (result.width_mm, result.depth_mm, result.height_mm) == (
        case["expect"]["width_mm"],
        case["expect"]["depth_mm"],
        case["expect"]["height_mm"],
    ), case["why"]

    if "labelled" in case:
        assert result.labelled is case["labelled"], case["why"]


class TestTheCorpusItself:
    def test_it_is_not_empty(self) -> None:
        assert len(CORPUS) > 50

    def test_it_has_both_outcomes(self) -> None:
        """A corpus of nothing but successes would not test 4.5's actual
        instruction, which is to refuse when unsure."""
        parses = [c for c in CORPUS if c["expect"] is not None]
        refusals = [c for c in CORPUS if c["expect"] is None]
        assert len(parses) >= 40
        assert len(refusals) >= 10

    def test_every_case_says_why(self) -> None:
        """A corpus entry with no reason is one nobody can judge when it
        starts failing."""
        for entry in CORPUS:
            assert entry["why"].strip(), entry["text"]

    def test_it_records_that_it_is_not_the_real_corpus(self) -> None:
        """4.5 asks for 300+ *real* strings. Until the crawler exists this is
        a stand-in, and the file has to keep saying so -- a count that looks
        like the requirement is exactly how the requirement gets forgotten."""
        payload = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
        assert "NOT the 300+ real strings" in payload["note"]


class TestUnits:
    @pytest.mark.parametrize(
        ("text", "expected_width"),
        [
            ('84"W x 36"D x 33"H', 2134),
            ("84 in W x 36 in D x 33 in H", 2134),
            ("213 cm W x 91 cm D x 84 cm H", 2130),
            ("2130 mm W x 910 mm D x 840 mm H", 2130),
            ("W 2.13 m D 0.91 m H 0.84 m", 2130),
        ],
    )
    def test_every_unit_converts_to_millimetres(self, text: str, expected_width: int) -> None:
        """2.4: integer millimetres everywhere, metres only at the glTF
        boundary."""
        result = parse(text, convention=Convention.WDH)
        assert result is not None, text
        assert result.width_mm == expected_width

    def test_a_trailing_unit_applies_to_the_whole_set(self) -> None:
        result = parse("213 x 91 x 84 cm", convention=Convention.WDH)
        assert result is not None
        assert (result.width_mm, result.depth_mm, result.height_mm) == (2130, 910, 840)


class TestConvention:
    def test_the_same_string_means_different_things(self) -> None:
        """The reason `convention` has no default: nothing in the string says
        whether 91 is the depth or the height, and a wrong guess transposes
        two axes silently."""
        text = "213 x 91 x 84 cm"
        wdh = parse(text, convention=Convention.WDH)
        whd = parse(text, convention=Convention.WHD)
        assert wdh is not None and whd is not None
        assert (wdh.depth_mm, wdh.height_mm) == (910, 840)
        assert (whd.depth_mm, whd.height_mm) == (840, 910)

    def test_labels_override_the_convention(self) -> None:
        """When the text names the axes, the retailer config is irrelevant --
        and must not be allowed to reorder what the page stated plainly."""
        text = 'W 84" H 33" D 36"'
        for convention in Convention:
            result = parse(text, convention=convention)
            assert result is not None, convention
            assert (result.width_mm, result.depth_mm, result.height_mm) == (
                2134,
                914,
                838,
            ), convention


class TestRefusing:
    """4.5's real instruction: when unsure, hand it to the LLM."""

    @pytest.mark.parametrize(
        "text",
        [
            "84 x 36",
            "213 x 91 x 84",
            '84" wide',
            "Please see the product page",
            "Dimensions vary by configuration",
            'W 84" W 92" D 36" H 33"',
        ],
    )
    def test_it_returns_none_rather_than_guessing(self, text: str) -> None:
        assert parse(text, convention=Convention.WDH) is None

    def test_a_packaged_only_string_is_refused(self) -> None:
        assert parse("Carton: 220 x 95 x 90 cm", convention=Convention.WDH) is None

    def test_implausible_numbers_are_refused(self) -> None:
        """9.3's R7 is rated Critical. A parser that returns a 20 mm sofa
        because the arithmetic worked has not failed safely."""
        assert parse("W 0.4 cm D 0.3 cm H 0.2 cm", convention=Convention.WDH) is None
        assert parse("W 84 m D 36 m H 33 m", convention=Convention.WDH) is None


class TestPlausibility:
    def test_the_bounds_match_the_database(self) -> None:
        """`products_dimensions_plausible` in
        supabase/migrations/..._catalog.sql states the same numbers. If they
        drift, the parser hands the catalog rows the database will reject,
        and the failure surfaces as a crawl error rather than as a bad
        measurement."""
        assert (MIN_MM, MAX_PLAN_MM, MAX_HEIGHT_MM) == (10, 5000, 3000)

    def test_everything_the_parser_returns_is_plausible(self) -> None:
        for entry in CORPUS:
            if entry["expect"] is None:
                continue
            result = parse(
                entry["text"],
                convention=Convention(entry["convention"]),
                is_rug=bool(entry.get("is_rug", False)),
            )
            assert result is not None
            assert result.plausible, entry["text"]


class TestRugs:
    def test_a_two_dimensional_rug_gets_a_default_height(self) -> None:
        result = parse("160 x 230 cm", convention=Convention.WDH, is_rug=True)
        assert result is not None
        assert result.height_mm == RUG_HEIGHT_MM

    def test_a_rug_with_a_stated_thickness_keeps_it(self) -> None:
        result = parse("W 160 cm D 230 cm H 2 cm", convention=Convention.WDH, is_rug=True)
        assert result is not None
        assert result.height_mm == 20

    def test_the_same_string_is_refused_for_a_sofa(self) -> None:
        assert parse("160 x 230 cm", convention=Convention.WDH, is_rug=False) is None


class TestProvenance:
    """4.5 step 4: `dimension_source` and `dimension_confidence`."""

    def test_a_labelled_parse_is_trusted_more_than_a_triple(self) -> None:
        labelled = parse('84"W x 36"D x 33"H', convention=Convention.WDH)
        triple = parse('84 x 36 x 33"', convention=Convention.WDH)
        assert labelled is not None and triple is not None
        assert labelled.labelled and not triple.labelled
        assert labelled.confidence > triple.confidence

    def test_overall_raises_confidence(self) -> None:
        plain = parse('84"W x 36"D x 33"H', convention=Convention.WDH)
        overall = parse('Overall: 84"W x 36"D x 33"H', convention=Convention.WDH)
        assert plain is not None and overall is not None
        assert overall.confidence >= plain.confidence

    def test_confidence_is_never_certainty(self) -> None:
        """Nothing extracted from a web page is worth 1.0, and a downstream
        check that treats it as such stops being a check."""
        for entry in CORPUS:
            if entry["expect"] is None:
                continue
            result = parse(
                entry["text"],
                convention=Convention(entry["convention"]),
                is_rug=bool(entry.get("is_rug", False)),
            )
            assert result is not None
            assert 0.0 < result.confidence <= 0.95

    def test_the_matched_text_is_kept(self) -> None:
        """`dimension_raw` in 6.3 is "kept for audit". When R7 bites, this is
        the only record of what the page actually said."""
        result = parse('Overall: 84"W x 36"D x 33"H; Seat height: 19"', convention=Convention.WDH)
        assert result is not None
        assert "84" in result.source
        assert "Seat" not in result.source


def test_dimensions_is_hashable_and_frozen() -> None:
    """It ends up in dicts and sets while candidates are compared."""
    dims = Dimensions(
        width_mm=1, depth_mm=2, height_mm=3, labelled=True, source="x", confidence=0.9
    )
    assert hash(dims)
    with pytest.raises(AttributeError):
        dims.width_mm = 5  # type: ignore[misc]
