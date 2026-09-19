# Capture guide — Tier B self-capture

`implementation-plan.md` §3.10, Tier B. **This is deferred until after the Phase 1 gate**
(revised 2026-09-17) and is *not* a Phase 0 task.

Phase 0's evaluation set is Tier A: **ARKitScenes** scenes converted by
[`adapters/arkitscenes.py`](adapters/arkitscenes.py) — free, open download, no measuring.
Tier A answers E1, E4, E6 and E7.

Two things Tier A cannot settle land here:

- **How the pipeline behaves on video a user shot following our capture coach** — so
  **E2 (uncalibrated scale), E3 (one-measurement calibration) and E8 (sloppy captures)**.
- **Openings.** ARKitScenes annotates 17 furniture classes and no doors or windows, so
  **E5 has no Tier A ground truth at all** and is decided here too.

Doing this after the gate means it is only paid for once the pipeline has already proven
itself on public data.

**Scope: 5–8 rooms in our own homes.** Not 12–15, and nobody else's flat. Five to eight
rather than three to five because E5 needs enough doors and windows to compute a recall
from. A tape measure or a phone measuring app is fine; a laser measure is a nice-to-have,
not a prerequisite — and doors and windows are the easiest things in a room to measure.
Record whichever you used in `instrument` so a systematic error can be traced later.

---

## What you need

- Something to measure with. A laser measure is easiest; a tape measure is fine for the
  rooms in your own home, especially with someone to hold the other end.
- **A phone.** A second one of a different OS is a bonus, not a requirement. Record the
  model and OS version either way.
- Paper and a pen for the floor plan sketch.
- 10–15 minutes per room once you have the routine.

## Room roster

5–8 rooms from your own home. Pick for *variety*, not count — one of each shape you have,
and favour rooms with more than one window. Tier A already covers the trait spread; what
these add is our own capture style, and every door and window we can measure.

| Useful if you have one | Why | Room |
| --- | --- | --- |
| Living room ×2–3 | The primary use case | |
| Bedroom ×2–3 | Small, furniture against every wall | |
| Home office | Desk against a wall, monitor clutter | |
| Small kitchen / dining | Reflective surfaces, tight walkways | |
| **Open-plan space** | Boundary becomes `open` segments (§3.11) | |
| **Cluttered room** | Furniture hiding the floor-to-wall line | |
| **Sparse white-walled room** | Low texture; the hardest case for reconstruction | |
| L-shaped or non-rectangular | Corner count, polygon IoU (E4) | |
| Many openings | Door/window recall (E5) | |
| Narrow room | Walkway validation downstream | |

Fill in the right-hand column as you go. Anything you do not have, Tier A already covers.

## Per room: three captures

| Capture | Phone | Style | Orientation |
| --- | --- | --- | --- |
| `-a` | whichever you own | careful | landscape |
| `-b` | a second phone *if you have one* | careful | landscape |
| `-c` | either | **deliberately sloppy** | portrait |

`-b` is optional: it separates device effects from capture effects, which is nice to have
and not worth borrowing a phone for. `-a` and `-c` are the ones E8 needs.

**Careful** follows §3.2: chest height, walk the perimeter slowly facing inward, keep both
the floor-to-wall and ceiling-to-wall lines in shot, end where you started, lights on and
blinds open. 45–90 seconds.

**Sloppy is not "bad data" — it is E8**, which decides how strict the capture coach has to
be. Do it properly: fast pans, portrait, don't bother keeping the ceiling line in frame,
walk it in 25–35 seconds. Be consistently sloppy rather than randomly so.

Name the files `gt-0NN-a.mp4` and put them in `eval/captures/` (gitignored — they are large
and they are the inside of your home).

## Measuring: what and in what order

Work around the room in one direction so nothing is missed. **Millimetres, integers** (§2.4).

1. **Ceiling height.** Once, away from any bulkhead. If the ceiling is sloped, note it and
   pick the room's dominant height — §3.11 already says sloped ceilings are approximated.
2. **Every wall length**, going around in order. Name them `W1`, `W2`, … in the same
   direction as the floor polygon.
3. **Every door and window**: which wall, the offset from that wall's start to the opening's
   near edge, the width, the sill height (0 for a door), and the height.
4. **Three furniture pieces**: label, footprint (width × depth), height. Three is enough for
   E6; pick the largest ones.
5. **A photo of a hand-drawn floor plan.** Sketch it, write the measurements on it,
   photograph it. This is what saves you when a number in the YAML looks wrong later.
6. **Floor polygon**, if you can reconstruct it from the wall lengths: CCW `[x, z]`,
   centroid at the origin. **Optional but wanted — without it E4 cannot be scored at all**,
   and E4 is one of the go/no-go gates. For a rectangular or L-shaped room this is just
   arithmetic on the wall lengths.

### Getting the offsets right

`offset_mm` is measured **from the start of the wall**, in the same direction the walls are
numbered, to the **near edge** of the opening — not to its centre. Getting this backwards on
one wall is the most common way a ground-truth file ends up quietly wrong, and the validator
can only catch it when the opening runs off the end.

## Writing it up

Copy [`ground_truth/gt-000-example.yaml`](ground_truth/gt-000-example.yaml) to the next
free id and replace every number. Add `source: {tier: B}` so the validator knows to require
a sloppy capture (Tier A rooms are exempt, since the dataset shot those). Fill in
`measured_on`, `measured_by` and `instrument` (whatever you actually measured with) — in six
months you will want to know which numbers came from which tool.

Then, **before you leave the room if you can**:

```sh
uv run python -m eval.validate_ground_truth
```

It checks the schema, then the things a schema cannot: duplicate ids, openings that run past
the end of their wall, and a polygon whose perimeter disagrees with the measured wall
lengths by more than 2%. That last check is the one that catches a transposed digit, and it
is worth 30 seconds while the room is still in front of you.

`gt-000-example.yaml` is synthetic. **Delete it once `gt-001` onwards exist** — it says so
at the top of the file. The harness's tests use the committed dummy run instead, so nothing
breaks when it goes.

## When the dataset is in

```sh
# Once a pipeline exists, score a run:
uv run python -m eval.harness --run runs/<name>
uv run python -m eval.harness --run runs/<name> --calibrated   # E3
```

Reports land in `eval/reports/<name>/` as `metrics.csv` and `report.md`, with the E2/E4/E5
gate verdicts at the top.

## Why Tier A is not enough on its own

ARKitScenes (Tier A) is the primary Phase 0 yardstick and it is a good one: FARO laser truth
paired with a real iPad capture of the same room. But those captures were shot by dataset
collectors on their own protocol, not by a user following our capture coach around their
living room. Scale, calibration and sloppiness are properties of *how the video was shot*,
which is exactly what Tier A holds constant and we do not — and it annotates no openings.

That is the whole reason these rooms exist, and it is why E2, E3, E5 and E8 wait for them.
Dataset terms for both tiers are recorded in
[`docs/model-licenses.md`](../docs/model-licenses.md) — evaluation-only, never shipped,
treated like a non-commercial checkpoint.
