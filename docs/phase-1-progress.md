# Phase 1 progress

Live status for `implementation-plan.md` §8 Phase 1 (Video → 3D Room Proof of
Concept — **the gate**). Updated as work lands. **If work stopped partway, the
"Stopped at" line below is the resume point.**

**Stopped at:** _**Both candidates run end to end on real captures; E4 is the
blocker, 2026-09-23.** MapAnything (candidate A) is wired and is genuinely
metric, so the scale circularity that made every room 2564 mm tall is gone --
ceilings now differ between rooms. E7 passes for VGGT and 3.4's `[VERIFY]` on
MapAnything's memory is answered at **36.2 GB**. **E2 and E4 both fail**: the
floor plan over-extends 3-4x because the reconstruction sees through doorways
and windows, and until that is fixed the wall metrics measure the
over-extension rather than the room. Full numbers in `eval/reports/phase1.md`.
Phase 1 is **not** complete: E2 and E4 fail on Tier A, and E2/E3/E5/E8 are
ultimately decided on Tier B captures that no code can produce._

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
| 11 | Run the bake-off E1–E8, write `eval/reports/phase1.md`, make the go/no-go call | **blocked** | This is the gate. The dataset is now in place (§"Tier A capture inputs"); still needs tasks 2, 3, 4, a GPU, and a decision on the §3.2 size limit. |

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

---

## The Modal image blocker, and what it actually was

Worker image builds ran past forty minutes and were killed with `Image build
terminated due to external shut-down`. That message reads like an
infrastructure fault and is not one.

**Cause: `pip_install` on the CUDA wheels.** torch and its nvidia-* dependencies
are about 2.5 GB, and pip's resolve-then-download pass over them ran long
enough for the builder to reclaim the task. The apt output visible when the
build died was simply the last thing printed, which is what sent the first two
investigations toward ffmpeg's dependency tree.

**Fix: `uv_pip_install`.** The same layers build in **under 15 seconds**, and
the whole four-image app now deploys in **83 s** where it previously never
finished. Isolating it needed one small image at a time rather than repeated
40-minute deploys; the intermediate scripts are not kept, since the conclusion
is here and the images carry the fix.

Three smaller things the same investigation turned up:

1. **VGGT pins `numpy<2`; the pipeline package asks for `>=2.0`.** They cannot
   share an environment, and they do not have to: the GPU container returns
   plain arrays over the wire and S5-S9 run in `cpu_image` on numpy 2. The
   constraint is relaxed in `vggt_image` only. Relaxing the *pipeline's* floor
   to suit a model would let a model's dependencies dictate the whole stack,
   which R15's monthly model churn makes a bad trade.
2. **VGGT imports torchvision**, which was not installed. It is now pinned and
   installed in the same layer as torch, so both resolve against one index in
   one solver pass.
3. **Modal's CLI cannot print to a cp1252 console.** `PYTHONIOENCODING=utf-8`
   is needed on Windows or every invocation dies on a Unicode tick mark.

## What has actually run on a GPU

Measured 2026-09-23, on real `gt-012-a` keyframes produced by S1-S2 on this
machine. **Not adapter or import tests -- real model inference.**

| | VGGT-1B-Commercial | SAM 3 |
|---|---|---|
| GPU | L40S (46 GB, capability 8.9) | L40S |
| Load | 40.8 s cold | 25.0 s |
| Inference | 1.8 s / 8 frames; **17.75 s / 16 frames** | 2.3 s for 7 prompts on 1 frame |
| Peak VRAM | 9.05 GB / **9.38 GB** | **2.12 GB** |
| Gated download | via `roomfittr-hf` secret | via `roomfittr-hf` secret |

Both are far inside 3.9's budget, and bf16 is native on this card.

**Three adapter assumptions were confirmed against the real output**, which is
the point of having run it rather than reasoned about it:

- VGGT returns `extrinsic` as `(1, N, 3, 4)` **world-to-camera**, so the
  inversion in `adapters/vggt.py` is required. The deployed function's poses
  come back `(16, 4, 4)` and `backends.validate` accepts them.
