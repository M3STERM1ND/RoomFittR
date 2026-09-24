# Phase 1 bake-off — interim report

`implementation-plan.md` §3.10 / §8 Phase 1. Written 2026-09-23.

**This is not the go/no-go report.** It records the first end-to-end runs the
project has ever done, the metrics they produced, and one finding that has to
be resolved before E2 can be evaluated at all. The verdict §8 asks for needs
the full dataset and Tier B, neither of which exists yet.

---

## 1. What actually ran

Four ARKitScenes captures, S1–S9 end to end, real models on real GPUs, with
artefacts written to R2. No mocks, no fallbacks, no synthetic geometry.

| Stage | Where | What |
| --- | --- | --- |
| S1–S2 | this machine | ffmpeg decode, 64 keyframes at 518 px |
| S3 | Modal L40S | **VGGT-1B-Commercial**, the gated commercial checkpoint |
| S4 | Modal L40S | **SAM 3**, §3.5's prompt groups verbatim |
| S5–S9 | this machine | alignment, geometry, scale, objects, shell |
| artefacts | **Cloudflare R2** | `rooms/{room_id}/scans/{scan_id}/{version}/{stage}/…` |

Every produced `RoomModel` validates against
`packages/schemas/src/room-model.schema.json`, and every shell exports as a
valid binary glTF well inside §9.2's 3 MB budget.

## 2. Runtime and memory (E7, partial)

| Capture | S1–S2 | S3 on GPU | S3–S9 total | Peak VRAM |
| --- | --- | --- | --- | --- |
| gt-012-a | 11.8 s | 21.0 s | 269.9 s | 11.40 GB |
| gt-005-a | 15.1 s | 21.3 s | — | 11.40 GB |
| gt-008-a | — | 21.4 s | — | 11.40 GB |

**E7 is comfortably met on the axes measured.** §3.9 budgets p95 ≤ 15 min and
one GPU; the worst run is under 5 minutes wall-clock and 11.4 GB peak, against
an L40S's 46 GB. At L40S pricing that is roughly **$0.15 per scan** against the
≤ $1 budget.

Two caveats. The 270 s total is dominated by SAM 3, which runs every prompt
against every frame — 12 frames × 30 prompts here, and the full budget would be
64 × 30. And the A100-80 path has not been timed, so the E7 comparison §3.10
asks for is half done.

## 3. Scale: the circularity is fixed, the accuracy is not

**Then.** gt-005, gt-008 and gt-012 are rooms of 3.67, 23.96 and 8.53 m2, and
all three reported an identical 2564 mm ceiling and an identical 1.0684 scale
factor. Identical outputs from different inputs is what made it worth chasing.
Two circular paths, both ours: `_provisional_scale` normalised an up-to-scale
reconstruction's vertical extent to 2590 mm so S6's metric thresholds would
apply, S6 then "measured" a 2590 mm ceiling *by construction*, and S7 fused
that back in as evidence. Removing it changed nothing, because every fitted
door reported exactly 2030 mm -- the door prior itself.

**Now.** MapAnything (candidate A) is wired and is genuinely metric: its
`depth_z` comes back in metres, its scale correction is **1.0000**, and the
recovered ceilings now *differ between rooms* -- 3044, 2968, 3240, 3285 mm.
The constant is gone and scale is being measured rather than assumed.

**It is measured, and it is wrong.** Against truth ceilings of 2820, 2479 and
2529 mm, those are 8-20% high, and E2's median wall error is 180% -- against a
criterion of 5%. The error is no longer an artefact of the pipeline's own
assumption; it is the reconstruction's, which is a better problem to have and
still a failing one.

## 3a. E1: the first real candidate comparison

Four captures, same S5-S9 code, same frames, same prompts. This is what the
adapter boundary in 3.4 was for.

| | **B: VGGT-1B-Commercial** | **A: MapAnything** |
| --- | --- | --- |
| GPU time (64 frames) | **21 s** | 38-69 s |
| Peak VRAM | **11.4 GB** | **36.2 GB** |
| Scale | up to scale, no anchor | **metric, factor 1.0** |
| Floor recall vs truth | 49-58% | **73-100%** |
| Floor area vs truth | 0.7-1.5x | 3.1-3.9x |
| E4 IoU >= 0.85 | 0 / 4 | 0 / 4 |
| E2 median wall error | 338% | **180%** |

**3.4's `[VERIFY]` on MapAnything's memory is answered: 36.2 GB at N = 64.**
That is inside an A100-80 and inside an L40S's 46 GB, but it is three times
VGGT's, and it grows with N -- the bake-off's N = 100 and 150 points are not
safe to assume.

