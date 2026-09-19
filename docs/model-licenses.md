# Model & dependency licence register

Required by `implementation-plan.md` §1.2 (Licensing Gate) and §8 Phase 0 task 7.

**The rule this file enforces.** RoomFittR is personal now and may grow, so **only models
whose _weights_ permit commercial use are eligible for the production pipeline.**
Non-commercial models may be used as evaluation baselines in Phase 1 and nowhere else.
A permissive *code* licence proves nothing — every model below where code and weights
diverge is a case where the code is Apache-2.0 and the weights are not.

**Verification status.** Every row marked ✅ was read from the source linked in that row on
**2026-09-17**. Rows marked ⏳ are unverified and must not be relied on. Model licences
change between checkpoints and between releases, so **re-verify a row before adopting the
model, not before merging this file.**

---

## 1. Verdicts at a glance

| Verdict | Models |
| --- | --- |
| ✅ **Production-eligible** | MapAnything (`-apache` checkpoint only), VGGT-1B-Commercial, Depth Anything 3 (BASE / SMALL / METRIC-LARGE / MONO-LARGE only), SAM 3, SAM 2, Grounding DINO, MoGe-2, SigLIP 2, COLMAP, GLOMAP |
| ⚠️ **Eligible with a condition** | LaMa (training-data provenance), FFmpeg (build flags) |
| ❌ **Evaluation baseline only** | MapAnything default checkpoint, VGGT-1B (original), Depth Anything 3 LARGE / GIANT / NESTED, SpatialLM (all variants) |

Three of these are traps worth stating plainly:

1. **Depth Anything 3's best checkpoints are non-commercial.** `DA3-LARGE` and `DA3-GIANT`
   are CC BY-NC 4.0. The Apache-2.0 set is `DA3-BASE`, `DA3-SMALL`, `DA3METRIC-LARGE` and
   `DA3MONO-LARGE`. Since §3.7 wants DA3 for *metric* depth, `DA3METRIC-LARGE` is the
   interesting one and it is Apache-2.0 — but the E-series bake-off must be run on the
   checkpoint we can actually ship, not on GIANT.
2. **SpatialLM is entirely non-commercial**, on both 1.0 and 1.1, Llama and Qwen variants,
   because the point-cloud encoder (SceneScript / Sonata) is CC BY-NC 4.0 regardless of the
   base LLM. §3.6's "SpatialLM candidate learned path" is therefore a **baseline only**: E4
   can measure it, the product cannot use it. The classical geometry path in §3.6 must
   remain the guaranteed path.
3. **MapAnything and VGGT both ship two checkpoints with the same API**, differing only in
   training data and licence. Downloading the wrong one is a single-character mistake with
   a licence consequence, which is why §5 below pins exact repo ids.

---

## 2. Reconstruction & geometry models

