# Phase 0 progress

Live status for `implementation-plan.md` §8 Phase 0 (Foundations & Evaluation Dataset).
Updated as work lands. **If work stopped partway, the "Stopped at" line below is the
resume point.**

**Stopped at:** _**Phase 0 complete 2026-09-19.** All five definition-of-done clauses met:
13 Tier A scenes converted and validating, harness green in CI, six fixtures, licence table
filled, E9 measured on two real devices. Two caveats carried into Phase 1, neither blocking
— see §"What Phase 1 should know" below._

---

## Task status

| # | Task (from the plan) | Status | Notes |
|---|---|---|---|
| 1 | Monorepo skeleton (pnpm + uv), lint/format/typecheck, GitHub Actions CI | **done** | pnpm + uv workspaces, 4 CI jobs. All gates green: `ruff`, `ruff format`, `mypy --strict`, `pytest`, `prettier`, `eslint`, `tsc`, `next build`. |
| 2 | `packages/schemas`: RoomModel, ObjectsFile, LayoutPlan, ValidationReport v0 + TS/Pydantic codegen + CI staleness check | **done** | 4 schemas + common. TS + Pydantic generated from the JSON Schemas; `pnpm schemas:check` fails on stale output. Round-trip and rejection tests in `workers/schemas_py/tests/`. |
| 3 | Hand-author 6 fixture RoomModels | **done** | `fixtures/rooms/` — rectangular, L-shaped, small bedroom, open boundary, many openings, narrow. Built by `_build.py`, validated against the schema and checked for geometric self-consistency in CI. Track Product is unblocked. |
| 4 | **Tier A** evaluation dataset: 12–15 **ARKitScenes** scenes via the adapter | **done — 13 scenes** | Rescoped twice (see below). Adapter written and tested ([`eval/adapters/`](../eval/adapters/)); full render→fuse→layout round trip under 1% wall error. **No application, no institutional email, no measuring, no spend** — follow [`eval/TIER_A_GUIDE.md`](../eval/TIER_A_GUIDE.md). **Download state on this machine (checked 2026-09-19):** `47332764` → converted to `gt-001`, but its **`.mov` is missing**, so there is ground truth and no pipeline input; `47333462` has the `.mov` but **no `highres_depth`**, so it cannot be converted; `41069021` is an **empty directory**. Re-pull all five asset types per scene. |
| 5 | `eval/` harness: run → compare → metrics CSV + markdown | **done** | `eval/harness.py` + `eval/metrics.py` scoring E2/E3/E4/E5. Runs end to end on the committed `eval/runs/dummy` fixture, now asserted by `eval/tests/test_harness_end_to_end.py` in CI rather than by hand. **Fixed 2026-09-19:** three of those tests hard-coded the capture count from when `gt-000-example` was the only ground-truth file, so adding `gt-001` turned CI red and each further scene would have done it again. They now derive the expected set from `load_ground_truth()`. |
| 6 | Modal / R2 / Supabase / Sentry accounts | **moved to Phase 1** | Nothing reads these until the first GPU run, and some need a card. Runbook is written and waiting: [`docs/infrastructure-setup.md`](./infrastructure-setup.md) + [`.env.example`](../.env.example). The one Phase 0 remnant, HF access to the two gated checkpoints, is **done**: `facebook/VGGT-1B-Commercial` and `facebook/sam3` approved and verified by real file download on 2026-09-19; `HF_TOKEN` is in `.env.local`. It still has to reach a Modal secret in Phase 1. |
| 7 | License verification table (§1.2) | **done** | [`docs/model-licenses.md`](./model-licenses.md). Every row read from its primary source on 2026-09-17 and cited. Three findings change the plan — see below. |
| 8 | E9 browser recording spike (MediaRecorder) | **done** | Measured 2026-09-19 on **iPhone / iOS 18.7 / Safari 26.6** and **Windows / Chrome 153**. E9 passes: 3-min 1080p capture survived app switch *and* screen lock, plays back; chunked upload resumes from the failed part. One real finding — chunk delivery froze ~43 s around backgrounding and flushed late, which costs D3 a tolerance. See [`findings.md`](../spikes/e9-browser-recording/findings.md) note B. |

Status values: `not started` · `in progress` · `done` · `blocked — needs you`

---

## Fixed 2026-09-19 — the adapter assumed the wrong up-axis

**The bug.** `eval/adapters/arkitscenes.py` treated **y** as the vertical axis. ARKitScenes'
world frame is **z-up**. Every room converted before this was wrong.

**Why it was nearly invisible.** It did not crash. It produced rooms that passed schema
validation — a kitchen annotated with 19 furniture boxes came out as 0.61 × 1.35 m under a
4.02 m ceiling. `gt-001` (the original "2/2 passing" file) had a 3.63 m ceiling over 5.6 m²
and looked merely unusual rather than broken.

