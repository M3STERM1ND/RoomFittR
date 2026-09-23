"""Parsing furniture dimensions out of retailer text (implementation-plan.md 4.5).

4.5 calls this "the Critical Field", and 9.3 agrees: R7 is "wrong dimensions
(package vs. assembled, swapped axes), stale prices" at **High** likelihood
and **Critical** impact. It is the highest-risk item in the catalog pipeline
and it earns that rating honestly -- a sofa listed at its carton size fits a
room it cannot fit, the solver places it, the validator agrees, and the user
finds out when it arrives.

So the design rule here is different from everywhere else in this repo:
**when in doubt, return nothing.** A `None` costs a Haiku call (4.5 step 3)
or a rejected product; a wrong answer costs a person a sofa. Every ambiguity
below -- two candidate sets that are equally good, a triple whose order
cannot be established, a number outside what furniture measures -- resolves
to `None` rather than to a best guess.

What this module does *not* do is decide the ordered-triple convention.
`213 x 91 x 84 cm` is W x D x H at one retailer and L x W x H at another, and
nothing in the string says which. 4.4 puts that in the per-retailer
`extraction_config`; passing the wrong one silently transposes depth and
height, so it is a required argument with no default.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

# 2.4: integer millimetres everywhere.
MM_PER_UNIT: dict[str, float] = {
    "mm": 1.0,
    "cm": 10.0,
    "m": 1000.0,
    "in": 25.4,
    "ft": 304.8,
}

# The same bounds the products table enforces, for the same reason (9.3 R7).
# A parser that returns a 2 mm wardrobe has not failed safely.
MIN_MM = 10
MAX_PLAN_MM = 5000  # width and depth
MAX_HEIGHT_MM = 3000

# 4.5: "Rugs: 8' x 10', 160 x 230 cm (2D; height defaults to 10 mm)".
RUG_HEIGHT_MM = 10

# 4.5: "Explicitly discard 'package', 'box', 'shipping', 'carton'." Matched on
# the label that introduces a dimension set, not on the whole page: a sofa
# whose description mentions the box it ships in still has real dimensions
# somewhere else on the page.
PACKAGED = re.compile(r"\b(pack(ag(e|ed|ing))?|box(ed)?|shipping|carton|crate|freight)\b", re.I)

# 4.5 step 2: "Multiple dimension sets (overall vs. seat vs. arm vs.
# packaged): pick overall/assembled."
OVERALL = re.compile(r"\b(overall|assembled|product|total|extended)\b", re.I)

# A set describing one part of the item rather than the item. Kept separate
# from PACKAGED because these are not wrong, merely not what we asked for.
COMPONENT = re.compile(
    r"\b(seat|arm|back|leg|cushion|headboard|footboard|shelf|drawer|"
    r"mattress|tabletop|top|base|clearance|interior|inside)\b",
    re.I,
)

_UNIT_WORDS = {
    "mm": "mm",
    "millimeter": "mm",
    "millimetre": "mm",
    "millimeters": "mm",
    "millimetres": "mm",
    "cm": "cm",
    "centimeter": "cm",
    "centimetre": "cm",
    "centimeters": "cm",
    "centimetres": "cm",
    "m": "m",
    "meter": "m",
    "metre": "m",
    "meters": "m",
    "metres": "m",
    "in": "in",
    "inch": "in",
    "inches": "in",
    '"': "in",
    "”": "in",
    "″": "in",
    "ft": "ft",
    "foot": "ft",
    "feet": "ft",
    "'": "ft",
    "’": "ft",
    "′": "ft",
}

_AXIS_WORDS = {
    "w": "w",
    "width": "w",
    "wide": "w",
    "d": "d",
    "depth": "d",
    "deep": "d",
    "l": "l",
    "length": "l",
    "long": "l",
    "h": "h",
    "height": "h",
    "high": "h",
    "tall": "h",
    "dia": "dia",
    "diam": "dia",
    "diameter": "dia",
    "round": "dia",
    "ø": "dia",
}


class Convention(StrEnum):
    """The per-retailer meaning of an unlabelled triple (4.4, 4.5)."""

    WDH = "wdh"
    LWH = "lwh"
    WHD = "whd"
    DWH = "dwh"


@dataclass(frozen=True, slots=True)
class Dimensions:
    """One resolved set, in integer millimetres (2.4)."""

    width_mm: int
    depth_mm: int
    height_mm: int
    # 4.5 step 4: provenance. `labelled` records whether the axes were named
    # in the text or inferred from the retailer's convention, which is the
    # single biggest driver of how much this answer should be trusted.
    labelled: bool
    source: str
    confidence: float

    @property
    def plausible(self) -> bool:
        return (
            MIN_MM <= self.width_mm <= MAX_PLAN_MM
            and MIN_MM <= self.depth_mm <= MAX_PLAN_MM
            and MIN_MM <= self.height_mm <= MAX_HEIGHT_MM
        )


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------
_VULGAR = {
    "¼": 0.25,
    "½": 0.5,
    "¾": 0.75,
    "⅐": 1 / 7,
    "⅑": 1 / 9,
    "⅒": 0.1,
    "⅓": 1 / 3,
    "⅔": 2 / 3,
    "⅕": 0.2,
    "⅖": 0.4,
    "⅗": 0.6,
    "⅘": 0.8,
    "⅙": 1 / 6,
    "⅚": 5 / 6,
    "⅛": 0.125,
    "⅜": 0.375,
    "⅝": 0.625,
    "⅞": 0.875,
}

# A number, optionally followed by a fraction: 33, 33.5, 213,5, 33 1/2, 33½.
_NUMBER = re.compile(
    r"""
    (?P<whole>\d+(?:[.,]\d+)?)          # 84 | 84.5 | 213,5
    (?:
        \s*(?P<fraction>\d+\s*/\s*\d+)  # 33 1/2
      | \s*(?P<vulgar>[¼½¾⅐⅑⅒⅓⅔⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞])
    )?
    """,
    re.X,
)


def _to_float(match: re.Match[str]) -> float | None:
    raw = match.group("whole")
    # A comma is a decimal point when it is followed by one or two digits and
    # nothing else: "213,5 cm". As a thousands separator it would make the
    # number at least 1000 of whatever unit, which is four metres of sofa.
    if "," in raw:
        head, _, tail = raw.partition(",")
        raw = f"{head}.{tail}" if len(tail) in (1, 2) else raw.replace(",", "")
    try:
        value = float(raw)
    except ValueError:
        return None

    if match.group("fraction"):
        numerator, _, denominator = match.group("fraction").partition("/")
        try:
            denom = float(denominator)
            if denom == 0:
                return None
            value += float(numerator) / denom
        except ValueError:
            return None
    elif match.group("vulgar"):
        value += _VULGAR[match.group("vulgar")]
    return value


# ---------------------------------------------------------------------------
# Measurements
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _Measure:
    """One number with, where the text said so, a unit and an axis."""

    value: float
    unit: str | None
    axis: str | None
    start: int
    end: int

    def mm(self, fallback_unit: str | None) -> int | None:
        unit = self.unit or fallback_unit
        if unit is None:
            return None
        return round(self.value * MM_PER_UNIT[unit])


# `7' 0"` and `7 ft 6 in`: feet and inches as one measurement. Matched before
# the general scan so the inches are not read as a second dimension --
# `7' 0" x 3'` is two dimensions, not three.
_FEET_INCHES = re.compile(
    r"(?P<feet>\d+)\s*(?:'|’|′|\bft\b|\bfeet\b|\bfoot\b)"
    r"\s*(?P<inches>\d+(?:[.,]\d+)?(?:\s*\d+\s*/\s*\d+)?)"
    r"\s*(?:\"|”|″|\bin\b|\binch(?:es)?\b)",
    re.I,
)

_AXIS_PREFIX = re.compile(
    # `\b` before the alternation cannot match `Ø`, which is not a word
    # character, so the diameter sign was in the word table and unreachable
    # from the pattern.
    r"(?:\b|(?<=[\s,(]))(?P<axis>width|depth|length|height|"
    r"dia(?:m(?:eter)?)?|ø|[wdlh])\s*[:.]?\s*$",
    re.I,
)
_AXIS_SUFFIX = re.compile(
    r"^\s*(?:\.|-)?\s*(?P<axis>width|depth|length|height|wide|deep|long|tall|high|"
    r"dia(?:m(?:eter)?)?|[wdlh])\b",
    re.I,
)
# Longest alternative first, and it matters: with `in` ahead of `inch`, the
# string `84 inchW` matched the unit as "in" and left "ch" sitting in front of
# the axis letter, so the W was never seen and the whole set went unlabelled.
_UNIT_SUFFIX = re.compile(
    r"^\s*(?P<unit>millimet(?:er|re)s?|centimet(?:er|re)s?|met(?:er|re)s?|"
    r"inch(?:es)?|feet|foot|mm|cm|ft|in|m|\"|”|″|'|’|′)\.?",
    re.I,
)


def _normalise(text: str) -> str:
    """NFKC, minus the fraction folding that would destroy `33½`.

    NFKC rewrites `½` to `1⁄2` and `″` to `"`, which is wanted for the quote
    marks and disastrous for the fractions -- `33½` would become `331⁄2` and
    read as 331. So the vulgar fractions are protected first.
    """
    protected = {ch: f"\x00{i}\x00" for i, ch in enumerate(_VULGAR)}
    for ch, token in protected.items():
        text = text.replace(ch, token)
    text = unicodedata.normalize("NFKC", text)
    for ch, token in protected.items():
        text = text.replace(token, ch)
    return text


def _scan(text: str) -> list[_Measure]:
    """Every measurement in one string, left to right."""
    measures: list[_Measure] = []
    consumed: list[tuple[int, int]] = []

    for match in _FEET_INCHES.finditer(text):
        feet = float(match.group("feet"))
        inch_match = _NUMBER.match(match.group("inches"))
        inches = _to_float(inch_match) if inch_match else None
        if inches is None:
            continue
        measures.append(
            _Measure(
                value=feet * 12 + inches,
                unit="in",
                axis=_axis_around(text, match.start(), match.end()),
                start=match.start(),
                end=match.end(),
            )
        )
        consumed.append((match.start(), match.end()))

    for match in _NUMBER.finditer(text):
        if any(start <= match.start() < end for start, end in consumed):
            continue
        value = _to_float(match)
        if value is None:
            continue
        rest = text[match.end() :]
        unit_match = _UNIT_SUFFIX.match(rest)
        unit = None
        end = match.end()
        if unit_match:
            unit = _UNIT_WORDS.get(unit_match.group("unit").lower().rstrip("."))
            end = match.end() + unit_match.end()
        measures.append(
            _Measure(
                value=value,
                unit=unit,
                axis=_axis_around(text, match.start(), end),
                start=match.start(),
                end=end,
            )
        )

    measures.sort(key=lambda m: m.start)
    return measures


def _axis_around(text: str, start: int, end: int) -> str | None:
    """The axis letter or word attached to a number, before or after it."""
    before = _AXIS_PREFIX.search(text[max(0, start - 24) : start])
    if before:
        return _AXIS_WORDS.get(before.group("axis").lower())
    after = _AXIS_SUFFIX.match(text[end : end + 24])
    if after:
        return _AXIS_WORDS.get(after.group("axis").lower())
    return None


# ---------------------------------------------------------------------------
# Resolving a set of measurements into W/D/H
# ---------------------------------------------------------------------------
_ORDER: dict[Convention, tuple[str, str, str]] = {
    Convention.WDH: ("w", "d", "h"),
    Convention.LWH: ("l", "w", "h"),
    Convention.WHD: ("w", "h", "d"),
    Convention.DWH: ("d", "w", "h"),
}


def _resolve(
    measures: list[_Measure], convention: Convention, *, is_rug: bool
) -> tuple[dict[str, int], bool] | None:
    """Turn measurements into millimetres per axis, or give up.

    Giving up is a real outcome and the common one on hard input: 4.5 step 3
    exists precisely to catch what this cannot resolve.
    """
    if not measures:
        return None

    # A unit stated once applies to the whole set: `213 x 91 x 84 cm`.
    units = [m.unit for m in measures if m.unit]
    if not units:
        return None
    # Mixed units are fine when each number carries its own (`84"W x 36"D`),
    # but a set where only some are marked takes the last one stated, which is
    # the trailing-unit convention.
    fallback = units[-1]

    labelled = [m for m in measures if m.axis]
    if len(labelled) >= 2 and len(labelled) == len(measures):
        return _resolve_labelled(measures, fallback, is_rug=is_rug)

    # Unlabelled: the retailer's convention decides, and only for a triple or
    # a rug's pair. Anything else is not a dimension set we understand.
    values: dict[str, int] = {}
    if len(measures) == 3:
        axes = _ORDER[convention]
    elif len(measures) == 2 and is_rug:
        axes = (_ORDER[convention][0], _ORDER[convention][1], "")
    else:
        return None

    for axis, measure in zip(axes, measures, strict=False):
        mm = measure.mm(fallback)
        if mm is None:
            return None
        values[axis] = mm

    if "l" in values:
        # 4.5: "ordered-triple convention per retailer config: W x D x H vs.
        # L x W x H". Length is the plan's longer side, which is width.
        values["w"], values["d"] = values.pop("l"), values.get("w", values.get("d", 0))
    if is_rug and "h" not in values:
        values["h"] = RUG_HEIGHT_MM
    if not {"w", "d", "h"} <= set(values):
        return None
    return values, False


def _resolve_labelled(
    measures: list[_Measure], fallback: str, *, is_rug: bool
) -> tuple[dict[str, int], bool] | None:
    values: dict[str, int] = {}
    for measure in measures:
        axis = measure.axis
        mm = measure.mm(fallback)
        if axis is None or mm is None:
            return None
        if axis == "dia":
            # 4.5: "Round items: diameter 36" -> width = depth."
            values["w"] = mm
            values["d"] = mm
            continue
        if axis == "l":
            # Length is the plan's other side: the width when nothing else
            # claimed that, the depth when a width was already stated.
            # `W 160 cm x L 230 cm` on a rug is 160 by 230, not a width given
            # twice and contradicting itself.
            axis = "d" if "w" in values else "w"
        if axis in values and values[axis] != mm:
            # The same axis twice with different numbers is exactly the
            # "several sets" case 4.5 hands to the LLM.
            return None
        values[axis] = mm

    if is_rug and "h" not in values and {"w", "d"} <= set(values):
        values["h"] = RUG_HEIGHT_MM
    if not {"w", "d", "h"} <= set(values):
        return None
    return values, True


# ---------------------------------------------------------------------------
# Candidate sets
# ---------------------------------------------------------------------------
# Splitting on "two or more spaces before a capital" was too eager: one line
# reading `W: 84"  D: 36"  H: 33"` became three sets of a single measurement
# each and resolved to nothing. A run of spaces only starts a new set when
# what follows actually names one.
_SET_LABEL = (
    r"overall|assembled|product|total|extended|seat|arm|back|leg|cushion|"
    r"headboard|footboard|shelf|drawer|mattress|tabletop|base|clearance|"
    r"interior|inside|pack(?:ag(?:e|ed|ing))?|box(?:ed)?|shipping|carton|"
    r"crate|freight|dimensions?|size"
)
_SET_SPLIT = re.compile(r"[;\n\r|]|\s{2,}(?=(?:" + _SET_LABEL + r")\b)", re.I)


def _candidates(text: str) -> list[str]:
    """Split a blob into the dimension sets it might contain.

    A spec table flattened to text runs several sets together: "Overall: 84W
    x 36D x 33H  Seat height: 19  Packaged: 90 x 40 x 38". Each has to be
    judged separately, because 4.5's whole instruction is to pick one of them
    and explicitly discard another.
    """
    parts = [part.strip() for part in _SET_SPLIT.split(text) if part and part.strip()]
    return parts or [text.strip()]


#: Returned by `_score` for a set that must never be used.
UNUSABLE = -100


def _score(part: str) -> int:
    """How much this looks like the set 4.5 asks for. Higher is better."""
    if PACKAGED.search(part):
        return UNUSABLE  # 4.5: "Explicitly discard".

    overall = OVERALL.search(part) is not None
    if COMPONENT.search(part) and not overall:
        # A seat, a tabletop or a drawer interior is not the product, and
        # using it as the product's size is R7 with the numbers read
        # correctly. Rejected rather than accepted at low confidence: the
        # cost is a Haiku call, and the alternative is a dining chair
        # recorded as 500 mm tall.
        return UNUSABLE

    return 10 if overall else 0


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------
def parse(
    text: str,
    *,
    convention: Convention,
    is_rug: bool = False,
) -> Dimensions | None:
    """Parse one dimension string or spec blob. `None` means "ask the LLM".

    `convention` has no default on purpose: it decides whether `213 x 91 x 84`
    is 91 deep or 91 high, nothing in the string says which, and a wrong
    guess is R7 happening silently.
    """
    if not text or not text.strip():
        return None

    normalised = _normalise(text)
    scored = [(_score(part), index, part) for index, part in enumerate(_candidates(normalised))]
    # Best first; ties keep document order, which puts "Overall" before
    # "Seat height" when neither is labelled.
    scored.sort(key=lambda item: (-item[0], item[1]))

    for score, _, part in scored:
        if score <= UNUSABLE:
            continue
        measures = _scan(part)
        resolved = _resolve(measures, convention, is_rug=is_rug)
        if resolved is None:
            continue
        values, labelled = resolved
        dims = Dimensions(
            width_mm=values["w"],
            depth_mm=values["d"],
            height_mm=values["h"],
            labelled=labelled,
            source=part.strip(),
            confidence=_confidence(labelled, score),
        )
        if not dims.plausible:
            # A number outside what furniture measures is not a near miss. It
            # is usually the wrong unit or the wrong set, and 9.3 wants it
            # rejected rather than corrected.
            continue
        return dims
    return None


def _confidence(labelled: bool, score: int) -> float:
    """4.5 step 4's `dimension_confidence`.

    Labelled axes are the thing that matters: an unlabelled triple is only as
    good as the retailer config, and that config is a human guess about a
    website that can change without telling us.
    """
    confidence = 0.9 if labelled else 0.6
    if score >= 10:
        confidence += 0.05
    if score < 0:
        confidence -= 0.2
    return round(min(confidence, 0.95), 2)
