# Tier A — assembling the Phase 0 evaluation set from ARKitScenes

`implementation-plan.md` §3.10 Tier A / §8 Phase 0. **This is the Phase 0 dataset task.**
Free, open download, **no application and no institutional email**. Tier B (our own rooms)
is deferred until after the Phase 1 gate — see [`CAPTURE_GUIDE.md`](CAPTURE_GUIDE.md).

Target: **12–15 scenes** converted to `eval/ground_truth/gt-0NN.yaml`.

<https://github.com/apple-aiml-research/ARKitScenes> — Apple moved this from
`apple/ARKitScenes`; both URLs resolve and the contents are identical.

---

## Why this one

ScanNet++, ZInD and Matterport3D all gate access behind academic credentials or an
institutional email. ARKitScenes does not: it is a plain `download_data.py`, no account, no
affiliation check. It is also the only *open* dataset that pairs **real commodity captures**
with **laser ground truth**:

- `highres_depth` — ground-truth depth projected from a **FARO laser scanner** mesh,
  1920×1440, uint16 millimetres. This is the yardstick.
- `lowres_wide.traj` — gravity-aligned ARKit camera poses.
- `mov` — the capture itself, which is pipeline input.
- `annotation` — oriented boxes over 17 furniture classes, which gives **E6** for free.

Its licence is also better than any academic dataset we looked at: Apple's terms are
non-commercial by default **but grant commercial use below a 700M-MAU threshold**. Recorded
in [`docs/model-licenses.md`](../docs/model-licenses.md) §4b.

**The one thing it lacks: doors and windows.** The taxonomy is 17 furniture classes with no
`door` or `window`, so **E5 has no Tier A ground truth** and moves to Tier B. The adapter
emits `openings: []` and says so in the file's notes — it does not guess at openings from
holes in the geometry, because that would be solving E5 in order to score E5.

---

## Step 1 — Pick scenes that can actually be converted

**Corrected 2026-09-19 after two failed downloads.** Not every scene works, and the
failures are silent — `download_data.py` skips a missing asset and exits 0, leaving a
directory that looks fine until the adapter refuses it. Two rules, both read off
`raw/metadata.csv` in the cloned repo:

1. **`is_in_upsampling` must be `True`.** That column indicates the scene has
   **`highres_depth`**, which is the FARO yardstick and the whole reason this dataset was
   chosen. `47333462` has it `False`: its download produced a `.mov` and no depth, so it
   cannot be converted at all.
2. **`fold` must match the `--split` you pass.** `41069021` — the id this guide used in
   its original example — is in **Validation**, so `--split Training` silently downloaded
   nothing and left an empty directory.

`is_in_threedod` should also be `True`; that is the 17-class furniture annotation, which
is E6's ground truth and (see step 3) the only machine-readable hint at what kind of room
a scene is.

```sh
# Training scenes with BOTH highres_depth and furniture boxes
awk -F, 'NR>1 && $4=="Training" && $6=="True" && $7=="True" {print $1}' raw/metadata.csv
```

```sh
git clone https://github.com/apple-aiml-research/ARKitScenes
cd ARKitScenes
python download_data.py raw --split Training --video_id 47332764 \
  --download_dir /data/arkitscenes \
  --raw_dataset_assets highres_depth lowres_wide.traj lowres_wide_intrinsics mov annotation
```

**Do not pull the whole thing** — the raw set is hundreds of gigabytes and we need five
asset types. Expect roughly **0.5–1 GB per scene**; four parallel downloads sustain about
3 MB/s, so a 12-scene set takes on the order of an hour.

**Verify every scene before converting**, because a partial download is silent:

```sh
for d in /data/arkitscenes/raw/Training/*/; do
  id=$(basename "$d")
  for a in highres_depth lowres_wide.traj lowres_wide_intrinsics "$id.mov" "${id}_3dod_annotation.json"; do
    [ -e "$d/$a" ] || echo "$id MISSING $a"
  done
done
```

## Step 2 — Check the layout and the conventions

The adapter's paths and its two coordinate conventions come from the published
documentation, not from a downloaded scene. So before converting anything:

```sh
uv run python -m eval.adapters.arkitscenes --scene /data/arkitscenes/raw/Training/47332764 --inspect
```

It writes nothing and prints:

```
  [found  ] frames dir: 47332764_frames
  [found  ] highres_depth: 682 frames
  [found  ] lowres_wide.traj: 1194 poses
  [found  ] intrinsics: 47332764_43856.438.pincam -> (256, 192, 213.835, 213.835, 128.155, 96.0334)
  [found  ] furniture boxes: 3

  fusing a sample to check the coordinate conventions...
  173,204 points
  extent mm: x -852..3020  y -435..2346  z -2994..995
  vertical span: 2781 mm  (expect roughly a room height)
  floor plane: 33,403 pts at y~-402 mm; ceiling: 2,545 pts at y~2319 mm
  implied ceiling height: 2733 mm

looks usable
```

**What to check:** the vertical span should be roughly a room height (2–3 m) and the implied
ceiling height should be plausible.

