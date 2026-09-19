"""The evaluation harness: ground truth in, metrics table out.

    uv run python -m eval.harness --run runs/dummy
    uv run python -m eval.harness --run runs/dummy --calibrated

A "run" is a directory of predicted RoomModel JSON files named after the
capture they came from:

    eval/runs/<run-name>/
        gt-001-a.json        <- a RoomModel, exactly as the pipeline emits it
        gt-001-b.json
        meta.json            <- optional: what produced this run

Keeping the harness's input a plain directory of RoomModels is deliberate. It
means a reconstruction model can be scored without being wired into anything,
and it is why this can run end to end today against a dummy result, months
before there is a pipeline (Phase 0 definition of done).

Outputs land in eval/reports/<run-name>/ as metrics.csv and report.md.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from eval.metrics import GateResult, RoomScore, evaluate_gates, score_capture

EVAL_DIR = Path(__file__).resolve().parent
GROUND_TRUTH_DIR = EVAL_DIR / "ground_truth"
RUNS_DIR = EVAL_DIR / "runs"
REPORTS_DIR = EVAL_DIR / "reports"


@dataclass
class RunSummary:
    run_name: str
    scores: list[RoomScore]
    gates: list[GateResult]
    missing: list[str]
    unmatched: list[str]


def load_ground_truth() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(GROUND_TRUTH_DIR.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        out[doc["room_id"]] = doc
    return out


def run(run_dir: Path, calibrated: bool) -> RunSummary:
    truth_by_room = load_ground_truth()

    # capture_id -> room_id, so a prediction can find its yardstick.
    capture_to_room = {
        c["capture_id"]: room_id
        for room_id, doc in truth_by_room.items()
        for c in doc.get("captures", [])
    }

    predictions = {p.stem: p for p in sorted(run_dir.glob("*.json")) if p.stem != "meta"}

    scores: list[RoomScore] = []
    unmatched: list[str] = []

    for capture_id, path in predictions.items():
        room_id = capture_to_room.get(capture_id)
        if room_id is None:
            # A prediction with no ground truth is not a silent skip. It is
            # usually a typo in a filename, and a typo that quietly removes a
            # room from the average is exactly how a bad model looks good.
            unmatched.append(capture_id)
            continue
        predicted = json.loads(path.read_text(encoding="utf-8"))
        scores.append(score_capture(room_id, capture_id, truth_by_room[room_id], predicted))

    missing = [c for c in sorted(capture_to_room) if c not in predictions]

    scores.sort(key=lambda s: (s.room_id, s.capture_id))
    return RunSummary(
        run_name=run_dir.name,
        scores=scores,
        gates=evaluate_gates(scores, calibrated=calibrated),
        missing=missing,
        unmatched=unmatched,
    )


def write_csv(summary: RunSummary, out: Path) -> None:
    rows = [s.as_row() for s in summary.scores]
    out.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        out.write_text("", encoding="utf-8")
        return
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _cell(value: Any) -> str:
    if value == "" or value is None:
        return "-"
    if isinstance(value, float) and math.isnan(value):
        return "-"
    return str(value)


def write_markdown(summary: RunSummary, calibrated: bool, out: Path) -> None:
    mode = "calibrated (one user measurement, E3)" if calibrated else "uncalibrated (E2)"
    lines = [
        f"# Evaluation report: `{summary.run_name}`",
        "",
        f"Scale mode: **{mode}**",
        f"Captures scored: **{len(summary.scores)}**",
        "",
        "## Phase 1 gate (implementation-plan.md 3.10)",
        "",
        "| Experiment | Question | Measured | Criterion | Result |",
        "|---|---|---|---|---|",
    ]
    verdicts = {None: "not enough data", True: "**PASS**", False: "**FAIL**"}
    for g in summary.gates:
        verdict = verdicts[g.passed]
        lines.append(
            f"| {g.experiment} | {g.question} | {g.measured} | {g.criterion} | {verdict} |"
        )

    lines += ["", "## Per-capture metrics", ""]
    if summary.scores:
        rows = [s.as_row() for s in summary.scores]
        headers = list(rows[0].keys())
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("|" + "---|" * len(headers))
        for row in rows:
            lines.append("| " + " | ".join(_cell(row[h]) for h in headers) + " |")
    else:
        lines.append("_No captures scored._")

    if summary.missing:
        lines += [
            "",
            "## Captures with no prediction in this run",
            "",
            "Ground truth exists for these but the run produced nothing. Either the",
            "pipeline failed on them or they were never run: both matter, because a",
            "success rate computed only over the captures that worked is not one.",
            "",
        ]
        lines += [f"- `{c}`" for c in summary.missing]

    if summary.unmatched:
        lines += [
            "",
            "## Predictions with no ground truth",
            "",
            "These were scored against nothing and excluded. Usually a filename typo.",
            "",
        ]
        lines += [f"- `{c}`" for c in summary.unmatched]

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run",
        required=True,
        help="Run directory, relative to eval/ or absolute (e.g. runs/dummy).",
    )
    parser.add_argument(
        "--calibrated",
        action="store_true",
        help="Score against E3 thresholds (one user measurement) instead of E2.",
    )
    args = parser.parse_args()

    run_dir = Path(args.run)
    if not run_dir.is_absolute():
        run_dir = EVAL_DIR / run_dir
    if not run_dir.is_dir():
        print(f"No such run directory: {run_dir}")
        return 1

    summary = run(run_dir, calibrated=args.calibrated)
    report_dir = REPORTS_DIR / summary.run_name
    write_csv(summary, report_dir / "metrics.csv")
    write_markdown(summary, args.calibrated, report_dir / "report.md")

    print(f"scored {len(summary.scores)} capture(s)")
    for g in summary.gates:
        verdict = "?" if g.passed is None else ("PASS" if g.passed else "FAIL")
        print(f"  {g.experiment:3s} {verdict:4s} {g.measured}")
    if summary.missing:
        print(f"  {len(summary.missing)} capture(s) had no prediction")
    if summary.unmatched:
        print(f"  {len(summary.unmatched)} prediction(s) had no ground truth")
    print(f"wrote {report_dir / 'metrics.csv'}")
    print(f"wrote {report_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