| Model | Code licence | Weights licence | Commercial? | Notes / restrictions | Src |
| --- | --- | --- | --- | --- | --- |
| **MapAnything** | Apache-2.0 ✅ | **Two checkpoints.** `facebook/map-anything-apache` → **Apache-2.0**. `facebook/map-anything` → **CC BY-NC 4.0** | ✅ **only** via `-apache` | Identical API; they differ in training-data composition (6 datasets vs. 13). `-v1` variants of both exist. The HF→repo conversion script takes an `--apache` flag. | [repo](https://github.com/facebookresearch/map-anything) |
| **VGGT** | commercial-use-friendly (since Jul 2025) ✅ | **Two checkpoints.** `facebook/VGGT-1B-Commercial` → `vggt-aup-license`. `facebook/VGGT-1B` → non-commercial | ✅ **only** via `-Commercial` | Commercial checkpoint is **gated** — requires an access form, auto-approved, and HF auth to download. Licence **excludes military applications**. Similar performance to the original. Gating must be handled in the Modal image build (token in secrets), not at request time. | [repo](https://github.com/facebookresearch/vggt), [card](https://huggingface.co/facebook/VGGT-1B-Commercial) |
| **Depth Anything 3** | Apache-2.0 ✅ | **Per checkpoint.** Apache-2.0: `DA3-BASE` (0.12B), `DA3-SMALL` (0.08B), `DA3METRIC-LARGE` (0.35B), `DA3MONO-LARGE` (0.35B). CC BY-NC 4.0: `DA3-LARGE`, `DA3-GIANT`, `DA3NESTED-GIANT-LARGE` (and their `-1.1` refreshes) | ✅ **only** for the four Apache-2.0 checkpoints | Prefer `-1.1` suffixed checkpoints (retrained after a training bug) — **but every `-1.1` released so far is on the NC side**, so the commercially usable set has no `-1.1` refresh. Weigh that in E1. | [repo](https://github.com/ByteDance-Seed/Depth-Anything-3) |
| **SpatialLM** | 1.1 code (Pointcept) Apache-2.0; 1.0 CC BY-NC 4.0 | **CC BY-NC 4.0** for all released weights | ❌ | The NC term comes from the point-cloud encoder (SceneScript in 1.0, Sonata in 1.1), **not** the base LLM — so the Qwen-2.5 variant is NC despite Qwen being Apache-2.0, and the Llama variant adds the Llama 3.2 licence on top. **Baseline only (E4).** | [repo](https://github.com/manycore-research/SpatialLM), [card](https://huggingface.co/manycore-research/SpatialLM1.1-Qwen-0.5B) |
| **MoGe-2** | MIT ✅ (bundled DINOv2 under Apache-2.0) | **MIT** | ✅ | Cleanest licence in the stack. Good reason to keep it as the independent scale estimator (§1.1, E3). | [repo](https://github.com/microsoft/MoGe), [card](https://huggingface.co/Ruicheng/moge-2-vitl-normal) |
| **COLMAP** | new BSD ✅ | n/a (no learned weights) | ✅ | Repo warns the effective licence depends on how it is built: `ceres-solver`, `poselib`, `sift-gpu`, `vlfeat` are separately licensed. Pin and record the build config for the D baseline. | [repo](https://github.com/colmap/colmap) |
| **GLOMAP** | BSD-3-Clause ✅ | n/a | ✅ | Same build-dependency caveat as COLMAP. | [repo](https://github.com/colmap/glomap) |

## 3. Segmentation & vision models

| Model | Code licence | Weights licence | Commercial? | Notes / restrictions | Src |
| --- | --- | --- | --- | --- | --- |
| **SAM 3** | SAM License (Meta, rev. 2025-11-19) ✅ | SAM License, **gated** | ✅ | Grants a "non-exclusive, worldwide, non-transferable and royalty-free limited license" including derivative works. **No MAU cap and no naming requirement.** Conditions: acknowledge SAM Materials in publications; acceptable-use policy bans military/warfare, nuclear, espionage, weapons, ITAR-governed activity; must comply with US/UN/EU/UK trade controls. Checkpoints require an approved HF access request. | [repo](https://github.com/facebookresearch/sam3), [licence](https://raw.githubusercontent.com/facebookresearch/sam3/main/LICENSE) |
| **SAM 2** (fallback) | Apache-2.0 ✅ | **Apache-2.0** | ✅ | Checkpoints, demo code and training code all Apache-2.0. Demo fonts are SIL OFL 1.1 — irrelevant to us, we ship no demo. Strictly more permissive than SAM 3. | [repo](https://github.com/facebookresearch/sam2) |
| **Grounding DINO** (fallback) | Apache-2.0 ✅ | ⏳ not separately stated | ✅ (code); weights ⏳ | Repo is Apache-2.0; the README does not state a separate weights licence. Confirm at the release/checkpoint page before the fallback is wired in. | [repo](https://github.com/IDEA-Research/GroundingDINO) |
| **SigLIP 2** | Apache-2.0 ✅ | **Apache-2.0** | ✅ | Checked on `google/siglip2-base-patch16-224`; re-check if a different size is chosen for the `pgvector` embeddings. | [card](https://huggingface.co/google/siglip2-base-patch16-224) |
| **LaMa** | Apache-2.0 (Samsung Research) ✅ | Apache-2.0 for `big-lama` | ⚠️ **conditional** | **The weights licence is permissive but the training data is not.** `big-lama` was trained on **Places2**, whose terms restrict use to non-commercial research and education. The model licence and the data licence disagree, and that exposure sits with whoever ships it. Textured shells are Tier 2 in §3.7 and not on the V1 path, so this is deferrable — but it must be resolved (retrain on permissive data, or pick another inpainter) **before** the textured tier ships, not after. | [repo](https://github.com/advimman/lama), [licence](https://raw.githubusercontent.com/advimman/lama/main/LICENSE), [Places2 terms](http://places2.csail.mit.edu/download-private.html) |
| **Hunyuan3D** | ⏳ | ⏳ | ⏳ | §1.2 flags territorial restrictions (typically an EU/UK/Korea carve-out). **Not evaluated, not adopted.** Verify only if it is ever actually considered. | — |

## 4. Non-model dependencies worth a licence note

Everything else in §1.1 is standard permissive tooling (Open3D MIT, trimesh MIT, shapely
BSD-3, scipy BSD-3, pygltflib MIT, httpx BSD-3, Playwright Apache-2.0, meshoptimizer/gltfpack
MIT). Two are not routine:

| Dependency | Licence | Why it needs care |
| --- | --- | --- |
| **FFmpeg** ✅ | LGPL-2.1-or-later **by default**; becomes **GPL-2.0-or-later** if built with `--enable-gpl` or any GPL library, notably **libx264** | The worker image decodes and normalises uploads (§3.3); it does not need to *encode* H.264. Build **without** `--enable-gpl` and **without** `--enable-nonfree`, and keep libx264 out, so FFmpeg stays LGPL. If an encode path is ever needed, prefer a non-GPL encoder or accept the GPL consequences deliberately. Record the actual configure flags in the Modal image. | [ffmpeg.org/legal](https://www.ffmpeg.org/legal.html) |
| **Vercel Hobby** | plan terms, not a software licence | §1.3 already notes Hobby is non-commercial. The moment the project monetises, the web host must move to Pro. Tracked here because it is the same class of mistake as an NC checkpoint. | — |

## 4b. Evaluation datasets

§3.10 Tier A scores the Phase 1 gate against public data, so the same rule applies to
datasets as to checkpoints: **evaluation only, never shipped, terms read before download.**
A non-commercial dataset is the same class of exposure as a non-commercial checkpoint, and
the risk is subtler — nothing about a derived ground-truth YAML announces where it came
from, which is why every Tier A file records `source.dataset` and `source.scene_id`.

| Dataset | Used for | Access | Terms | Verified |
| --- | --- | --- | --- | --- |
| **ARKitScenes** (Apple) | **Tier A primary.** FARO laser depth + real iPad captures + 17-class furniture boxes. | **Open download script. No account, no application, no affiliation check.** | Dual licence. Non-commercial by default; **commercial use is granted** if monthly active users stayed below **700 million** before August 2024, above which Apple's explicit permission is required. We are obviously below it, so this is usable commercially — more permissive than any academic dataset considered. Licence queries: ARKitScenes@group.apple.com | ✅ read 2026-09-17 |
| ~~ScanNet++~~ | rejected | Application **requires academic credentials / institutional email** | n/a | ✅ rejected on access |
| ~~Zillow Indoor (ZInD)~~ | rejected — would have supplied openings for E5 | Manual approval, **verifies academic credentials**, up to two weeks | Academic **non-commercial only**; explicitly bars incorporation into a product | ✅ rejected on access |
| ~~Matterport3D~~ | rejected | Academic agreement | n/a | ✅ rejected on access |

**Why the rejections are recorded.** So the next person does not spend an afternoon
rediscovering that the obvious datasets are gated. ARKitScenes was not the first choice on
data quality — ScanNet++ has sub-millimetre scans and annotates doors and windows — it is
the choice that is actually reachable.

**What this means in practice.** Derived ground truth stays in the repo (it is our
measurement of their data, and it is small text). The **source scans and videos do not** —
`eval/captures/` is gitignored for exactly this reason. ARKitScenes' commercial grant means
Tier A is not the liability an academic dataset would have been, but the data is still used
only to *measure*, never as training input and never shipped.

## 5. Pinned identifiers

Copy these exactly. The wrong string is a licence violation, not a bug.

```
# Production-eligible
facebook/map-anything-apache        # Apache-2.0    NOT facebook/map-anything
facebook/VGGT-1B-Commercial         # gated         NOT facebook/VGGT-1B
depth-anything/DA3METRIC-LARGE      # Apache-2.0    NOT DA3-LARGE / DA3-GIANT
depth-anything/DA3-BASE             # Apache-2.0
Ruicheng/moge-2-vitl-normal         # MIT
google/siglip2-base-patch16-224     # Apache-2.0

# Evaluation baseline only -- must not reach the production pipeline
facebook/map-anything               # CC BY-NC 4.0
facebook/VGGT-1B                    # non-commercial
depth-anything/DA3-GIANT-1.1        # CC BY-NC 4.0
manycore-research/SpatialLM1.1-*    # CC BY-NC 4.0 (all variants)
```

## 6. How this gate stays enforced

The register is a snapshot; the gate has to survive Phase 1, where checkpoints get swapped
during a bake-off and it is easy to leave an NC model wired in. Three things carry it:

1. **Every Phase 1 experiment records the exact checkpoint id it ran**, in the run's
   `meta.json` alongside the metrics (`eval/runs/<run>/meta.json` already exists for this).
   `eval/reports/phase1.md` cannot claim a winner without naming the checkpoint.
2. **Adopting a model means adding its row here first**, with the source read and dated —
   not after the code is merged.
3. **The NC list in §5 is the denylist.** When `workers/pipeline` gains real model loading
   (Phase 1), a startup assertion should refuse to load any id on it outside an explicitly
   flagged evaluation run. Cheap to write, and it is the only mechanism here that does not
   depend on someone remembering.