The intrinsics line showing `(256, 192, …)` against 1920×1440 depth is expected, not a bug:
`wide_intrinsics` is not a downloadable asset, and `backproject` rescales the `lowres_wide`
intrinsics to the depth resolution.

**The up-axis is detected per scene, not assumed.** ARKitScenes' world frame is z-up; an
earlier version of this adapter hard-coded y and silently produced rooms of the wrong size.
See `detect_up`. If the span is still nonsense, the remaining assumption is whether `.traj`
stores world-to-camera (the adapter assumes it does and inverts it).

The adapter is built to fail loudly here rather than produce a plausible-looking wrong
yardstick: if the cloud is not a room, `split_horizontal_surfaces` raises.

## Step 3 — Convert

```sh
uv run python -m eval.adapters.arkitscenes \
  --scene /data/arkitscenes/raw/Training/47332764 \
  --room-id gt-001 \
  --room-type living \
  --traits cluttered
```

```
wrote eval/ground_truth/gt-001.yaml
  4 walls, ceiling 2699 mm, 2 furniture pieces, 0 openings (E5 is Tier B)
```

`--stdout` previews without writing. `--frame-stride` / `--pixel-stride` trade speed for
density; the defaults (every 10th frame, every 8th pixel) are plenty.

### Choosing `--room-type` and `--traits`

The adapter cannot know these — you set them from looking at the scene. They are what makes
the set cover hard cases on purpose rather than being 15 plain rectangles.

- `--room-type`: `living` · `bedroom` · `office` · `kitchen_dining` · `open_plan` · `other`
- `--traits`: `cluttered` · `sparse` · `white_walls` · `large_mirror` · `glass` · `dark` ·
  `bay_window` · `sloped_ceiling` · `non_manhattan`

Aim for the §3.10 spread: several living rooms and bedrooms, an office, a kitchen/dining
area, **at least one open-plan space, one cluttered room and one sparse white-walled room**,
plus an L-shaped one and a narrow one. Those last two are what E4 actually exercises.

## Step 4 — Validate

```sh
uv run python -m eval.validate_ground_truth
```

Tier A rooms are exempt from the "needs a sloppy capture" rule (we did not shoot them) and
from needing openings.

## Step 5 — Wire up the video

```sh
mkdir -p eval/captures
ln -s /data/arkitscenes/raw/Training/47332764/47332764.mov eval/captures/gt-001-a.mov
```

**On Windows, do not use `ln -s`.** Git Bash falls back to *copying* when it cannot create a
symlink, so each capture silently becomes a full 1 GB duplicate, and native symlinks require
Administrator. Use a hard link instead - same volume, no admin, no duplication:

```powershell
New-Item -ItemType HardLink -Path eval\captures\gt-001-a.mov `
         -Target D:\arkitscenes\raw\Training\47332764\47332764.mov
```

`eval/captures/` is gitignored; the YAML is what gets committed.

## Step 6 — Repeat to 12–15, then commit

```sh
uv run python -m eval.validate_ground_truth   # "12/12 ground-truth files valid"
git add eval/ground_truth
```

**Do not delete `gt-000-example.yaml`.** This guide used to say to remove it once real
files existed. That is wrong: it is the ground truth the committed dummy run
(`eval/runs/dummy`) is scored against, so `eval/tests/test_harness_end_to_end.py` fails
without it. It is a harness fixture that happens to live in this directory, not a Tier A
room, and `validate_ground_truth` reports it separately from the scene count.

---

## Accuracy, honestly

The derived layout is the yardstick E2 and E4 are scored against, so its own error comes
straight off their budget. Measured on synthetic rooms of known size
(`tests/test_arkitscenes_adapter.py`):

| Case | Wall error |
| --- | --- |
| Clean dense cloud | **0.00%** |
| Full render → depth frames → fusion → layout round trip | **< 1%** (4637/3799/4586/3799 mm against a true 4600×3800) |
| Deliberately harsh: a third of points dropped, 8 mm scatter | 1.5% median, 3.7% worst |

Three caveats that are real, not hedging:

1. **Spot-check the first few floor polygons by eye.** The trace handles furniture shadows,
   unscanned bands against walls, and scan leaking into a disconnected neighbouring room —
   but a room whose floor is almost entirely hidden is beyond it. It raises rather than
   guessing when it cannot see enough floor.
2. **An open door is a known weakness.** Floor is *continuous* through a doorway, so a scan
   that ran into the hall gives a tongue of floor that no connected-component test can
   separate. The 8 m depth cap bounds it, but if a polygon looks like it grew an arm, that
   is why — drop that scene or note it.
3. **Ceiling height needs the ceiling to have been scanned.** Captures that never look up
   are refused with a clear message rather than given a made-up height.

## How circular is this?

Worth stating plainly, because it affects how much E4 proves. The adapter's input is dense,
complete, metric and gravity-aligned; S6's input is a sparse, noisy, scale-ambiguous
reconstruction from phone video. They share no code. But the floor here is found
*geometrically*, which is the same class of operation S6 performs — so Tier A measures S6
against a far better instrument, not against an independent oracle. **Tier B, measured by
hand, is the independent check.** That is one of the reasons it still has to happen.
