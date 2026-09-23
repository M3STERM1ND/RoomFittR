# Phase 4 progress

Live status for `implementation-plan.md` §8 Phase 4 (Layout Engine). Updated
as work lands. **If work stopped partway, the "Stopped at" line below is the
resume point.**

**Stopped at:** _**Both Claude calls are now built and measured, 2026-09-22.**
`workers/planner` holds L2's planner and L4's coherence pick; the layout
engine stays network-free and the planner wraps it. A real layout costs
**$0.024–$0.029**, against §8's ≤ $0.10 clause. What remains needs a Postgres
catalog or two people scoring sixty layouts — see §"What only you can do"._

---

## Task status

| Task (from the plan) | Status | Notes |
|---|---|---|
| `category_rules.yaml` for all categories | **done** | All 22 categories of §4.2. Reviewable data rather than code, with neutral defaults so a category the catalog invents first degrades to "no special rules". |
| L1 room analysis | **done** | `room.py`: door keep-outs (both sides when the swing is unknown, which V1 leaves it), window zones, free wall runs, circulation raster. |
| Python validator (H1–H7, S1–S6) | **done** | `validator.py`. Every violation carries a measured value and a sentence, because §5.5 makes the report what the UI shows. |
| **TS validator** | **done** | `packages/geometry`. No dependencies: it runs on every pointer-move, which §9.2 budgets at 8 ms p95. |
| **Shared parity fixtures** | **done** | `fixtures/validation/` — 20 cases covering every rule, generated from Python and asserted byte-identical by both sides. §9.1 gates this on every CI run. |
| L2 LLM planner | **done** | `workers/planner/plan.py`: one `claude-sonnet-5` call with adaptive thinking, tool-schema output, two attempts then the template. The template plans, post-checks and chaos tests still run underneath and are what happens with no key. |
| Template fallback plans per room type | **done** | Living, bedroom, office, dining. Not a degraded mode: §5.4 says a layout is always produced, so these are the floor the product stands on. |
| L3 shortlist + ranking | **done against an in-memory catalog** | The filtering and ranking are written and tested; §4.11's `layout_candidates` view is a thin adapter once Postgres exists. |
| L4 product pick + budget rebalancing | **done** | `workers/planner/pick.py`: one `claude-haiku-4-5-20251001` call for the whole layout. An id outside the slot's shortlist is dropped for the top-ranked item (§5.4), and the budget pass — swap down, drop lowest priority, upgrade the musts — runs over whatever it chose. |
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
| LLM cost per layout measured and ≤ $0.10 | ✅ **measured**: $0.0292 (living) and $0.0241 (bedroom) per layout, two calls each, against the fixture rooms and the in-memory catalog. `workers/planner/tests/test_live.py` asserts the clause; run it with `ROOMFITTR_LIVE_LLM=1`. A real catalog has longer product titles, so re-measure when Phase 3 lands. |

**Phase 4 is not complete**, and the one outstanding clause is the human
evaluation — the only one nothing automated can discharge.

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

6. **`strict: true` tools take a subset of JSON Schema.** `maxItems`,
   `minimum` and `maximum` are rejected with a 400. Because §7.4 makes every
   LLM error a template fallback, the first live run produced perfectly legal
   layouts from the template while the model was never called once — the
   failure mode this project keeps meeting, and the reason
   `UNSUPPORTED_KEYWORDS` now has a test walking every schema rather than a
   comment. §5.4's numeric bounds moved to `post_check`, which is where they
   have to hold anyway.
7. **The solver threw away which product it placed.** `Placement` carried
   `slot_id` but not `product_id`, so a finished layout recorded that slot S1
   held a sofa costing €400 and not *which* sofa. §6.2's `placed_items` has
   both columns, and without the product id a layout cannot be persisted,
   repriced on a catalog refresh, or linked to a retailer — which is the
   entire point of L4. Found by a test asserting the model's choice was
   honoured, because there was no way to ask.
8. **Two of §5.4's own rules collide.** L4 says the model picks "favoring
   visual coherence" and also that leftover money upgrades `must` slots. The
   upgrade ranks on price alone, so with a loose budget it replaced the chosen
   sofa with the dearest in the shortlist every time — R12 reintroduced by the
   budget pass. A deliberate choice is now exempt from the upgrade; swapping
   *down* is not, because H7 is hard and being over budget has no legal
   alternative.

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

1. **Phase 3's catalog**, which needs the retailer decisions in D7–D9 and a
   Supabase project. The layout engine is written against `Product`, which is
   the shape of §4.11's `layout_candidates` view.
2. **The human evaluation.** §8 asks both of you to score sixty layouts 1–5 on
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

## What D12 still needs

D12 asks whether Claude is the right planner, decided by running the layout
eval set against `claude-sonnet-5`, `claude-haiku-4-5` and an open-weights
model and comparing human scores against cost. Two of the three inputs now
exist: the call is built and the cost is measured. What is missing is the
human scores, so D12 stays open and is gated on the same evaluation as the
DoD clause above.

One observation from sampling the live planner, recorded because it will
matter when those scores come in: given §5.4's `default_budget_ratios`, the
model reproduces them almost exactly (0.34 / 0.12 / 0.12 / 0.14 for a living
room). Its real contribution in V1 is the **wall anchors**, which do vary
run to run and are the spatially interesting half. If the human eval comes
back flat between the model and the template, the budget split is the part to
suspect first — not the anchoring.

`ANTHROPIC_LAYOUT_MODEL` and `ANTHROPIC_PICK_MODEL` override the model ids,
so the bake-off needs no code change.