"""The Phase 0 definition of done, as a test.

> "eval harness runs end to end on a dummy result"

That was originally proved by running the harness once by hand and looking at
the output. This runs it on every CI build instead, against the committed
`eval/runs/dummy` fixture, so the claim keeps being true after the metrics are
edited in Phase 1.

The dummy run is three hand-built captures of the example room: two careful
(~1-2% scale error) and one sloppy (9% error, missed window). The numbers below
assert on the *shape* of the output and on the two gate outcomes that the
fixture was built to produce, not on every decimal -- a metric can be refined
without rewriting this file, but it cannot silently stop reporting.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from eval.harness import load_ground_truth, run, write_csv, write_markdown

EVAL_DIR = Path(__file__).resolve().parents[1]
DUMMY = EVAL_DIR / "runs" / "dummy"

# The dummy fixture only ever covers the example room. Tier A scenes land in
# ground_truth/ as they are converted, and every one of them is legitimately
# "missing" from a run that never claimed to predict it -- so these tests ask
# what the harness *should* have found rather than hard-coding a count that
# goes stale on the next scene.
DUMMY_CAPTURES = {"gt-000-a", "gt-000-b", "gt-000-c"}


def all_capture_ids() -> set[str]:
    return {
        c["capture_id"] for doc in load_ground_truth().values() for c in doc.get("captures", [])
    }


def test_dummy_run_is_committed() -> None:
    """A fixture that only exists on one laptop proves nothing."""
    assert DUMMY.is_dir(), f"missing dummy run fixture at {DUMMY}"
    captures = sorted(p.stem for p in DUMMY.glob("*.json") if p.stem != "meta")
    assert captures == ["gt-000-a", "gt-000-b", "gt-000-c"]


def test_every_capture_is_matched_and_scored() -> None:
    summary = run(DUMMY, calibrated=False)
    assert len(summary.scores) == 3
    # An unmatched prediction means the harness quietly scored the wrong set.
    assert summary.unmatched == []
    # None of the dummy's own captures may go unscored; anything else in
    # ground truth is expected to be missing from a gt-000-only run.
    assert not (DUMMY_CAPTURES & set(summary.missing))
    assert set(summary.missing) == all_capture_ids() - DUMMY_CAPTURES


def test_gates_are_evaluated_not_skipped() -> None:
    """Every gate must reach a verdict. `None` means "no data", which on a
    complete run is a harness bug rather than a model result."""
    summary = run(DUMMY, calibrated=False)
    experiments = [g.experiment for g in summary.gates]
    assert "E2" in experiments
    assert "E4" in experiments
    assert experiments.count("E5") == 2  # doors and windows scored separately
    assert all(g.passed is not None for g in summary.gates)


def test_the_sloppy_capture_actually_fails_something() -> None:
    """The fixture is built so the run is not all-green.

    A harness that passes everything is indistinguishable from one that checks
    nothing, so the dummy deliberately contains a capture that misses a window
    and is 9% out on scale.
    """
    summary = run(DUMMY, calibrated=False)
    assert any(not g.passed for g in summary.gates), "dummy run should not pass every gate"

    sloppy = next(s for s in summary.scores if s.capture_id == "gt-000-c")
    careful = next(s for s in summary.scores if s.capture_id == "gt-000-a")
    assert sloppy.as_row()["wall_err_median_pct"] > careful.as_row()["wall_err_median_pct"]


def test_calibrated_mode_scores_against_different_thresholds() -> None:
    """E3 (one user measurement) is a different question from E2, so it must
    produce a different gate, not the same one relabelled."""
    e2 = next(g for g in run(DUMMY, calibrated=False).gates if g.experiment in {"E2", "E3"})
    e3 = next(g for g in run(DUMMY, calibrated=True).gates if g.experiment in {"E2", "E3"})
    assert e2.experiment != e3.experiment
    assert e2.criterion != e3.criterion


def test_writes_a_csv_and_a_markdown_report(tmp_path: Path) -> None:
    summary = run(DUMMY, calibrated=False)
    csv_path = tmp_path / "metrics.csv"
    md_path = tmp_path / "report.md"
    write_csv(summary, csv_path)
    write_markdown(summary, False, md_path)

    with csv_path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 3
    for column in ("room_id", "capture_id", "wall_err_median_pct", "polygon_iou"):
        assert column in rows[0]

    report = md_path.read_text(encoding="utf-8")
    assert "# Evaluation report: `dummy`" in report
    assert "Phase 1 gate" in report
    # Whatever the verdicts are, they have to be stated.
    assert "**PASS**" in report or "**FAIL**" in report
    for capture in ("gt-000-a", "gt-000-b", "gt-000-c"):
        assert capture in report


def test_a_prediction_with_no_ground_truth_is_reported_not_dropped(tmp_path: Path) -> None:
    """Silently ignoring an unmatched file is how a typo removes a hard room
    from the average and makes a bad model look good."""
    strays = tmp_path / "strays"
    strays.mkdir()
    (strays / "gt-000-a.json").write_text(
        (DUMMY / "gt-000-a.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (strays / "gt-999-typo.json").write_text(
        (DUMMY / "gt-000-a.json").read_text(encoding="utf-8"), encoding="utf-8"
    )

    summary = run(strays, calibrated=False)
    assert summary.unmatched == ["gt-999-typo"]
    assert {s.capture_id for s in summary.scores} == {"gt-000-a"}
    # Every other capture in ground truth has no prediction here.
    assert set(summary.missing) == all_capture_ids() - {"gt-000-a"}

    md_path = tmp_path / "report.md"
    write_markdown(summary, False, md_path)
    report = md_path.read_text(encoding="utf-8")
    assert "gt-999-typo" in report
    assert "gt-000-b" in report


def test_empty_run_does_not_crash(tmp_path: Path) -> None:
    """Phase 1 will produce runs where everything failed. That has to render as
    a report saying so, not as a traceback."""
    empty = tmp_path / "empty"
    empty.mkdir()
    summary = run(empty, calibrated=False)
    assert summary.scores == []
    assert set(summary.missing) == all_capture_ids()

    write_csv(summary, tmp_path / "metrics.csv")
    write_markdown(summary, False, tmp_path / "report.md")
    assert "_No captures scored._" in (tmp_path / "report.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("calibrated", [False, True])
def test_report_is_deterministic(calibrated: bool, tmp_path: Path) -> None:
    """Two runs over the same inputs must produce identical bytes, or the
    Phase 1 bake-off cannot be diffed between models."""
    first, second = tmp_path / "a.md", tmp_path / "b.md"
    write_markdown(run(DUMMY, calibrated=calibrated), calibrated, first)
    write_markdown(run(DUMMY, calibrated=calibrated), calibrated, second)
    assert first.read_bytes() == second.read_bytes()
