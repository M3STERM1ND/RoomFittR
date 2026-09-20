# Phase 1 progress

Live status for `implementation-plan.md` §8 Phase 1 (Video → 3D Room Proof of
Concept — **the gate**). Updated as work lands. **If work stopped partway, the
"Stopped at" line below is the resume point.**

**Stopped at:** _**Blocked on infrastructure, 2026-09-20.** Every CPU stage of
the pipeline is built and tested (S1, S2, S5, S6, S7, S8, S9, plus the S3/S4
adapter contract). The gate itself — E1–E8 — cannot be run here: it needs a
GPU, the two gated Hugging Face checkpoints, a Modal workspace and the
ARKitScenes video assets, none of which exist on this machine. See
§"What only you can do"._

---

## Task status

Tasks are numbered as in §8 Phase 1.

| # | Task | Status | Notes |
|---|---|---|---|
| 1 | S1–S2 ingest/frames module with quality metrics; unit tests | **done** | `workers/pipeline/roomfittr_pipeline/{ingest,frames}.py`. ffprobe validation against the §3.2 contract, GPS/audio strip verified against the output file's bytes, decode at 3 fps with rotation resolved before S3 sees a pixel, sharpness/exposure/flow scoring, motion-based keyframe selection. Scoring and selection are pure functions over arrays, so their thresholds are tested without a video file. |
| 2 | S3 reconstruction adapters for candidates A, B, C + D baseline | **contract only — blocked** | `backends.py` defines the common interface §3.4 asks for (`frames → poses, intrinsics, depth, confidence`), the §3.4 failure detection, and the back-projection every candidate feeds into S5. **No model is loaded**: MapAnything, VGGT-1B and DA3 need GPU weights and a GPU. |
| 3 | Profile VRAM and time at N = 40/80/100/150 on L40S and A100-80 (E7) | **blocked** | Needs a GPU. |
| 4 | S4 minimal SAM 3 segmentation | **contract only — blocked** | `SegmentationBackend` protocol plus §3.5's three prompt groups, verbatim, with a test that no prompt appears in two groups. `facebook/sam3` weights are gated and the model needs a GPU. |
| 5 | S5 gravity + Manhattan alignment, structure/object partition | **done** | `align.py` and `objects.py`. RANSAC floor plane refitted on inliers, minimal rotation onto +Y, Manhattan yaw from a circular histogram modulo 90°. Oriented boxes by rotating calipers with the §3.5 percentile trim, and track merging. |
| 6 | S6 classical geometry extraction + sanity checks | **done** | `geometry.py`. Floor/ceiling as histogram peaks, wall lines fitted to evidence with the Manhattan prior, floor plan assembled from the line arrangement, open boundaries, opening fitting in wall-local coordinates, §3.6's sanity checks. |
| 7 | S6 SpatialLM candidate adapter (E4) | **not started — deliberately** | Phase 0's licence review found SpatialLM non-commercial on every variant (`docs/model-licenses.md`), so §3.6's learned path is an E4 baseline only and the classical path is what ships. Building the adapter is only worth it when E4 can actually be run. |
| 8 | S7 scale fusion (E2, E3) | **done** | `scale.py`. Weighted median over model/mono/door/ceiling/user sources, outlier rejection, confidence from the spread between sources. |
| 9 | S8 minimal shell (Tier 0) → GLB | **done** | `shell.py`. Floor, ceiling, walls with opening cutouts, window glass, exported as binary glTF in metres. Asserted far inside §9.2's 3 MB budget. |
| 10 | Debug viewer (three.js, point cloud + RoomModel + shell) | **not started** | Needs R2 to load artefacts from, and there are no real artefacts to look at until a reconstruction runs. |
| 11 | Run the bake-off E1–E8, write `eval/reports/phase1.md`, make the go/no-go call | **blocked** | This is the gate. Needs tasks 2, 3, 4 and the dataset. |

Status values: `not started` · `in progress` · `done` · `contract only — blocked` · `blocked`

---

## What was built, and what it is worth

Roughly 2,400 lines of pipeline and 1,900 of tests, 325 tests green, with
`ruff`, `ruff format`, `mypy --strict` and `pytest` all clean.

The honest summary is that **the deterministic spine of the pipeline is
finished and the learned parts are not started**. Everything between "here are
posed frames with depth" and "here is a schema-valid RoomModel and a GLB" now
exists and is tested against synthetic rooms whose true dimensions the tests
know. What is missing is the part that turns a video into posed frames with
depth, and that part is three model integrations behind an interface that is
already defined.

That split was not a preference. S3 and S4 are the only two stages that need a
GPU, and they are the only two that cannot be written here.

### Bugs found while building, all of the same shape

Worth recording because they share a character: each produced output that
passed every structural check and was wrong in a way only a measurement or a
viewer would reveal. This is the failure mode Phase 0's up-axis bug had too.

1. **Yaw sign.** `yaw_rotation(θ)` *reduces* a heading measured as
   `atan2(z, x)`, so cancelling a measured yaw needs the positive angle. The
   intuitive negation left every room at twice its true yaw — a perfectly
   valid RoomModel of a room rotated the wrong way.
2. **Floor-extent fallback.** The rule that lets open-plan rooms work will
   also shrink any room with a full-width obstruction against a wall, unless
   it fires only when the floor runs *past* all wall evidence. Floor stopping
   *short* of a wall is occlusion, not a boundary.