**First diagnosis was wrong, and the correction matters.** The failures correlated with
ARKitScenes' `sky_direction` column, which made it look like an orientation-handling bug.
It was not: `sky_direction` describes how the *device* was held, the pose already accounts
for it, and the camera's own down-vector is therefore useless as a gravity signal — for a
`Left` scene, image-down points along world-x. The world frame was z-up for **every** scene
measured, including the `Up` one. `Left` scenes failed loudly; the `Up` scene failed
quietly, which is the only reason `gt-001` ever existed.

**How up is now determined**, per scene, from two independent signals:

| Signal | What it gives | Why it is trustworthy |
| --- | --- | --- |
| Spread of camera centres | which axis | Somebody walking a room moves metres horizontally, centimetres vertically. Measured ratio 0.29–0.70 against the next axis. |
| Annotated furniture bottoms | which way up | Furniture rests on the floor. Measured **26–96 mm** from the floor extreme versus **2000+ mm** from the ceiling, on every scene checked. |

The furniture signal is Apple's annotation, not our geometry, so it is a genuine
cross-check rather than a restatement of the same assumption. The adapter **raises** when
the axis is ambiguous (a stairwell, a two-floor capture) or the annotation is missing,
because a wrong up-axis does not fail downstream — it yields a plausible room of the wrong
size.

**Effect on the same scenes, before and after:**

| scene | before | after |
| --- | --- | --- |
| 47332764 | 5.6 m², ceiling 3.63 m | 7.8 m², ceiling **2.71 m** |
| 41048223 | 0.5 m², ceiling 4.02 m | 6.4 m², ceiling **2.62 m** |
| 41048225 | 1.1 m², ceiling 3.34 m | 7.3 m², ceiling **2.63 m** |
| 41048229 | 1.2 m², ceiling 3.52 m | 6.8 m², ceiling **2.63 m** |

Every ceiling now lands in 2.2–2.9 m.

**Test cover.** The bug survived because no test touched `fuse_scene` — the round-trip test
builds synthetic clouds already in the y-up frame and bypasses fusion entirely. Added to
`eval/tests/test_arkitscenes_adapter.py`: `alignment_matrix` maps the detected up-direction
onto +y and is a rotation rather than a reflection (a reflection would mirror the floor plan
undetected); `detect_up` recovers each axis and both signs; it refuses an ambiguous capture
and a scene with no annotation; and one regression test builds a z-up room and asserts the
naive y-up reading is wrong before asserting the aligned reading is right.

### Outcome of the rebuild

25 scenes downloaded and converted; **13 kept, 12 rejected**. Rejections are recorded with
reasons in `eval/.rejected.json`. The adapter refused five outright (no horizontal surface
near floor or ceiling, or a floor outline collapsing to two corners) and an automated
plausibility rule dropped seven more for a footprint under 4 m² — the failure that produced
the original bad files, where a room was too small to hold its own annotated furniture.

Spread of the 13, against §3.10's ask:

| room type | count |
| --- | --- |
| living | 3 |
| bedroom | 3 |
| kitchen_dining | 3 |
| open_plan | 3 |
| office | 1 |

Nine `cluttered`, two `sparse`. Areas span 4.2–17.7 m² and ceilings 2.08–2.82 m.

## What Phase 1 should know

Two things are true of this dataset that the DoD does not capture, and both affect how much
E2/E4 results can be trusted:

1. **The floor outlines are ragged and none has been checked by eye.** Corner counts run
   14–50 and bounding-box fill 0.29–0.56. Some of that is honest — a traced outline around
   kitchen cabinets is not a clean rectangle — but the guide's caveat 1 (a scan that ran
   through an open doorway grows an arm no connected-component test can remove) has **not**
   been ruled out on any scene. The automated rule catches rooms that are too small; it
   cannot catch a plausibly-sized wrong shape. **Look at a few floor plans before trusting
   an E4 polygon-IoU number.**
2. **Room type and traits are machine-derived**, from the 17-class furniture annotation
   (a `bed` box implies bedroom; `stove` + `refrigerator` implies kitchen) and object count
   for cluttered/sparse. The appearance traits §3.10 asks for — `white_walls`, `glass`,
   `dark`, `large_mirror`, `bay_window`, `sloped_ceiling` — need RGB and are **not set on any
   scene**. So "one sparse white-walled room" is half-satisfied: sparse yes, white-walled
   unverified.

A third, smaller note: the 48% rejection rate means the kept set is biased toward scenes
whose floor was scanned well. That is the right bias for a yardstick, but it makes Tier A
easier than a random ARKitScenes sample, and E8 (capture robustness) is on Tier B anyway.

---



## Rescope (2026-09-17)