The two models fail differently, and the difference is the useful part.
**VGGT sees half the room; MapAnything sees all of it and then three times as
much again.** MapAnything's recall is the harder thing to fix, so its failure
mode is the more promising one -- over-extension is a filtering problem,
missing geometry is not.

A first attempt at that filtering is in `pipeline._trim_outliers`, bounding
the floor plan by the camera path plus a margin (3.4 step 4, 3.11). It moved
gt-012's ceiling from 3350 to 3044 mm and E2's median from 197% to 180%. It is
not enough, and the remaining over-extension is the single clearest thing to
work on next.

## 4. Geometry (E4, one capture)

| | ground truth | predicted |
| --- | --- | --- |
| vertices | 18 | 5 |
| spans (mm) | 4080 × 5622 | 4286 × 2403 |
| area | 8.51 m² | 8.53 m² |

Polygon IoU **0.00**, so E4 fails on this capture. The X span matches within
5% and the area is within 0.3%, but the recovered footprint covers less than
half the room's depth: a 5-vertex box where the truth is an irregular
18-vertex space. Area agreeing while shape does not is a coincidence of two
errors, not a partial success.

One capture is not E4, which §3.10 scores over ≥ 80% of captures. It does
suggest the limiting factor is coverage rather than scale — 64 keyframes of a
37.8 s handheld pass did not see the whole room.

## 5. Objects (E6, not yet scored)

S4 produced 10, 49 and 61 tracked objects on the three captures. ARKitScenes
annotates 17 furniture classes, so E6 can be scored against
`annotation` — that harness path is not written yet.

On a single frame, SAM 3 found `floor` at score 0.84 and nothing for `sofa`,
`chair`, `table`, `wall`, `door` or `window` at threshold 0.4. The `floor` hit
matters structurally: S5's plane fit depends on it.

## 6. Three bugs the real runs found

Each was invisible to the synthetic end-to-end test and to every unit test.

1. **SAM 3's masks and VGGT's depth are different sizes.** 388×518 against
   392×518, because VGGT pads its input to a multiple of 14. The code skipped
   mismatched masks, which silently discarded *every* instance; the symptom
   was S5 refusing to orient the room because no floor had been segmented.
   Masks are now resized with nearest-neighbour.
2. **The same door was detected twice and fitted twice.** S4's tracking is IoU
   between adjacent frames, so a door that leaves the view and returns becomes
   two tracks. §3.6's sanity check correctly refuses a wall with overlapping
   doors. Duplicate openings on one wall are now merged by union of extent.
3. **The circular ceiling source**, above.

## 7. Verdict

**Phase 1 cannot be marked complete.** §3.10's go condition is that E1–E5 and
E7 pass.

| Experiment | State |
| --- | --- |
| E1 reconstruction bake-off | **partial** — A and B compared on 4 captures (§3a); needs C, D and all 13 |
| E2 uncalibrated scale | **measurable now, and failing**: 180% median against 5% (§3) |
| E3 one-measurement calibration | not run; needs E2's machinery working |
| E4 floor polygon | **fails**, 0 of 4 at IoU >= 0.85, both candidates |
| E5 openings | **no Tier A ground truth** — ARKitScenes has no door/window annotations (§3.10) |
| E6 furniture | objects produced; scoring path not written |
| E7 runtime and memory | **VGGT passes**; MapAnything measured at 36.2 GB (§3a) |
| E8 sloppy captures | needs Tier B |
| E9 browser recording | **passed** (Phase 0) |
| E10 appearance | needs five people |

## 8. What has to happen next

**Unblocked, in priority order:**

1. **Stop the floor plan over-extending.** MapAnything already recovers
   essentially the whole room; its polygon is then 3-4x too large because the
   reconstruction sees through doorways and windows. `_trim_outliers` is a
   start and is not enough. This is the one change that would move E4, and E4
   moving is what makes E2's wall metric mean anything.
2. **Profile at N = 100 and 150.** MapAnything is at 36.2 GB at N = 64, and
   3.9's budget is one GPU.
3. **The §3.2 size decision.** Nine of the 13 captures exceed `MAX_BYTES`
   (750 MB) and cannot run at all. The three options and their trade-offs are
   in `docs/phase-1-progress.md`; the bake-off cannot cover the dataset until
   one is chosen.
4. **E6's scoring path** against ARKitScenes' furniture annotations.

**Genuinely blocked on a person:**

- **Tier B captures** — 5–8 self-captured rooms. E2, E3, E5 and E8 are decided
  there (§3.10), so **three of the five experiments the go/no-go requires
  cannot be reached by any amount of code**.
- **E10** — five people scoring appearance side by side.