3. **Walls built inwards.** The shell extruded wall thickness into the room,
   so a measured 4 m span rendered as 3.8 m — an error the size of the 5%
   E2 holds the whole pipeline to, introduced after the measuring was done.
4. **A swallowed exception.** A broad `except` around the mesh extrusion hid a
   missing triangulation engine and returned a room with **no walls at all**,
   which every downstream stage accepted.
5. **Premature rounding.** `HeightProfile.height_mm` rounded to an integer in
   the input's units, quantising a metres-scale reconstruction's 2.5 m ceiling
   to 2 m before the scale factor was applied.

Each now has a regression test that asserts the failure as well as the fix,
so a guard cannot silently stop testing anything.

### One documented deviation from the plan

`objects.MERGE_CONTAINMENT` adds containment alongside §3.5's specified
`IoU > 0.3` for deciding whether two tracks are the same object. IoU alone
does not do the job the plan asks of it: a partial view is *contained* in the
full view rather than similar to it, and a 0.4 m glimpse of a 2 m sofa scores
0.09, so it survives as a second object inside the first. E6 is where this
gets measured against real annotations. The reasoning is recorded at the
constant.

### One thing the plan leaves implicit, now decided

§3.7's confidence bands are derived from the spread between scale sources. A
*single* source has nothing to disagree with, so its spread is zero and the
band would read `high`. One unchecked estimate is exactly what the confidence
field exists to warn about, so a lone source is capped at `medium`; only a
user measurement earns `high` on its own.

---

## Definition of done (from the plan)

| Clause | State |
|---|---|
| The go/no-go criteria in §3.10 are evaluated and documented | ❌ blocked — needs the bake-off |
| One reconstruction model and one geometry path chosen | ❌ blocked (D1, D2 stay open) |
| Schema-valid RoomModels for ≥ 80% of captures | ❌ not measurable — no captures have been run |
| Median uncalibrated wall-length error ≤ 5% | ❌ blocked (E2 is a Tier B experiment anyway) |
| p95 time ≤ 15 min, cost ≤ $1/scan, measured | ❌ blocked — needs a GPU to measure on |

**Phase 1 is not complete and should not be recorded as complete.** No
acceptance criterion in §8 has been met, because every one of them is a
measurement on real captures.

---

## What only you can do

These are the blockers, in the order they unblock things.

1. **Infrastructure accounts** (§8: "Phase 1 opens by doing the infrastructure
   setup moved out of Phase 0"). The runbook is written and waiting in
   [`infrastructure-setup.md`](./infrastructure-setup.md): Modal workspace, R2
   dev/prod buckets, Sentry. Modal needs a card. Supabase is not needed until
   Phase 5.
2. **A GPU.** This machine has no NVIDIA GPU (`nvidia-smi` is not installed;
   it is a Windows 10 laptop). §3.9 wants a 48 GB L40S or 80 GB A100 per job,
   which is what the Modal account is for. D15 ("local GPU for development")
   resolves to *no* on this machine.
3. **The Hugging Face token.** `phase-0-progress.md` records `HF_TOKEN` as
   being in `.env.local`, but **there is no `.env.local` in this working
   copy** — only `.env.example`. The gated checkpoints
   (`facebook/VGGT-1B-Commercial`, `facebook/sam3`) were approved on
   2026-09-19, so this is a matter of restoring the file, not re-requesting
   access.
4. **The ARKitScenes video assets.** `eval/captures/` does not exist here. The
   13 converted ground-truth files are committed and valid, but the `.mov`
   inputs they pair with are not (they are gitignored, correctly — they are
   large and not source). Phase 0's own notes flag this: of the scenes
   checked, one had ground truth but no `.mov`, one had the `.mov` but no
   depth, and one directory was empty. Follow
   [`eval/TIER_A_GUIDE.md`](../eval/TIER_A_GUIDE.md) to re-pull all five asset
   types per scene.

Nothing in this list can be worked around from inside the repository, and none
of it is a judgement call — it is access, hardware and a card.

---

## What Phase 2 should know

- **S6 expects approximately-metric input.** Its thresholds are metric (2 cm
  grid cells, a 0.3 m wall band, a 250 mm minimum wall), so S5 must apply a
  provisional metric scale before S6 runs. S7 then refines it and S9 applies
  the result. This is what makes `rescale_scan` (§7.1) a cheap CPU re-run.
- **The evaluation adapter and S6 must stay different.**
  `eval/adapters/geometry.py` traces a floor raster; S6 fits lines to wall
  evidence and assembles the plan from where they cross. If either is ever
  changed to match the other, E4 silently starts scoring a method against
  itself. `TIER_A_GUIDE.md` already flags the residual circularity.
- **gltfpack is not wired up.** §3.7 calls for `gltfpack -cc -tc`. It is a
  binary belonging in the worker image and a packaging step rather than a
  modelling one — a flat-coloured Tier 0 shell is a few thousand untextured
  triangles and lands far inside budget without it, which `check_budget`
  proves per scan rather than assuming. Revisit for Tier 1.
- **Appearance sampling is stubbed.** `shell.appearance()` returns the neutral
  Tier 0 defaults the mesh is built with. Sampling real wall and floor colours
  from the keyframes (§3.7 Tier 0) needs S2's full-resolution frames and is
  Phase 2 work.
