"""Build the dimension-parsing corpus (implementation-plan.md 4.5).

4.5 asks for the parser to be "unit-tested against a corpus of **300+ real
strings collected in Phase 3**". This builds a corpus of the same shape from
the formats 4.5 enumerates, crossed with the units and spellings retailers
actually use.

**It is not that corpus, and it must not be mistaken for it.** These strings
are written from the specification, so they cover what we already know to
handle and by construction cover nothing else. The value of 300 *real*
strings is precisely the formats nobody thought of -- the retailer who writes
`Dims: 84x36x33`, the one who puts the height first without saying so, the
one whose spec table has two "Width" rows. Collecting those needs the crawler,
which needs D7-D9.

So this file is the floor, not the ceiling. When real strings arrive, append
them to `corpus.json` with their expected result and delete nothing: a
generated case that a real string contradicts is a case that was wrong.

Regenerate with:

    uv run python fixtures/dimensions/_build.py
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "corpus.json"

# (label, mm per unit, how retailers spell it)
UNITS: list[tuple[str, float, list[str]]] = [
    ("in", 25.4, ['"', " in", " in.", '" ', " inch", " inches", "”"]),
    ("cm", 10.0, [" cm", "cm", " cm.", " centimetres", " centimeters"]),
    ("mm", 1.0, [" mm", "mm"]),
]


def mm(value: float, per_unit: float) -> int:
    return round(value * per_unit)


def case(
    text: str,
    expect: dict[str, int] | None,
    why: str,
    *,
    convention: str = "wdh",
    is_rug: bool = False,
    labelled: bool | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "text": text,
        "convention": convention,
        "expect": expect,
        "why": why,
    }
    if is_rug:
        entry["is_rug"] = True
    if labelled is not None:
        entry["labelled"] = labelled
    return entry


def build() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []

    # -- Labelled, every unit spelling x every layout 4.5 lists -------------
    # 84 x 36 x 33 of whatever unit, so the expected answer is arithmetic
    # rather than a number copied by hand into a test.
    w, d, h = 84, 36, 33
    layouts = [
        ("{w}{u}W x {d}{u}D x {h}{u}H", True),
        ("W: {w}{u} D: {d}{u} H: {h}{u}", True),
        ("{w}{u} W x {d}{u} D x {h}{u} H", True),
        ("Width {w}{u} Depth {d}{u} Height {h}{u}", True),
        ("W{w}{u} D{d}{u} H{h}{u}", True),
        ("{w}{u} wide x {d}{u} deep x {h}{u} high", True),
        ("Width: {w}{u}, Depth: {d}{u}, Height: {h}{u}", True),
    ]
    for unit, per, spellings in UNITS:
        for spelling in spellings:
            for layout, is_labelled in layouts:
                text = layout.format(w=w, d=d, h=h, u=spelling)
                cases.append(
                    case(
                        text,
                        {
                            "width_mm": mm(w, per),
                            "depth_mm": mm(d, per),
                            "height_mm": mm(h, per),
                        },
                        f"labelled {unit}, layout {layout!r}",
                        labelled=is_labelled,
                    )
                )

    # -- Ordered triples, per retailer convention (4.5 step 2) -------------
    for unit, per, spellings in UNITS:
        spelling = spellings[0]
        triple = f"213 x 91 x 84{spelling}"
        for convention, (a, b, c) in {
            "wdh": ("width_mm", "depth_mm", "height_mm"),
            "lwh": ("width_mm", "depth_mm", "height_mm"),
            "whd": ("width_mm", "height_mm", "depth_mm"),
            "dwh": ("depth_mm", "width_mm", "height_mm"),
        }.items():
            values = [mm(213, per), mm(91, per), mm(84, per)]
            expect = {a: values[0], b: values[1], c: values[2]}
            if all(_within(expect)):
                cases.append(
                    case(
                        triple,
                        expect,
                        f"unlabelled triple read as {convention}",
                        convention=convention,
                        labelled=False,
                    )
                )
        # 213 inches is 5.4 m, past the plausibility bound, so the sizes here
        # are chosen per unit. The case is about the separator, not the size.
        a, b, c = (84, 36, 33) if unit == "in" else (213, 91, 84)
        cases.append(
            case(
                f"{a} × {b} × {c}{spelling}",
                {
                    "width_mm": mm(a, per),
                    "depth_mm": mm(b, per),
                    "height_mm": mm(c, per),
                },
                "multiplication sign instead of the letter x",
                labelled=False,
            )
        )

    # -- Fractions and decimals (4.5 step 2) -------------------------------
    cases += [
        case(
            '33 1/2"W x 20"D x 30"H',
            {"width_mm": 851, "depth_mm": 508, "height_mm": 762},
            "ascii fraction",
            labelled=True,
        ),
        case(
            '33½" W x 20" D x 30" H',
            {"width_mm": 851, "depth_mm": 508, "height_mm": 762},
            "vulgar fraction, which NFKC would otherwise turn into 331/2",
            labelled=True,
        ),
        case(
            '33¼" W x 20" D x 30" H',
            {"width_mm": 845, "depth_mm": 508, "height_mm": 762},
            "quarter fraction",
            labelled=True,
        ),
        case(
            '33¾" W x 20" D x 30" H',
            {"width_mm": 857, "depth_mm": 508, "height_mm": 762},
            "three-quarter fraction",
            labelled=True,
        ),
        case(
            "213,5 x 91 x 84 cm",
            {"width_mm": 2135, "depth_mm": 910, "height_mm": 840},
            "european decimal comma",
            labelled=False,
        ),
        case(
            "213.5 x 91 x 84 cm",
            {"width_mm": 2135, "depth_mm": 910, "height_mm": 840},
            "decimal point",
            labelled=False,
        ),
        case(
            "W 213,5 cm D 91,5 cm H 84,5 cm",
            {"width_mm": 2135, "depth_mm": 915, "height_mm": 845},
            "decimal comma on every axis",
            labelled=True,
        ),
    ]

    # -- Feet and inches (4.5 step 2) --------------------------------------
    cases += [
        case(
            "7' 0\" x 3' 0\" x 2' 9\"",
            {"width_mm": 2134, "depth_mm": 914, "height_mm": 838},
            "feet and inches; the inches must not read as a fourth dimension",
            labelled=False,
        ),
        case(
            "7 ft 6 in x 3 ft 0 in x 2 ft 6 in",
            {"width_mm": 2286, "depth_mm": 914, "height_mm": 762},
            "feet and inches spelled out",
            labelled=False,
        ),
        case(
            "W 7' 0\" D 3' 0\" H 2' 6\"",
            {"width_mm": 2134, "depth_mm": 914, "height_mm": 762},
            "labelled feet and inches",
            labelled=True,
        ),
    ]

    # -- Rugs: 2D, height defaults (4.5 step 2) ----------------------------
    cases += [
        case(
            "8' x 10'",
            {"width_mm": 2438, "depth_mm": 3048, "height_mm": 10},
            "rug in feet; height defaults to 10 mm",
            is_rug=True,
            labelled=False,
        ),
        case(
            "160 x 230 cm",
            {"width_mm": 1600, "depth_mm": 2300, "height_mm": 10},
            "rug in centimetres",
            is_rug=True,
            labelled=False,
        ),
        case(
            "W 160 cm x L 230 cm",
            {"width_mm": 1600, "depth_mm": 2300, "height_mm": 10},
            "rug with width and length labelled",
            is_rug=True,
            labelled=True,
        ),
        case(
            "160 x 230 cm",
            None,
            "the same string is not a dimension set for a non-rug: only two numbers",
        ),
    ]

    # -- Round items (4.5 step 2) ------------------------------------------
    cases += [
        case(
            'Diameter 36" Height 29"',
            {"width_mm": 914, "depth_mm": 914, "height_mm": 737},
            "diameter sets width and depth alike",
            labelled=True,
        ),
        case(
            "Ø 120 cm, H 75 cm",
            {"width_mm": 1200, "depth_mm": 1200, "height_mm": 750},
            "diameter sign",
            labelled=True,
        ),
        case(
            "Dia. 90 cm H 45 cm",
            {"width_mm": 900, "depth_mm": 900, "height_mm": 450},
            "abbreviated diameter",
            labelled=True,
        ),
    ]

    # -- Several sets: pick overall, discard packaged (4.5 step 2) ---------
    cases += [
        case(
            'Overall: 84"W x 36"D x 33"H; Seat height: 19"',
            {"width_mm": 2134, "depth_mm": 914, "height_mm": 838},
            "overall wins over a component measurement",
            labelled=True,
        ),
        case(
            "Packaged: 90 x 40 x 38 in; Overall: 84 x 36 x 33 in",
            {"width_mm": 2134, "depth_mm": 914, "height_mm": 838},
            "packaged is discarded even though it comes first",
            labelled=False,
        ),
        case(
            "Assembled: 213 x 91 x 84 cm | Carton: 220 x 95 x 90 cm",
            {"width_mm": 2130, "depth_mm": 910, "height_mm": 840},
            "assembled wins over carton",
            labelled=False,
        ),
        case(
            "Shipping dimensions: 90 x 40 x 38 in",
            None,
            "the only set on offer is the one 4.5 says to discard",
        ),
        case(
            "Box size: 220 x 95 x 90 cm",
            None,
            "box is discarded; nothing else to fall back to",
        ),
        case(
            'Seat: 20"W x 20"D x 19"H',
            None,
            "a seat is not the product, and using it would be R7 with the numbers read correctly",
        ),
        case(
            "Tabletop: 120 x 60 x 4 cm",
            None,
            "the top is not the table",
        ),
        case(
            'Overall width 84" Overall depth 36" Overall height 33"',
            {"width_mm": 2134, "depth_mm": 914, "height_mm": 838},
            "overall repeated on each axis",
            labelled=True,
        ),
    ]

    # -- Things that must not parse ----------------------------------------
    cases += [
        case('84" wide', None, "one dimension is not a set"),
        case("Approximately 84 inches", None, "one dimension, in prose"),
        case("84 x 36", None, "two numbers and not a rug"),
        case("213 x 91 x 84", None, "no unit anywhere, so the numbers mean nothing"),
        case("", None, "empty"),
        case("   ", None, "blank"),
        case("Some assembly required", None, "no numbers at all"),
        case("Weight: 45 kg", None, "a measurement, but not of length"),
        case(
            "W 0.4 cm D 0.3 cm H 0.2 cm",
            None,
            "below the 10 mm floor the products table also enforces; rejected "
            "rather than recorded as a 4 mm sofa",
        ),
        case(
            "W 84 m D 36 m H 33 m",
            None,
            "metres where inches were meant: implausible, so rejected",
        ),
        case(
            'W 84" W 92" D 36" H 33"',
            None,
            "two different widths is the 'several sets' case 4.5 hands to the LLM",
        ),
        case(
            "1,213 x 91 x 84 cm",
            None,
            "a comma with three digits after it is a thousands separator, and "
            "12 metres of sofa is implausible",
        ),
    ]

    return cases


def _within(expect: dict[str, int]) -> list[bool]:
    return [
        10 <= expect["width_mm"] <= 5000,
        10 <= expect["depth_mm"] <= 5000,
        10 <= expect["height_mm"] <= 3000,
    ]


def main() -> None:
    cases = build()
    payload = {
        "note": (
            "Built by fixtures/dimensions/_build.py from the formats "
            "implementation-plan.md 4.5 enumerates. NOT the 300+ real strings "
            "4.5 asks for -- those need the crawler, which needs D7-D9. "
            "Append real strings here; delete nothing."
        ),
        "count": len(cases),
        "cases": cases,
    }
    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"wrote {OUT} with {len(cases)} cases")


if __name__ == "__main__":
    main()