Phase 0 as written cost a laser measure, several weekends and access to other people's
homes — all spent *before* knowing whether the pipeline works at all. The gate needs a
yardstick, not specifically **our** yardstick, so `implementation-plan.md` §3.10 and §8 were
revised:

- **Tier A (Phase 0, free):** 12–15 **ARKitScenes** scenes — FARO laser depth, real iPad
  captures, and 17-class furniture boxes that give E6 for free. Layout derived by
  `eval/adapters/arkitscenes.py`.
- **Tier B (after the gate):** 5–8 rooms in our own homes, tape measure is fine. Only paid
  for if the pipeline already cleared Tier A.
- **Accounts moved to Phase 1**, which is the first thing that reads them.

**Second revision, same day: ScanNet++ → ARKitScenes.** ScanNet++ was the first pick on data
quality, but its application — like ZInD's and Matterport3D's — verifies **academic
credentials / an institutional email**, which we do not have. ARKitScenes is a plain
download script with no account or affiliation check, and its licence is actually *more*
permissive (commercial use granted below 700M MAU). The rejected options are recorded in
`docs/model-licenses.md` §4b so nobody rediscovers this.

**The cost of the swap:** ARKitScenes annotates 17 furniture classes and **no doors or
windows**, so **E5 loses its Tier A ground truth** and joins Tier B. That is cheap — doors
and windows are the easiest things in a room to measure with a tape — but it is why Tier B
grew from 3–5 rooms to 5–8.

**Consequence for the gate:** E1/E4/E6/E7 are decided on Tier A; **E2, E3, E5 and E8 on
Tier B**, so the Phase 1 verdict comes in two steps. Written into §3.10, not just here.

---

## Findings that change the plan

From task 7 ([`model-licenses.md`](./model-licenses.md)):

1. **Depth Anything 3's strong checkpoints are non-commercial.** `DA3-LARGE` and
   `DA3-GIANT` are CC BY-NC 4.0. The Apache-2.0 set is `DA3-BASE`, `DA3-SMALL`,
   `DA3METRIC-LARGE`, `DA3MONO-LARGE`. E1 must be run on a checkpoint we can ship.
2. **SpatialLM is entirely non-commercial**, on every variant, because the point-cloud
   encoder is CC BY-NC 4.0 regardless of the base LLM. §3.6's "SpatialLM candidate learned
   path" is an **E4 baseline only** — the classical geometry path must stay the guaranteed
   one.
3. **Two models ship near-identical checkpoints with different licences**
   (`facebook/map-anything` vs `-apache`, `facebook/VGGT-1B` vs `-Commercial`). Exact ids
   are pinned in §5 of that file. The commercial VGGT checkpoint and SAM 3 weights are
   **gated** — request HF access before the Modal image build needs them.

---

## What only you can do

One thing left (the other two cleared on 2026-09-19):

1. **Download 11–14 more ARKitScenes scenes** and convert them —
   <https://github.com/apple-aiml-research/ARKitScenes>. No account, no application. Follow
   [`eval/TIER_A_GUIDE.md`](../eval/TIER_A_GUIDE.md). The conventions are already confirmed
   by `gt-001`, so `--inspect` is now a per-scene sanity check rather than a one-off.
   **Two judgement calls per scene are yours, not the adapter's:** `--room-type` and
   `--traits` (for the §3.10 spread), and the eyeball check on the floor polygon that
   catches a scan that ran through an open doorway.
2. ~~**Click Hugging Face access** on `facebook/VGGT-1B-Commercial` and `facebook/sam3`.~~
   **Done 2026-09-19** — both approved, token in `.env.local`, both verified by real download.
   Goes into a Modal secret in Phase 1 (`docs/infrastructure-setup.md` §4).
3. ~~**Run the E9 page on your phone.**~~ **Done 2026-09-19** — iPhone iOS 18.7 / Safari
   26.6 and desktop Chrome 153 both measured and written up in `findings.md`.

---

## Definition of done (from the plan)

> 12–15 Tier A ground-truth files committed and passing validation; eval harness runs end
> to end on a dummy result; fixtures committed; license table filled (models + dataset
> terms); E9 findings written up with at least one real device measured.

(As revised — see §Rescope.)

| Clause | State |
|---|---|
| 12–15 Tier A ground-truth files committed and valid | ✅ **13 scenes**, `14/14 ground-truth files valid` (13 + the harness fixture) |
| Eval harness runs end to end on a dummy result | ✅ asserted in CI |
| Fixtures committed | ✅ six, validated in CI |
| License table filled (models + dataset terms) | ✅ models done; dataset terms recorded on access |
| E9 findings written up, ≥ 1 real device measured | ✅ written; **2 devices measured** (iPhone iOS 18.7 / Safari 26.6, Windows Chrome 153) |