- `depth` arrives as `(1, N, H, W, 1)` and `depth_conf` as `(1, N, H, W)` --
  different ranks, which is why the adapter squeezes conditionally.
- **`depth_conf` is unbounded, measured 1.09 to 13.48**, not the 0-1 the
  contract promises. `_normalise_confidence` is not defensive coding; without
  it every downstream confidence threshold would be meaningless. After the
  round trip the confidence range is 0.082 to 1.0.

**What SAM 3 found on one frame:** `floor` only, at score 0.84, mask shape
`[388, 518]` matching the frame exactly. `sofa`, `chair`, `table`, `wall`,
`door` and `window` all returned nothing at threshold 0.4 on that frame. One
frame of a kitchen is not evidence about E6 either way, but the `floor` hit
matters structurally: S5's plane fit needs floor points, and this is where
they come from.

## What is still not proven

- **No full-dataset run.** One capture, 16 frames, one candidate. E1 compares
  three models across 13 scenes.
- **MapAnything has never been loaded.** Its numpy floor is unchecked against
  the pipeline's; if it also pins `<2`, the VGGT reasoning applies.
- **No artefact has been written to R2**, because R2 does not exist yet.
- **S1-S9 has not run end to end on a real capture.** The chain is tested
  against a synthetic reconstruction and each half is proven on real data, but
  the two have not been joined in one run.

## What only you can do

These are the blockers, in the order they unblock things.

