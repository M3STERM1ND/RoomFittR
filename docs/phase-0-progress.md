# Phase 0 progress

Live status for `implementation-plan.md` §8 Phase 0 (Foundations & Evaluation Dataset).
Updated as work lands. **If work stopped partway, the "Stopped at" line below is the
resume point.**

**Stopped at:** _in progress — see task table_

---

## Task status

| # | Task (from the plan) | Status | Notes |
|---|---|---|---|
| 1 | Monorepo skeleton (pnpm + uv), lint/format/typecheck, GitHub Actions CI | in progress | Layout + pnpm/uv workspaces done. CI workflow still to write. |
| 2 | `packages/schemas`: RoomModel, ObjectsFile, LayoutPlan, ValidationReport v0 + TS/Pydantic codegen + CI staleness check | in progress | 4 schemas + common written; TS and Pydantic codegen both working; `--check` staleness gate working. Round-trip tests still to write. |
| 3 | Hand-author 6 fixture RoomModels | not started | |
| 4 | Capture evaluation dataset (12–15 rooms × 3 captures, laser ground truth) | **blocked — needs you** | Physical capture. Everything around it can be built. |
| 5 | `eval/` harness: run → compare → metrics CSV + markdown | not started | |
| 6 | Modal / R2 / Supabase / Sentry accounts | **blocked — needs you** | Account creation + billing. Scaffolding and runbook can be built. |
| 7 | License verification table (§1.2) | not started | Table can be built; the licence facts themselves need verifying against real sources. |
| 8 | E9 browser recording spike (MediaRecorder) | not started | |

Status values: `not started` · `in progress` · `done` · `blocked — needs you`

---

## What only you can do

These are in Phase 0 but are not things I can complete:

1. **Task 4 — capture the evaluation dataset.** Needs a phone, a laser measure and
   12–15 real rooms. This is the single biggest Phase 0 item and it gates Phase 1.
2. **Task 6 — create accounts.** Modal, Cloudflare R2, Supabase and Sentry all need
   signup and, in places, payment details.
3. **Task 7 — confirm the licences.** I can build the table and record what each
   check needs, but I will not write down a licence I have not read. Model weights
   licences change between checkpoints and getting one wrong is exactly the risk
   §1.2 exists to catch.

---

## Definition of done (from the plan)

> Dataset captured and measured; eval harness runs end to end on a dummy result;
> fixtures committed; license table filled; E9 findings written up.
