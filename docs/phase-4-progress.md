# Phase 4 progress

Live status for `implementation-plan.md` §8 Phase 4 (Layout Engine). Updated
as work lands. **If work stopped partway, the "Stopped at" line below is the
resume point.**

**Stopped at:** _**Everything buildable without an API key or a database is
done, 2026-09-20.** The engine plans, shortlists, picks, solves, validates and
renders a layout end to end, in Python and (for the validator, placer and
proxies) in TypeScript, with the two pinned together. What remains needs the
Claude API, a Postgres catalog, or two people scoring sixty layouts — see
§"What only you can do"._

---

## Task status

| Task (from the plan) | Status | Notes |
|---|---|---|
| `category_rules.yaml` for all categories | **done** | All 22 categories of §4.2. Reviewable data rather than code, with neutral defaults so a category the catalog invents first degrades to "no special rules". |
| L1 room analysis | **done** | `room.py`: door keep-outs (both sides when the swing is unknown, which V1 leaves it), window zones, free wall runs, circulation raster. |
| Python validator (H1–H7, S1–S6) | **done** | `validator.py`. Every violation carries a measured value and a sentence, because §5.5 makes the report what the UI shows. |
| **TS validator** | **done** | `packages/geometry`. No dependencies: it runs on every pointer-move, which §9.2 budgets at 8 ms p95. |
| **Shared parity fixtures** | **done** | `fixtures/validation/` — 20 cases covering every rule, generated from Python and asserted byte-identical by both sides. §9.1 gates this on every CI run. |
| L2 LLM planner | **fallback only — blocked** | The template plans, the post-checks and the chaos tests are done and are what runs today. The Claude call needs `ANTHROPIC_API_KEY`. |
| Template fallback plans per room type | **done** | Living, bedroom, office, dining. Not a degraded mode: §5.4 says a layout is always produced, so these are the floor the product stands on. |
| L3 shortlist + ranking | **done against an in-memory catalog** | The filtering and ranking are written and tested; §4.11's `layout_candidates` view is a thin adapter once Postgres exists. |
| L4 product pick + budget rebalancing | **done** | Deterministic path complete: swap down, drop lowest priority, upgrade the musts with what is left. The Haiku call for visual coherence is the blocked half. |
| L5 solver | **done** | Beam search over whole partial layouts, because a pose is only good relative to what is already placed. |
| L6 solve–validate–retry | **done** | `engine.py`. Circulation and budget are not local, so they cannot be checked per pose; this is what makes the acceptance property a guarantee. |
| **TS single-item placer** | **done** | `packages/geometry/src/placer.ts`. Tested by placing and then running the real validator over the result. |
| **Parametric furniture proxies** | **done** | `packages/furniture-proxies`: 25 families covering all 22 categories. |
| Layout review tool | **not started** | A developer convenience for eyeballing a layout. Worth building alongside the Phase 6 viewer rather than twice. |
| Layout eval set (~60 cases) | **partial** | The property half is done and runs in CI: six fixture rooms × two budgets × two room types, all with zero hard violations and within budget. The *human* half needs a real catalog and two people. |

---

## Definition of done (from the plan)

| Clause | State |
|---|---|
| 100% of eval cases produce a layout with zero hard violations and within budget | ✅ for the property suite over the fixture rooms; the 60-case set needs Phase 3's catalog |
| Parity suite green | ✅ 20 fixtures, byte-identical, in CI |
| Human eval mean ≥ 3.5, no case scoring 1 from a rule bug | ❌ **human task** — needs two people scoring sixty layouts |
| LLM cost per layout measured and ≤ $0.10 | ❌ blocked — no API key, so no call has been made |

**Phase 4 is not complete**, and the two outstanding clauses are the ones that
need a person and a bill respectively.

---

## What was found while building

Recorded because each was invisible to every structural check, which is the
pattern this project keeps hitting.

1. **Rotation handedness.** `yaw_deg` is right-handed about +Y — 2.4,
   three.js and `align.yaw_rotation` all agree — so a positive rotation
   carries +X towards −Z. The 2D rotation matrix one writes by reflex does the
   opposite. The pipeline's object boxes had it backwards, which would have
   drawn every ghost box mirrored; nothing caught it because a box and its
   mirror have identical dimensions.
2. **A silently-skipped patch.** The static-grid caching described in one
   commit message was never applied — the replacement target did not match and
   failed quietly, so the measurement that followed showed no improvement and
   the wrong conclusion was drawn from it. Applied properly, it is part of
   what took worst-case generation from 5.5 s to 3.9 s against the 5 s budget.
3. **Two failures that look alike.** §7.4 separates "catalog too thin for a
   category → skip the slot with an explanation" from "solver can't place a
   `must` slot → fail the layout". Conflating them meant an empty catalog
   produced no room at all rather than an empty one — which is exactly the
   state the project is in until Phase 3 runs.
4. **Infinity is not NaN.** A non-finite budget share passed a `> 0` and
   `== itself` filter, reached the renormalisation, made the total infinite,
   and set *every* share to zero. One bad number from a model would have
   silently zeroed the budget.
5. **Proportion bands assume real furniture.** A table top is 25–70 mm thick,
   so a catalog row claiming a 12 mm-tall table gives its legs a height of
   −13 mm. §9.3 rates wrong catalog dimensions R7, "High" likelihood, so that
   is expected input; every proxy dimension is now derived by splitting what
   is left rather than by subtracting and hoping.

## Two deliberate deviations, both recorded at the code

- **`objects.MERGE_CONTAINMENT`** adds containment alongside §3.5's specified
  `IoU > 0.3`. IoU alone does not do the job the plan asks of it: a partial
  view is *contained* in the full view rather than similar to it, and a 0.4 m
  glimpse of a 2 m sofa scores 0.09. E6 measures whether this was right.
- **Keep-outs are oriented rectangles, not polygons**, and the circulation
  grid uses distances rather than shapely buffers. Both changes were made so
  the browser could reproduce the Python answer exactly; §5.5 says the
  validator has exactly two primitives, and a keep-out as a general polygon
  was a third.

---

## What only you can do

1. **An Anthropic API key** (`ANTHROPIC_API_KEY`). Unblocks L2's planner and
   L4's coherence pick, and is the only way to measure the ≤ $0.10-per-layout
   clause or to resolve D12. Everything downstream already runs without it.
2. **Phase 3's catalog**, which needs the retailer decisions in D7–D9 and a
   Supabase project. The layout engine is written against `Product`, which is
   the shape of §4.11's `layout_candidates` view.
3. **The human evaluation.** §8 asks both of you to score sixty layouts 1–5 on
   "would I actually arrange it this way", target mean ≥ 3.5. This is the
   clause that decides whether the solver's scoring weights are right, and
   nothing automated substitutes for it — the weights in `solver.py` are
   explicitly a prior, not a result.

## What Phase 6 should know

- **The validator is the contract, and it exists twice.** Import
  `@roomfittr/geometry` in the editor rather than calling the API on every
  drag. If you change a rule, change it in both and regenerate
  `fixtures/validation/` — regenerating to make a failing parity test pass
  records whatever Python does and leaves the disagreement in place.
- **`@roomfittr/furniture-proxies` gives you boxes, not meshes.** A proxy is a
  list of oriented boxes precisely so the viewer can draw a whole family in
  one instanced call, which is what keeps §9.2's 150-draw-call budget
  reachable.
- **Millimetres everywhere, metres only at the glTF boundary.** The pipeline,
  the layout engine and both validators are integer millimetres (2.4). The
  shell mesh converts once, at export.