1. **Infrastructure accounts** (§8: "Phase 1 opens by doing the infrastructure
   setup moved out of Phase 0"), per
   [`infrastructure-setup.md`](./infrastructure-setup.md). **Modal is done**
   (2026-09-20): workspace `tejas-15913`, `main` + `dev` environments,
   `roomfittr-hf` secret in both, tokens in `.env.local`. Note the TLS
   workaround recorded in that runbook — AVG's HTTPS interception breaks the
   Modal client until its root is added to certifi, and `uv tool upgrade modal`
   undoes the fix. **Sentry is also done** (2026-09-20): org `roomfittr`, both
   projects created, all six variables in `.env.local`, verified by a real
   source-map upload and a live event to each DSN. **Still outstanding: R2
   dev/prod buckets**, which is now the only infrastructure item left and the
   one that blocks the pipeline, since it has nowhere to write artefacts
   without it. Supabase is not needed until Phase 5. Two settings remain unset:
   a **spend limit** at modal.com/settings/usage, and Sentry's **server-side
   data scrubber**.
2. **A GPU.** This machine has no NVIDIA GPU (`nvidia-smi` is not installed;
   it is a Windows 10 laptop). §3.9 wants a 48 GB L40S or 80 GB A100 per job,
   which is what the Modal account is for. D15 ("local GPU for development")
   resolves to *no* on this machine.
3. ~~**The Hugging Face token.**~~ **Resolved 2026-09-20.** `.env.local` exists
   on this machine and `HF_TOKEN` is present. Both gated checkpoints were
   verified by authenticated fetch, not just by the approval page:
   `facebook/VGGT-1B-Commercial` and `facebook/sam3` both return HTTP 200 on a
   file download with this token (a `read` token on account `hungwa`). The
   token is also loaded into the `roomfittr-hf` Modal secret in `main` and
   `dev`, so the worker image can reach the weights at build time.
4. ~~**The ARKitScenes video assets.**~~ **Resolved 2026-09-20** — see
   §"Tier A capture inputs" for the verification. All 13 `.mov` files are in
   `eval/captures/`, hardlinked to `D:/arkitscenes/raw/Training/`, and each one
   is confirmed to be the *same inode* as its scene's source file, which is
   identity rather than a size comparison. They remain gitignored, correctly.

Nothing in this list can be worked around from inside the repository, and none
of it is a judgement call — it is access, hardware and a card.

---

---

## Tier A capture inputs

Verified 2026-09-20. All 13 selected ARKitScenes scenes have their `.mov` in
`eval/captures/`; **nothing needed downloading**, and the scenes were already
present under `D:/arkitscenes/raw/Training/` (38 scenes, 32 GB, of which these
13 are the selected subset).

| room | scene_id | duration | display | codec | rot | MB |
|---|---|---|---|---|---|---|
| gt-001 | 41048223 | 96.1 s | 1440×1920 | hevc | 90 | 842 |
| gt-002 | 41048225 | 127.3 s | 1440×1920 | hevc | 90 | 1115 |
| gt-003 | 41048229 | 93.6 s | 1440×1920 | hevc | 90 | 821 |
| gt-004 | 42444477 | 168.7 s | 1920×1440 | hevc | 0 | 1480 |
| gt-005 | 42898342 | 47.8 s | 1920×1440 | hevc | 0 | 421 |
| gt-006 | 42898745 | 98.5 s | 1920×1440 | hevc | 0 | 864 |
| gt-007 | 43828231 | 144.6 s | 1440×1920 | hevc | 90 | 1268 |
| gt-008 | 44358360 | 78.5 s | 1920×1440 | hevc | 180 | 689 |
| gt-009 | 45261292 | 61.6 s | 1920×1440 | hevc | 0 | 540 |
| gt-010 | 45663347 | 197.1 s | 1920×1440 | hevc | 180 | 1729 |
| gt-011 | 47331686 | 91.1 s | 1920×1440 | hevc | 0 | 799 |
| gt-012 | 47332705 | 37.8 s | 1920×1440 | hevc | 0 | 332 |
| gt-013 | 47332764 | 120.0 s | 1920×1440 | hevc | 0 | 1052 |

**11.95 GB logical, 0 bytes additional.** Every file is a hardlink to the
ARKitScenes copy, so the two paths share one set of blocks. `ingest.probe()`
reads all 13 — they are real, undamaged HEVC, all at 60 fps, and none carries
an audio track.

Three findings, in descending order of how much they matter.

**1. Nine of the 13 exceed the §3.2 upload limit, so `ingest.validate()`
rejects them.** `MAX_BYTES` is 750 MB; these captures run 332–1729 MB because
ARKitScenes records at roughly 70 Mbit/s, far above what a phone upload
produces for the same duration. Only gt-005, gt-008, gt-009 and gt-012 pass.
This is not a broken limit and not broken data — it is the product's
*user-upload* contract being applied to *evaluation* input, which is a
different thing. **It needs a decision before E1–E8 can run on the full set**,
and the options are not equivalent:

- Have the eval harness call `probe()` and skip `validate()`, on the grounds
  that the size cap exists to bound what a user can upload, not what the
  reconstruction can read. Cheapest, and keeps the eval measuring the pipeline
  rather than the upload gate.
- Transcode the captures down to a phone-like bitrate first. More faithful to
  what production will actually see — a real upload is re-encoded by the phone
  — but it changes the pixels the gate is measured on, which makes E1's
  numbers harder to attribute.
- Raise `MAX_BYTES`. Worst of the three: it changes a product limit to suit a
  test fixture.

Left undecided deliberately; whoever runs the bake-off should pick and record
the choice in `eval/reports/phase1.md`.

**2. Every ground-truth file pointed at a capture that does not exist.** All 13
carried `file: captures/gt-tmp-a.mov` — a placeholder from a batch run where
`room_id` and `capture_id` were patched afterwards and `file` was missed. The
adapter has always emitted the right value
([`arkitscenes.py`](../eval/adapters/arkitscenes.py) line 469) and its unit test
asserts `captures/gt-001-a.mov`, so nothing caught the drift between what the
adapter produces and what was committed. Fixed in place; all 54 eval tests pass.
Worth noting the shape, because it is the same one the rest of this file keeps
recording: a generated file edited by hand, where the generator and the artefact
then disagree in a way no structural check looks at.

**3. `ffprobe` was not resolvable on this machine.** The dev dependency group
was not installed, so `static-ffmpeg` — the Windows fallback in
`ingest._resolve` — was absent and every probe raised `INTERNAL`. `uv sync`
fixes it; the binaries land in `.venv/` on D:.

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
