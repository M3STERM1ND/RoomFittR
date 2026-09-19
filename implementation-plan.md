# implementation-plan.md
## FurnituR — Technical Implementation Plan (V1)

> **Relationship to `Masterplan.md`:** The Masterplan is the source of truth for *what* we build and *why*. This document only covers *how*. If the two ever conflict on product scope, the Masterplan wins and this document gets updated.
>
> **Status:** Draft for review. Nothing here is built yet. Items marked **[VERIFY]** are facts (licenses, memory numbers, prices) that must be re-checked against primary sources when the relevant phase starts. Items marked **[EXPERIMENT]** are decisions gated on a test described in §3.10 or §10.

---

## 0. The Five Decisions That Shape Everything Else

These are the load-bearing calls in this plan. Everything below follows from them.

1. **The "clean 3D room" is a generated, simplified model, not a photoreal reconstruction with furniture erased.**
   We reconstruct the video only to *measure* the room (walls, floor, ceiling, doors, windows) and to *sample its appearance* (wall/floor color and texture). We then build a lightweight room shell mesh from that measured geometry. Furniture is "removed" because it is never rebuilt, not because it is inpainted out of a 3D scan.
   This turns the hardest research problem in V1 (3D inpainting of Gaussian splats or meshes) into a tractable engineering problem, and it gives us the "simplified/efficient models" the Masterplan wants for browser performance.

2. **Feed-forward multi-view reconstruction models, not classical photogrammetry, are the primary reconstruction path.**
   Models in the VGGT / MapAnything / Depth Anything 3 family predict camera poses and dense geometry from a set of frames in seconds. They handle the blank walls and low texture typical of rooms far better than COLMAP-style SfM. COLMAP stays around only as an accuracy baseline during the Phase 1 bake-off.

3. **One machine-readable `RoomModel` JSON (millimeters, versioned schema) is the contract between the CV pipeline, the layout engine, the database, and the viewer.**
   The CV pipeline writes it and everything downstream reads it. This lets the two of us work in parallel: layout and web work can start from hand-made fixture rooms before the pipeline is finished.

4. **The AI decides *what* and *roughly where*. Deterministic code decides *exactly where* and *whether it's legal*.**
   The LLM picks categories, the budget split, products from pre-filtered shortlists, and spatial intents ("sofa against the long wall, facing the window"). A deterministic solver turns intents into coordinates, and a deterministic validator is the final authority on collisions, clearances, and budget. The LLM never outputs raw coordinates that get used unchecked.

5. **Managed, scale-to-zero services around a plain-Python GPU pipeline.**
   Vercel (web) + Supabase (Postgres, Auth, Realtime) + Cloudflare R2 (media) + Modal (GPU/CPU workers and cron). With no traffic we pay roughly nothing, and there are no servers for two people to babysit. The pipeline is a normal Python package inside a Docker image, so we can move it off Modal later without a rewrite.

---

## 1. Tech Stack

### 1.1 Summary Table

| Concern | Choice | Why it fits FurnituR |
|---|---|---|
| **Frontend framework** | **Next.js (App Router, current stable) + React + TypeScript** | One codebase for UI and a thin API layer, first-class Vercel deploy, a big ecosystem, and the best-supported host for React Three Fiber. |
| **Styling / UI primitives** | Tailwind CSS + Radix UI primitives | Accessible headless components without being tied to a template look. Fast for two people. |
| **3D rendering** | **three.js via React Three Fiber (R3F) + drei** | The most mature open-source WebGL stack. It is declarative inside React, which suits an editor with selectable, draggable items. It loads glTF natively, and drei provides controls, BVH raycasting, and contact shadows. |
| **3D asset format** | glTF 2.0 binary (`.glb`) with meshopt geometry compression + KTX2 (Basis Universal) textures, packed with `gltfpack` | Open standard with the smallest browser payload. `gltfpack` is a single binary that is easy to drop into the worker image. |
| **Client state** | Zustand (scene/editor state), TanStack Query (server state) | Small, predictable, no boilerplate. Keeps server state out of the editor store. |
| **In-browser capture** | `getUserMedia` + `MediaRecorder`; file-upload fallback | Works in current mobile Safari and Chrome with no native app. Upload covers devices where recording in the browser misbehaves. |
| **Uploads** | Uppy with S3 multipart plugin → presigned multipart uploads directly to R2 | Room videos are 100–500 MB. Resumable, chunked, browser-to-bucket uploads never touch our API server. |
| **API layer** | Next.js Route Handlers (TypeScript) + Zod validation | CRUD, presigning, and layout edits are light and synchronous. No separate API server to deploy. |
| **Database** | **Supabase Postgres** (+ `pgvector`) | Relational data fits the model (rooms → scans → layouts → items → products). Row-Level Security gives per-user access control in the database itself. Free tier to start, $25/mo Pro when needed. |
| **Migrations / types** | Supabase CLI SQL migrations. TS types via `supabase gen types`. Python models via Pydantic. | Plain SQL as the single schema source, with typed access from both languages. |
| **Object storage** | **Cloudflare R2** (private bucket, presigned URLs) | S3-compatible, **zero egress fees** (3D assets are downloaded constantly), 10 GB free, cheap after that. |
| **Authentication** | **Supabase Auth**: Google OAuth + **anonymous sign-in** + identity linking | Matches "usable without an account, sign in to keep things" exactly. Anonymous users get a real user ID, so RLS works identically, and linking Google later keeps all their rooms. |
| **Background jobs / workers** | **Modal** (serverless GPU + CPU Python functions, cron) with job state in a Postgres `jobs` table | Per-second GPU billing that scales to zero. Python-native, which is where all the CV and ML code lives. Built-in cron for scraping, price refresh, and sweepers. No Redis/Celery to operate. |
| **Video processing** | FFmpeg / ffprobe, OpenCV, NumPy | The standard tools for decoding, rotation, and metadata stripping, plus cheap blur/exposure metrics. |
| **3D reconstruction** | **Primary candidates [EXPERIMENT]:** MapAnything (Apache-2.0 checkpoint), VGGT-1B (commercial-use checkpoint) + metric depth, Depth Anything 3. **Baseline:** COLMAP/GLOMAP. | See §3.4. Feed-forward, fast, robust on low texture. MapAnything and DA3 predict metric scale directly. |
| **Metric depth / scale** | MoGe-2 (MIT) as an independent scale estimator | A second opinion on absolute scale, independent of the reconstruction model. |
| **Room layout extraction** | Classical geometry (plane/line fitting on the gravity-aligned point cloud) as the guaranteed path. **SpatialLM** as a candidate learned path. [EXPERIMENT] | The classical path is predictable and debuggable. SpatialLM outputs walls, doors, windows, and object boxes directly from point clouds, which could replace a lot of hand-written code if it's accurate on our data. |
| **Object detection / segmentation** | **SAM 3** (text-promptable image + video segmentation with tracking). Fallback: Grounding DINO + SAM 2 (both Apache-2.0). | Open-vocabulary prompts ("sofa", "door", "window", "rug") with instance tracking across frames, so we don't train a custom detector. |
| **2D inpainting (textures)** | LaMa (Apache-2.0) | Fills occluded patches in wall/floor texture atlases. Only needed for the textured tier (§3.7). |
| **Mesh / geometry utilities** | Open3D, trimesh, shapely, scipy, pygltflib | Standard, permissively licensed Python geometry tooling. |
| **Scraping** | Python: `httpx`, `selectolax`, `extruct` (JSON-LD/microdata), Playwright (JS-rendered fallback), `price-parser` | Most furniture retailers embed schema.org `Product` JSON-LD, so structured extraction does most of the work. A headless browser runs only when necessary. |
| **LLM** | **Claude API**: `claude-sonnet-5` for layout planning, `claude-haiku-4-5-20251001` for extraction fallback, product style/category tagging (vision), and room-type inference. Structured JSON output via tool schemas. Batch API for catalog tagging. | Strong at structured spatial reasoning over compact JSON, reliable schema-conforming output, and cheap enough per layout (cents). Haiku plus the Batch API keeps catalog tagging costs low. Open-weights alternative in §10. |
| **Embeddings (style similarity)** | SigLIP 2 image embeddings stored in `pgvector` | "Similar style" swaps and coherence scoring without an LLM call per item. |
| **Observability** | Sentry (web + workers, free tier), structured JSON logs, a `scan_events` table | Enough to debug a failed scan end to end without a logging stack to run. |
| **Deployment** | Vercel (web/API), Supabase (DB/Auth/Realtime), R2 (media), Modal (workers). GitHub Actions CI. | All managed, all with free or near-free tiers at our scale. |
| **Repo** | Monorepo: `pnpm` workspaces (TS) + `uv` (Python) | One place for the schemas shared across TS and Python. |

### 1.2 Licensing Gate **[VERIFY]**

Several strong 3D-vision models are released **non-commercial** (e.g., DUSt3R/MASt3R, original 3D Gaussian Splatting, some Depth Anything 3 and SceneScript checkpoints). FurnituR is personal at first but may grow, so **only models whose weights allow commercial use are eligible for the production pipeline**. Non-commercial models may be used as evaluation baselines only.

Before any model is adopted, record its code license, weights license, and model-card restrictions in `docs/model-licenses.md`. Specific checks:

- MapAnything: confirm which checkpoint is Apache-2.0.
- VGGT: confirm the terms of the commercial-use checkpoint.
- Depth Anything 3: licenses differ by checkpoint size.
- SAM 3: SAM License terms.
- SpatialLM: code license vs. base-LLM weights license.
- MoGe-2, LaMa, SigLIP 2: expected permissive, still confirm.
- Hunyuan3D (if ever considered): territorial restrictions.

### 1.3 Expected Running Cost at Personal-Beta Scale **[VERIFY prices]**

| Item | Estimate |
|---|---|
| Vercel Hobby | $0 (Hobby is non-commercial. Move to Pro if the project monetizes.) |
| Supabase | $0 → $25/mo once we exceed free-tier DB/storage or need no-pause projects |
| Cloudflare R2 | ~$0–5/mo (10 GB free; ~$0.015/GB-month after) |
| Modal GPU | ~$0.20–0.60 per scan (≈5–10 GPU-minutes on L40S/A100 class). Monthly free credits cover early testing. |
| Claude API | ~$0.02–0.10 per generated layout. Catalog tagging a few dollars per 10k products via Batch API. |
| **Total, ~100 scans/month** | **≈ $30–80/mo** |

**Cost guardrails (built in from day one):** per-user daily scan quotas, a global daily GPU-minute cap checked before dispatch, and a hard per-job timeout.

---

## 2. System Architecture

### 2.1 Component Diagram

```
                            ┌──────────────────────────────────────────┐
                            │              Browser (Next.js)           │
                            │  Capture · Upload · Status · 3D Viewer   │
                            │  Layout Editor (R3F) · TS Validator      │
                            └───┬───────────┬──────────────┬───────────┘
            multipart PUT       │           │ HTTPS/JSON   │ Realtime (WS)
            (presigned)         │           │              │ scan/layout status
                                ▼           ▼              │
                    ┌──────────────┐  ┌─────────────────┐  │
                    │ Cloudflare R2│  │ Next.js API     │  │
                    │ (private)    │  │ (Vercel)        │  │
                    │ videos, glb, │  │ Zod · presign · │  │
                    │ textures,    │  │ CRUD · validate │  │
                    │ debug, thumbs│  │ · dispatch jobs │  │
                    └──────▲───────┘  └───┬─────────┬───┘  │
                           │              │ JWT/RLS │ HMAC  │
                           │              ▼         ▼       │
                           │    ┌───────────────┐ ┌──────────────────────┐
                           │    │ Supabase      │ │ Modal                │
                           │    │ Postgres+RLS  │◀┤ dispatch endpoint    │
                           │    │ Auth (Google, │ │  ├ process_scan (GPU)│
                           │    │  anonymous)   │ │  ├ rescale_scan (CPU)│
                           │    │ Realtime ─────┼─┘  ├ generate_layout   │
                           │    │ pgvector      │    ├ purge_* (CPU)     │
                           │    └───────▲───────┘    ├ catalog_* (cron)  │
                           │            │ psycopg    └ sweep_jobs (cron) │
                           │            │ (worker role)        │         │
                           └────────────┴──────────────────────┘         │
                                  S3 API (worker creds)        │
                                                               ▼
                                          ┌─────────────────────────────┐
                                          │ External: Claude API,       │
                                          │ retailer websites (crawl)   │
                                          └─────────────────────────────┘
```

### 2.2 Communication Rules

| From → To | Mechanism | Auth |
|---|---|---|
| Browser → API | HTTPS JSON, `/api/v1/*` | Supabase session JWT (anonymous or Google) |
| Browser → R2 | Presigned multipart PUT (upload), presigned GET (assets) | URL signature, 15-minute TTL |
| Browser ← status | Supabase Realtime subscription on `scans` / `layouts` rows (RLS-filtered), with 10 s polling fallback | JWT |
| API → Postgres | `supabase-js` **with the user's JWT**, so RLS enforces ownership | JWT |
| API → Modal | HTTPS POST to a Modal web endpoint that only calls `.spawn()` and returns | Shared bearer token (Vercel env + Modal secret) |
| Worker → Postgres | `psycopg` using a dedicated `worker` DB role (write access to pipeline tables only; no `auth` schema) | Connection string in Modal secret |
| Worker → R2 | S3 API with a worker-scoped R2 token | R2 token in Modal secret |
| Worker → Claude | Anthropic SDK | API key in Modal secret |

**Job-state principle:** Postgres is the source of truth for every job's status. Modal is only compute. The API inserts a `jobs` row, then dispatches. The worker claims the row (`status = queued → running`, sets `locked_at`, `heartbeat_at`) before doing work. A scheduled sweeper re-queues or fails jobs whose heartbeat is stale. If a Modal call is lost, the job still resolves.

### 2.3 End-to-End Data Flow

```
[1] CAPTURE (browser)
    getUserMedia → MediaRecorder (or pick file) → client checks (duration, size, mime)
    POST /rooms → POST /rooms/:id/scans → presigned multipart → Uppy uploads to R2
    POST /scans/:id/upload/complete
        API: HEAD object (size/type), insert jobs(process_scan), dispatch → 202

[2] PROCESSING (Modal GPU worker, process_scan; each stage checkpoints to R2)
    S1 ingest       ffprobe validate · remux w/o metadata (strip GPS) · normalize rotation
    S2 frames       decode @3 fps · blur/exposure scoring · keyframe selection (60–150)
    S3 reconstruct  feed-forward model → camera poses + per-frame depth/pointmaps
    S4 segment      SAM 3 prompts over keyframes (tracked) → instance masks
                    (furniture, clutter, doors, windows, floor, wall, ceiling)
    S5 fuse         lift masks into 3D · gravity + Manhattan alignment
                    · split structure points vs. object points · object OBBs
    S6 layout       floor/ceiling heights · wall polygon · openings on walls
    S7 scale        metric-model scale ⊕ MoGe-2 scale ⊕ door/ceiling priors
                    ⊕ user measurement (if any) → scale factor + confidence
    S8 shell        room shell mesh (floor, walls w/ cutouts, ceiling)
                    · appearance: flat colors (tier 0) / projected textures (tier 1)
                    · gltfpack → room_shell.glb
    S9 publish      room_model.json + objects.json + assets → R2
                    · room_models row · scan.status = ready
                    · auto-enqueue generate_layout if the user already set a budget

[3] 3D ROOM (browser)
    GET /scans/:id/assets (presigned GETs) → load room_shell.glb in R3F
    Detected furniture shown as toggleable "ghost" boxes (removed by default)
    Optional: user enters a wall length → POST calibration → rescale_scan (CPU, ~10 s)

[4] FURNITURE SELECTION + [5] LAYOUT (Modal CPU worker, generate_layout)
    L1 room analysis    usable floor, door-swing keep-outs, window zones, free wall runs
    L2 AI plan          Claude: room type, category slots, budget split, spatial intents
    L3 shortlist        SQL: catalog filtered by category/price/footprint/style → top-K
    L4 AI pick          Claude (or deterministic scorer): choose per slot for coherence
    L5 solve            deterministic candidate poses + scoring + beam search + fallbacks
    L6 VALIDATE         deterministic hard/soft constraint check + budget check
    L7 persist          layouts + placed_items + validation_report → status ready

[6] INTERACTIVE 3D SCENE (browser)
    Load layout → instantiate furniture proxies (scaled to product dims, tinted)
    Edit: drag/rotate/swap/add/remove → TS validator runs live on every change
    Save: PATCH /layouts/:id/items (optimistic concurrency) → server re-validates
    Tap item → product card → GET /out/:productId → allowlisted redirect to retailer
```

### 2.4 Cross-Cutting Conventions

- **Units:** Integers in **millimeters** in JSON, the DB, and the layout engine. glTF assets are in **meters**, per the glTF spec. Money is integer **cents** plus an ISO currency code.
- **Coordinates:** Right-handed, **Y-up** (matches glTF/three.js). Floor plane is `y = 0`. Origin is the centroid of the floor polygon. The **+X axis is aligned with the longest wall** after Manhattan alignment.
- **Rotation:** Degrees about +Y. The AI solver uses {0, 90, 180, 270}. Manual editing snaps to 15° (hold a modifier for free rotation).
- **Placed-item pose:** Footprint center `(x_mm, z_mm)`, `elevation_mm` (0 for floor items), `rotation_deg`. An item's local "front" is −Z before rotation.
- **Schemas:** `packages/schemas/*.schema.json` (JSON Schema) is the single definition of `RoomModel`, `ObjectsFile`, `LayoutPlan`, and `ValidationReport`. TypeScript types and Pydantic models are generated from it, and CI fails if the generated files are stale.
- **Pipeline versioning:** Every artifact and `room_models` row records `pipeline_version` (semver). R2 key layout: `rooms/{room_id}/scans/{scan_id}/{pipeline_version}/{stage}/…`
- **Repo layout:**
  ```
  apps/web/                 Next.js app (UI + /api/v1)
  packages/schemas/         JSON Schemas + generated TS
  packages/geometry/        TS validator + 2D geometry (browser + API)
  packages/furniture-proxies/  parametric proxy generators / GLB library
  workers/pipeline/         Python: video → RoomModel (no Modal imports)
  workers/layout/           Python: planner, solver, validator
  workers/catalog/          Python: crawl, extract, normalize, tag
  workers/modal_app/        Thin Modal wrappers, cron, dispatch endpoint
  eval/                     Datasets manifest, metrics, bake-off scripts
  fixtures/                 Golden RoomModels, layouts, HTML snapshots, short videos
  supabase/migrations/      SQL migrations + RLS policies
  ```

---

## 3. The Hardest Part: Video → 3D

### 3.1 What the Pipeline Must Actually Produce

The pipeline does not need to produce a beautiful 3D scan. It must produce:

1. **Correct room geometry in real-world units:** floor polygon, ceiling height, and wall segments. Accuracy matters more than anything else in this pipeline.
2. **Openings on the right walls:** doors (position, width, height, swing side if inferable) and windows (position, width, sill height, head height).
3. **The existing furniture as boxes:** where it is and how big, so it can be removed and optionally shown as ghosts.
4. **Enough appearance to recognize the room:** wall color, floor material, optionally real textures.
5. **A scale confidence** that tells the UI whether to nudge the user for a measurement.

### 3.2 Input Requirements & Capture Guidance

**Hard limits (validated client-side and again in S1):**

| Property | Requirement |
|---|---|
| Duration | 20 s – 3 min (reject < 20 s; truncate/downsample > 3 min) |
| Resolution | ≥ 720p on short side; 1080p recommended |
| Frame rate | ≥ 24 fps (VFR accepted, normalized on decode) |
| Size | ≤ 750 MB |
| Container/codec | MP4/MOV (H.264/HEVC), WebM (VP8/VP9/AV1) |
| Scope | One room per video |

**Capture guidance (shown as a 3-step coach screen before recording):**
- Hold the phone at chest height and walk slowly along the room's perimeter, facing inward and panning across each wall. End where you started.
- Keep the floor-to-wall line and the ceiling-to-wall line in view as often as possible. These junctions drive the wall geometry.
- Turn on the lights and open the blinds. Avoid pointing straight at bright windows for long.
- Pan slowly. **In-app, we read `DeviceOrientationEvent` rotation rate during recording and show a "slow down" hint above ~45°/s.**
- Landscape is recommended (wider field of view). Portrait is accepted.

**Optional sidecar (cheap, recorded in-browser):** Gravity vector samples from `DeviceMotionEvent` at ~10 Hz, uploaded as `motion.json`. Used as a prior for up-axis estimation in S5. Never required. iOS needs a permission prompt, so skip it if denied.

### 3.3 S1–S2: Ingest, Frame Extraction, Preprocessing

**S1 Ingest (CPU)**
1. `ffprobe`: duration, codec, dimensions, rotation tag, fps, audio presence. Unreadable → `VIDEO_UNREADABLE` (user-fixable).
2. Remux with `-map_metadata -1 -an` to strip GPS/device metadata and audio. The stripped file replaces the original in R2; the original is deleted. Home footage should not carry location data.
3. Record `video_meta` on the scan row.

**S2 Frames (CPU, vectorized)**
1. Decode at **3 fps** with auto-rotation applied → candidate frames. A 90 s video yields ~270 candidates.
2. Per frame, computed on a 640 px downscale:
   - **Sharpness:** variance of Laplacian, normalized by the video's median.
   - **Exposure:** mean luminance, % clipped highlights, % crushed shadows.
   - **Motion:** median sparse optical-flow magnitude versus the previous kept frame (OpenCV Lucas-Kanade on FAST corners).
3. Drop frames below the 25th percentile of sharpness or with bad exposure.
4. **Keyframe selection:** Greedily keep a frame when accumulated flow since the last keyframe exceeds a threshold (≈8–12% of the image diagonal), or when rotation since the last keyframe exceeds ~10°. Then trim or pad to the target count `N` with uniform temporal coverage.
   - `N` is set by the GPU memory profile (§3.8). Start at **N = 100**, clamped to [40, 150].
5. Save two copies of each keyframe: a model-input resolution copy (e.g., 518 px long side for VGGT-class models) and a **full-resolution copy for texturing**.
6. Early exits (user-fixable):
   - `< 30` usable frames → `VIDEO_TOO_SHORT_OR_BLURRY`
   - median luminance too low → `VIDEO_TOO_DARK`

### 3.4 S3: Reconstruction

**Candidates (decided in the Phase 1 bake-off, §3.10):**

| Candidate | Output | Scale | Why consider | Concerns |
|---|---|---|---|---|
| **A. MapAnything** (Apache-2.0 checkpoint) | Poses, intrinsics, per-view metric pointmaps | **Metric** | One model gives geometry *and* scale. Can also accept intrinsics if known. | Accuracy on phone video of cluttered rooms is unproven for us. Memory at N ≈ 100 **[VERIFY]**. |
| **B. VGGT-1B** (commercial checkpoint) + MoGe-2 scale | Poses, depth, pointmaps, tracks | Up to scale (needs S7) | Very strong published pose/depth accuracy. Fast (~seconds for 100 frames on a 40–80 GB GPU **[VERIFY]**). Optional bundle-adjustment refinement from its tracks. | Scale must come from elsewhere. Memory grows roughly linearly (~20 GB at 100 frames, ~40 GB at 200 **[VERIFY]**). |
| **C. Depth Anything 3** (commercially licensed checkpoint only) | Poses, depth, rays | Metric variants exist | Recent "any-view" model with strong reported results. | License differs by checkpoint **[VERIFY]**. |
| **D. COLMAP/GLOMAP + dense MVS** (baseline only) | Sparse + dense cloud | Up to scale | Well-understood reference. | Slow, fails on blank walls. Some MVS tools are AGPL. **Not for production.** |

**Process (for whichever model wins):**
1. Load weights from a Modal Volume. Use Modal memory snapshots to cut cold starts.
2. Run inference on N keyframes in bfloat16.
3. **If N exceeds the GPU budget:** process overlapping chunks (e.g., 60 frames with 15 overlap) and align chunks with a Sim(3) fit on shared frames. Build this only if the bake-off shows we need N > the single-pass limit.
4. **Filter geometry:**
   - Drop points with low model confidence (bottom 30%).
   - Drop points at grazing angles and depth discontinuities.
   - Remove the sky/outdoor region seen through windows. Points far beyond the wall planes are removed in S6.
5. Output `recon.npz` (poses, intrinsics, depth maps, confidence maps), `points.ply` (voxel-downsampled at 1 cm), and **reprojection/consistency metrics** used for failure detection:
   - Too few confident points, or poses that collapse into a line → `RECON_FAILED` (retry once with different keyframe spacing, then fail as `INSUFFICIENT_COVERAGE`).

### 3.5 S4–S5: Segmentation and 3D Fusion

**S4 Segmentation (GPU)**
- Run **SAM 3** in video mode over the keyframe sequence with text prompts in three groups:
  - **Removable:** `sofa, armchair, chair, table, coffee table, desk, bed, nightstand, dresser, bookshelf, cabinet, tv stand, television, rug, lamp, plant, ottoman, bench, box, bag, clothes, clutter`
  - **Structure:** `door, window, wall, floor, ceiling`
  - **Fixed (not removable by default):** `radiator, fireplace, built-in shelving, kitchen counter, column, stairs`
- Output per-frame instance masks with track IDs and scores.
- **Fallback path** if SAM 3 underperforms or is unavailable: Grounding DINO (box proposals from the same prompts) → SAM 2 video propagation.

**S5 Fusion (CPU/GPU)**
1. **Lift:** For each mask pixel with valid depth, back-project to 3D using the S3 poses. Accumulate points per track ID.
2. **Merge** tracks that refer to the same physical object (3D IoU > 0.3 and same label group).
3. **Gravity alignment:**
   - Estimate the up-axis from points labeled `floor` (RANSAC plane). Cross-check against the motion sidecar gravity if present.
   - If there are no floor labels, use the dominant horizontal plane among the lowest 10% of points.
4. **Manhattan alignment:** Build a histogram of wall-labeled point normals projected onto the floor plane, find the dominant direction (mod 90°), and rotate so it aligns with the X axis.
5. **Partition:**
   - **Structure points:** labeled wall/floor/ceiling, or unlabeled points within 5 cm of a later-fitted structural plane.
   - **Object points:** points belonging to a removable/fixed instance.
6. **Objects:** For each instance, fit a gravity-aligned oriented bounding box (min-area rectangle on the XZ projection + Y extent), trimmed to the 2nd–98th percentile of points to reject bleed. Write `objects.json`: label, score, OBB (center, size, yaw) in mm, `removable` (from label group), and a thumbnail crop reference.

### 3.6 S6: Room Geometry Extraction

**Guaranteed classical path (always built):**
1. **Floor and ceiling:** Histogram structure points along Y. The lowest dense peak is the floor (set to y = 0) and the highest is the ceiling. `ceiling_height_mm = peak_ceiling − peak_floor`. If the ceiling is poorly observed (common), mark `ceiling_observed = false` and fall back to the wall-top evidence or a 2400 mm prior with low confidence.
2. **Wall evidence map:** Take structure points between 0.3 m and (ceiling − 0.3 m) and project them to a 2D density grid on XZ (2 cm cells). **Exclude object points.** Furniture against a wall must not pull the wall inward.
3. **Line extraction:**
   - Run RANSAC/Hough line detection on the density map, with a strong prior for the two Manhattan directions.
   - Allow non-Manhattan lines only when evidence is strong (e.g., bay window, angled wall).
4. **Polygon closure:**
   - Build the room boundary as the largest closed polygon from candidate lines (cell-complex / arrangement approach, scored by wall-evidence coverage along edges versus free space inside).
   - Snap near-90° corners, then merge collinear segments.
5. **Open boundaries:** Where evidence is missing over a long run (open-plan, a doorway without a door), create a wall segment flagged `kind: "open"`. It is rendered as a thin floor-edge line instead of a wall, and the solver treats it as a boundary but not as a wall to place furniture against.
6. **Openings:**
   - For each `door` or `window` instance, project its 3D points onto the nearest wall plane (within 30 cm).
   - Fit an axis-aligned rectangle in wall coordinates: `offset_mm` (from the wall start), `width_mm`, `sill_mm`, `height_mm`.
   - Doors: `sill ≈ 0`. Default width/height clamped to plausible ranges (600–1200 × 1900–2400 mm).
   - Door swing side is left as `unknown` in V1 unless inferable. The validator uses a conservative keep-out covering both sides (§5.5).

**Candidate learned path [EXPERIMENT]: SpatialLM**
- Feed the gravity-aligned structure point cloud to SpatialLM. Parse its walls/doors/windows/boxes output into the same `RoomModel` structure.
- Use it if it beats the classical path on the Phase 1 metrics. Keep the classical path as the fallback when SpatialLM output fails schema or sanity checks: non-closed polygon, area outside 3–150 m², wall lengths < 0.5 m.

**Sanity checks (both paths):** Polygon is simple (non-self-intersecting), area 3–150 m², ceiling 2000–4500 mm, every opening lies within its wall's extent, and no two doors overlap.

Failure → `LAYOUT_EXTRACTION_FAILED` (system error, retryable once with the alternate path).

### 3.7 S7–S8: Dimension Estimation, Furniture Removal, and the Room Shell

**S7 Scale (dimension estimation)**

We fuse independent scale estimates into one factor `s` (multiplying reconstruction units → mm):

| Source | How | Typical reliability |
|---|---|---|
| `s_model` | Native metric output of reconstruction model (A or C) | To be measured |
| `s_mono` | Median ratio of MoGe-2 metric depth to reconstruction depth over confident pixels on all keyframes | To be measured |
| `s_door` | Detected door heights vs. prior (2030 mm ± 60) — only doors fully visible top-to-bottom | Good when ≥1 clean door |
| `s_ceiling` | Ceiling height vs. prior (2440–2740 mm) | Weak — sanity check only |
| `s_user` | User-entered wall length / ceiling height / door width vs. measured model value | **Authoritative** |

- If `s_user` exists, use it and set confidence to `high`.
- Otherwise, take a robust weighted median of the available sources (weights learned from Phase 1 data).
  - **Confidence** comes from the spread between sources: within ±3% → `high`, ±3–8% → `medium`, worse → `low`.
  - At `low`, the scan still becomes `ready`, but the UI shows a mandatory-looking (dismissible) "Help us get your measurements right" prompt before layout generation.
- `rescale_scan` is a **CPU-only re-run of S7–S9** using stored intermediate data (~10–20 s). Entering a measurement never re-runs the GPU stages.

**How "furniture removal" works in V1**

Because the shell is generated from measured geometry, existing furniture is absent by construction. The remaining problem is appearance where furniture blocked the view of walls and floor. Two tiers:

- **Tier 0: flat materials (always produced, guaranteed).**
  - For each wall, the floor, and the ceiling: take pixels labeled wall/floor/ceiling whose depth agrees with the fitted plane (within 5 cm, i.e., not occluded), and compute a robust median color in linear RGB with white balance normalized across frames.
  - Floor material class (wood / carpet / tile / other) comes from a Haiku vision call on 2–3 floor crops and maps to a small tileable material library (CC0 textures) tinted to the median color.
  - The result is recognizably "your room", clean, and cheap.
- **Tier 1: projected textures [EXPERIMENT, stretch for V1].**
  - Per planar surface, allocate a texture atlas at ~5 mm/texel, capped at 2048² per surface.
  - For each texel, choose the best keyframe by score = sharpness × cos(view angle) × (distance falloff), considering only views where the texel is **unoccluded** (plane depth ≤ observed depth + 5 cm and not inside a removable-object mask).
  - Blend the top 2–3 views with feathering. Texels with no valid view are filled with LaMa in texture space, or with the Tier 0 color when a hole exceeds ~0.5 m².
  - Adopt only if Phase 2 side-by-side review shows Tier 1 looks better than Tier 0 on at least 70% of test rooms **and** has no glaring artifacts.

**Ghost objects:** Removed furniture from `objects.json` renders as translucent boxes behind a "Show what was here" toggle. Users can mark a detected object as **"keep"**, and it then becomes a fixed obstacle for layout. This handles built-ins and items the user isn't replacing. Fixed-group objects default to "keep".

**S8 Shell mesh**
- Floor: triangulated polygon (earcut). Ceiling: same polygon at `ceiling_height`, rendered back-face culled so the camera can look in from above.
- Walls: one quad strip per segment with thickness 100 mm, with **opening cutouts** (door/window rectangles subtracted in wall-local 2D, then triangulated). Windows get a simple frame and a semi-transparent glass pane. Doors get a frame.
- UVs in meters for tileable materials (Tier 0) or atlas UVs (Tier 1).
- Export with trimesh/pygltflib → `gltfpack -cc -tc` (meshopt + KTX2). **Budget: room_shell.glb ≤ 3 MB (Tier 0) / ≤ 12 MB (Tier 1).**

### 3.8 Output Formats

| Artifact | Format | Consumer | Retained |
|---|---|---|---|
| `room_model.json` | `RoomModel` schema (below) | Layout engine, viewer, DB (`room_models.geometry`) | Yes |
| `objects.json` | `ObjectsFile` schema | Viewer ghosts, layout (kept obstacles) | Yes |
| `room_shell.glb` | glTF binary, meshopt + KTX2 | Viewer | Yes |
| `thumb.jpg` | 640 px render of top-down view | Room list | Yes |
| `keyframes/`, `recon.npz`, `masks/` | JPEG, NumPy, RLE masks | `rescale_scan`, debugging, re-texturing | Yes, deleted with the room |
| `points.ply` | Downsampled point cloud | Debug viewer (internal) | 30 days, then lifecycle-deleted |
| `video.mp4` (metadata-stripped) | Original remux | Reprocessing with future pipeline versions | Yes (per Masterplan), deletable |

**`RoomModel` (sketch, not final schema):**
```
RoomModel {
  schema_version, pipeline_version, units: "mm", up: "+Y"
  floor_polygon: [[x,z], ...]              // CCW, closed implicitly
  ceiling_height_mm, ceiling_observed: bool
  walls: [{ id: "W1", start: [x,z], end: [x,z], thickness_mm,
            kind: "solid" | "open", length_mm }]
  openings: [{ id: "O1", wall_id, type: "door" | "window" | "passage",
               offset_mm, width_mm, sill_mm, height_mm,
               swing: "left" | "right" | "unknown", confidence }]
  fixed_obstacles: [{ id, label, center:[x,z], size:[w,d,h], yaw_deg, source: "detected"|"user_kept" }]
  appearance: { walls: {W1: {color, material}}, floor: {...}, ceiling: {...}, tier: 0|1 }
  scale: { factor, sources: {...}, confidence: "high"|"medium"|"low", user_calibrated: bool }
  quality: { coverage_pct, frames_used, warnings: [...] }
}
```

### 3.9 GPU Requirements & Processing Time

**GPU memory [VERIFY during Phase 1 profiling]:**

| Stage | Est. peak VRAM (N ≈ 100) | Notes |
|---|---|---|
| S3 VGGT-class reconstruction | ~20–25 GB | Scales ~linearly with N. ~40 GB at N=200. |
| S3 MapAnything | TBD | Profile. |
| S4 SAM 3 video segmentation | ~8–16 GB | Frame batching trades memory for time. |
| MoGe-2 (scale) | ~4–6 GB | Per-frame, batched. |
| LaMa (Tier 1 only) | ~2–4 GB | |

**Target GPU class:** a single **48 GB (L40S) or 80 GB (A100/H100)** GPU per job, with stages run sequentially and models unloaded between stages. 24 GB cards (e.g., a local RTX 4090) are fine for development at N ≤ ~60–80.

**Expected processing time (warm container, N ≈ 100) [EXPERIMENT]:**

| Stage | Est. time |
|---|---|
| Cold start (container + weights from Volume, with snapshots) | 20–90 s |
| S1–S2 ingest + frames | 20–60 s |
| S3 reconstruction | 10–60 s |
| S4 segmentation | 60–180 s |
| S5–S6 fusion + geometry | 20–60 s |
| S7 scale (MoGe-2) | 15–40 s |
| S8 shell Tier 0 / Tier 1 | 10 s / 60–180 s |
| **Total** | **p50 target ≤ 6 min, p95 ≤ 15 min, hard timeout 30 min** |

These numbers are why the UX is "upload and come back" (Masterplan §9), with a Realtime progress bar showing the stage name.

### 3.10 Phase 1 Experiments (Must Pass Before Building the Product)

**Evaluation dataset (built in Phase 0).** Revised 2026-09-17: the original plan called
for 12–15 self-captured rooms with laser-measured ground truth. That costs a laser measure,
several weekends, and access to other people's homes — all spent *before* knowing whether the
pipeline works at all. The gate needs a yardstick, not specifically **our** yardstick, so
Phase 0 now uses public ground truth and defers self-capture to a post-gate reality check.

**Tier A — primary yardstick (Phase 0, free).** **ARKitScenes** (Apple). Chosen after
ScanNet++, ZInD and Matterport3D all turned out to gate access behind academic credentials
or an institutional email, which we do not have. ARKitScenes is a **direct download script
with no account, no application and no affiliation check**, and it is the only open option
that pairs *real* commodity captures with laser ground truth:

- `highres_depth` — ground-truth depth projected from a **FARO laser scanner** mesh, 1920×1440,
  uint16 millimetres. This is the metric yardstick.
- `lowres_wide.traj` — gravity-aligned ARKit camera poses (axis-angle + metres).
- `mov` — the RGB capture itself, which is pipeline input.
- `annotation` — oriented bounding boxes over 17 furniture classes, which gives **E6** ground
  truth for free.

Target **12–15 scenes**, picked for the trait spread of the original roster. Layout is
derived by `eval/adapters/arkitscenes.py`: back-project the laser depth through the poses
into a room-scoped point cloud, split floor and ceiling by height histogram, trace the floor
outline.

- Access: free, open, no application. Apple's licence is dual: non-commercial by default,
  **with commercial use granted below a 700M-MAU threshold** — more permissive than any
  academic dataset we looked at. Recorded in `docs/model-licenses.md` §4b.
- **What it does not have: door and window annotations.** Its taxonomy is 17 furniture
  classes and contains no `door` or `window`, so **E5 has no Tier A ground truth** and moves
  to Tier B with E2, E3 and E8. That is a cheap move: doors and windows are the easiest
  thing in a room to measure with a tape.

**How circular is this?** Less than it looks, but not zero, and worth stating plainly. The
adapter's input is dense, complete, metric and already gravity-aligned; S6's input is a
sparse, noisy, scale-ambiguous reconstruction from phone video. They share no code. But
unlike ScanNet++ — where floor and ceiling came from *human* semantic labels — here the
floor is found geometrically, which is the same *class* of operation S6 performs. So Tier A
measures S6's end-to-end error against a far better instrument, not against an independent
oracle. Tier B, measured by hand, is the independent check.

**Tier B — reality check (deferred until after the Phase 1 gate).** **3–5 self-captured
rooms**, our own homes only, measured with whatever is to hand (tape measure or a phone
measuring app; a laser measure is a nice-to-have, not a prerequisite). Each captured 3× where
two phones are available, including one deliberately "sloppy" capture (faster pans, portrait).
This is what E2, E3 and E8 ultimately need, because Tier A was not captured by a user
following our capture coach. Running it *after* the gate means it is only paid for if the
pipeline has already proven itself on Tier A.

**Consequence for the experiments below.** E1, E4, E6 and E7 are answered on Tier A.
**E2, E3, E5 and E8 are decided on Tier B**: scale and capture robustness depend on how the
video was shot, and openings have no Tier A annotation at all. The Phase 1 gate is therefore
reached in two steps — an E1/E4/E6/E7 verdict on public data, then an E2/E3/E5/E8 verdict
once Tier B exists. **Tier B grows from 3–5 rooms to 5–8** to carry E5's opening statistics;
it is still our own homes and still needs no laser measure.

**Experiments:**

| ID | Question | Method | Pass criteria |
|---|---|---|---|
| **E1** | Which reconstruction model? | Run A/B/C (+D baseline) on all captures through the same S5–S7 code | Highest pipeline success rate. Tie-break on wall-length error, then time and cost. |
| **E2** | Is uncalibrated scale good enough? | Compare the fused `s` against ground truth | **Median wall-length error ≤ 5%, p90 ≤ 10%** uncalibrated |
| **E3** | Does one user measurement fix scale? | Apply one known wall length | **Median ≤ 2%, p90 ≤ 4%** on the *other* walls |
| **E4** | Classical vs. SpatialLM geometry | Polygon IoU vs. ground-truth floor plan, corner count accuracy, wall-length error | Polygon IoU ≥ 0.85 on ≥ 80% of captures (best path) |
| **E5** | Opening detection | Door/window recall and precision; position error along the wall | Door recall ≥ 85%, window recall ≥ 80%, offset error ≤ 150 mm |
| **E6** | Furniture detection for removal | Recall of furniture ≥ 40 cm in largest dimension; OBB footprint error | Recall ≥ 85%. False "wall" pull-in from furniture: none that moves a wall > 10 cm. |
| **E7** | Runtime and memory | Profile each stage on L40S and A100-80 | p95 end-to-end ≤ 15 min; fits one GPU; cost ≤ $1/scan |
| **E8** | Capture robustness | Sloppy vs. careful captures | Success rate on sloppy captures ≥ 60% (drives how strict the capture coach must be) |
| **E9** | Browser recording | MediaRecorder on iOS Safari, Android Chrome, desktop Chrome/Safari/Firefox: codec, resolution, stability over 3 min, upload resume | Usable ≥1080p (or 720p) file on all target browsers; upload resumes after network drop |
| **E10** | Tier 0 vs. Tier 1 appearance | Blind side-by-side with 5 people | Decide Tier 1 inclusion (§3.7) |

**Go / no-go after Phase 1:**
- **Go:** E1–E5 and E7 pass.
- **Go with required calibration:** E2 fails but E3 passes. Measurement input becomes a required step, not an optional one (a product change, flagged to the Masterplan).
- **No-go / rethink:** E3 or E4 fails. Options: stricter guided capture, a manual corner-tapping fallback ("tap the 4 corners of your floor" over keyframes), or re-evaluating newer models before building anything else.

### 3.11 Known Limitations (Expected Even If Phase 1 Passes)

- Mirrors, large glass, and glossy floors create phantom geometry. Mitigation: SAM 3 `mirror` prompt → exclude those points; flag a warning.
- Very dark rooms and strong backlight from windows.
- Curved walls, sloped/attic ceilings, and multi-level floors: approximated or unsupported in V1 (a warning is shown).
- Open-plan spaces: boundaries become `open` segments, and results depend on where the user stopped walking.
- Furniture completely hiding a wall base for its whole length can shift that wall.
- Closed doors with no visible frame may be missed. Door swing side is unknown.
- Scale without a user measurement is a best guess and is presented as such.

---

## 4. Furniture Pipeline

### 4.1 Stages

```
retailers (config) ─▶ discover ─▶ fetch ─▶ extract ─▶ parse dims ─▶ normalize
                                                                     │
          layout system ◀─ publish ◀─ dedupe ◀─ tag/classify ◀─ validate
```

All stages are idempotent Modal functions working in batches, driven by a `crawl_runs` record. Raw fetched pages are stored (compressed) in R2 so extraction can be re-run without refetching when parsers improve.

### 4.2 Discover

- **V1 retailer scope:** Start with **5–8 retailers** chosen for (a) schema.org `Product` JSON-LD on product pages, (b) public XML sitemaps, (c) crawl terms that don't prohibit it, (d) mid-market prices, (e) dimensions published on the page. The Masterplan's "broad" coverage grows from here once the pipeline is proven. The final list is an open decision (§10).
- **Legitimacy checks per retailer (recorded in `retailers.crawl_policy`):** robots.txt rules, Terms of Service review, affiliate program / product feed availability (preferred when it includes dimensions).
- **Discovery methods, in order of preference:**
  1. Affiliate/product feeds (CSV/XML) where available.
  2. XML sitemaps filtered by URL patterns for in-scope categories.
  3. Category listing pages crawled with pagination (only when 1 and 2 are unavailable).
- **In-scope V1 categories (closed vocabulary):** sofa, sectional, loveseat, armchair, accent_chair, coffee_table, side_table, console_table, tv_stand, bookshelf, bed_frame, nightstand, dresser, desk, office_chair, dining_table, dining_chair, rug, floor_lamp, bench, ottoman, cabinet. Each maps to a proxy family and to placement rules (§5).

### 4.3 Fetch

- `httpx` with HTTP/2, a clear User-Agent identifying the bot and a contact address, per-domain concurrency of 1–2, a delay of ≥ 2 s (or robots `crawl-delay`), exponential backoff on 429/5xx, and conditional requests (ETag/Last-Modified).
- **Playwright fallback** only for retailers flagged `requires_js`.
- **Store:** `product_pages` row (url, status, fetched_at, content_hash) + gzipped HTML in R2. Unchanged hash → skip extraction.
- **Circuit breaker:** > 20% errors in a run → pause the retailer and alert. Never attempt to evade blocking.

### 4.4 Extract

In priority order, merged field by field with provenance:
1. **JSON-LD / microdata** via `extruct`: name, sku, gtin, brand, offers.price, priceCurrency, availability, image[], url, sometimes `width/depth/height` or `additionalProperty`.
2. **Retailer-specific selectors** (small per-retailer config: CSS selectors for the spec table / dimensions block / breadcrumb category).
3. **LLM fallback (Haiku)** only for fields still missing, given a cleaned text extract (the spec section, ≤ 4k tokens) and a strict JSON schema output.

### 4.5 Dimension Parsing (the Critical Field)

1. **Locate** candidate dimension strings: JSON-LD dimension properties, spec-table rows matching `/dimension|overall|size|width|depth|height|length/i`, and title patterns.
2. **Regex/grammar parser** (unit-tested against a corpus of 300+ real strings collected in Phase 3), handling:
   - `84"W x 36"D x 33"H`, `W: 213 cm D: 91 cm H: 84 cm`, `84 in. W x 36 in. D x 33 in. H`
   - `213 x 91 x 84 cm` (ordered-triple convention per retailer config: W×D×H vs. L×W×H)
   - `7' 0" x 3'`, fractions (`33 1/2"`, `33½"`), decimals with commas (`213,5 cm`)
   - Units: in, ", ft, ', cm, mm, m
   - **Multiple dimension sets** (overall vs. seat vs. arm vs. packaged): pick **overall/assembled**. Explicitly discard "package", "box", "shipping", "carton".
   - Rugs: `8' x 10'`, `160 x 230 cm` (2D; height defaults to 10 mm)
   - Round items: `diameter 36"` → width = depth
   - Configurable products (variant sizes): each variant becomes its own product record if variant URLs/SKUs exist; otherwise take the default variant, only if its dimensions are unambiguous.
3. **LLM fallback** when the parser finds candidates but can't resolve them (ambiguous ordering, several sets). Haiku returns `{width, depth, height, unit, source_quote}`. **Grounding check:** every numeric value must appear in `source_quote`, and `source_quote` must appear verbatim in the page text. Otherwise reject.
4. **Provenance:** `dimension_source ∈ {jsonld, parser, llm_grounded}` and `dimension_confidence`.

### 4.6 Normalize

- **Units → integer mm**, rounded. Inches × 25.4. Keep `dimension_raw` for audit.
- **Axis semantics:** width = side-to-side as seen from the front, depth = front-to-back, height = floor to top. If a retailer uses L×W×H for tables/beds, map L→width and W→depth per category config.
- **Price → integer cents + ISO currency.** Sale price becomes `price_cents` and list price goes to `list_price_cents`. Price ranges (variants) use the default variant's price. **V1 is single-market (USD); non-USD products are excluded** (§10).
- **Category:** breadcrumb/category mapping table first. Haiku classification (title + image) into the closed vocabulary when unmapped. Store `category_confidence`.
- **Style and color tags (Haiku vision via Batch API):** closed style vocabulary (modern, mid_century, scandinavian, industrial, traditional, farmhouse, bohemian, minimalist, coastal, glam) with 1–3 tags, a dominant color hex (verified by k-means on the product image, not just the LLM), and a primary material.
- **Image embedding:** SigLIP 2 on the primary image → `pgvector`.

### 4.7 Validate

A product becomes `active` only if **all** hard checks pass. Otherwise it gets a `rejected_*` status with a reason:

| Check | Rule | Failure status |
|---|---|---|
| Dimensions present | width, depth, height all present (rugs: height defaulted) | `rejected_no_dimensions` |
| Dimension plausibility | Per-category min/max (e.g., sofa W 1300–4000, D 700–1200, H 600–1100 mm; coffee table H 300–600) | `rejected_implausible_dimensions` |
| Dimension consistency | Height not > 3× width for non-tall categories; no W=D=H identical triples unless category allows (cube ottoman) | `pending_review` |
| Price present & plausible | > 0, within category band ($10–$15,000) | `rejected_no_price` |
| URL safety | Domain in retailer allowlist, https, resolves to 200 on the product domain (no off-domain redirect) | `rejected_bad_link` |
| Image present | ≥1 image URL that returns image/* | `rejected_no_image` |
| Category mapped | In closed vocabulary with confidence ≥ 0.7 | `pending_review` |
| Availability | Not `Discontinued` | `unavailable` |

`pending_review` items are excluded from layouts and reviewed through a simple internal admin page (sort by frequency of failure reason, fix parser, re-run).

### 4.8 Deduplication

- **Within a retailer:** unique key `(retailer_id, retailer_sku)`, falling back to the canonical URL (normalized: lowercase host, strip tracking params, trailing slash). Re-crawls update the record in place.
- **Cross-retailer (same product sold by several shops):** Group into `duplicate_group_id` when (GTIN matches) OR (image embedding cosine ≥ 0.95 AND title similarity ≥ 0.8 AND dimensions within ±2%). V1 keeps all members but **shows only the cheapest active member** in shortlists and exposes "also sold at" on the product card.

### 4.9 Images, Prices, and Broken Links Over Time

- **Images:** Store the source image URL and cache **one 400 px WebP thumbnail** per product in R2 for fast, reliable UI. Full-size images are loaded from the retailer only when the product card opens. (Caching policy is an open legal question, §10.)
- **Price refresh:** A scheduled job re-fetches active products on a rolling window: every product at least every 7 days, products placed in any layout at least every 2 days. `price_history` rows record changes. The UI shows "Price as of <date>". Layout totals use the current price, and the placed-item snapshot is kept for "price changed" badges.
- **Link health:** Every refresh is also a link check.
  - 404/410, or a redirect to a non-product page (category/home), increments `consecutive_failures`.
  - At 2 failures: status `unavailable`. Existing layouts keep the item, marked "No longer available", with a one-click "find similar" swap.
  - At 5 failures, or 60 days unavailable: archive.
- **Outbound safety:** All buy links go through `GET /out/:productId`, which reads the stored canonical URL, re-checks the host against the retailer allowlist, appends affiliate params if configured, and issues a 302. Rendered links use `rel="noopener noreferrer"`. No user-supplied URLs exist in V1.

### 4.10 3D Representation of Catalog Products

Retailers almost never publish usable 3D models. V1 plan:

- **Parametric proxies (V1 default):**
  - For each proxy family (~20: `sofa_3seat`, `sofa_sectional_L`, `armchair`, `coffee_table_rect`, `coffee_table_round`, `bed_frame_platform`, `bed_frame_headboard`, `dresser`, `bookshelf_open`, `rug_rect`, `rug_round`, `floor_lamp_arc`, …), write a small TS generator that builds a low-poly mesh **from width/depth/height** with sensible proportions (arm width, leg height, seat height). The stand-in is exactly true to size without stretched-looking legs.
  - Materials are tinted with the product's dominant color, with a basic material type (fabric/wood/metal).
  - **Performance budget:** ≤ 2k triangles per item.
  - The product photo stays the primary "what it looks like" signal in the product card.
- **Image-to-3D (candidate upgrade) [EXPERIMENT E13]:** Generate GLBs offline for active products with an open image-to-3D model (e.g., TRELLIS-family, MIT-licensed **[VERIFY]**), rescale to exact product dimensions, decimate to ≤ 10k triangles, and pack with gltfpack. Cost is roughly tens of GPU-seconds per product. Adopt per category only if the result beats the proxy in a blind comparison and has no badly malformed shapes.

### 4.11 Making Products Available to the Layout System

- The layout engine queries `products` directly (Postgres), with composite indexes (§6).
- **Materialized view `layout_candidates`:** active products + current price + tags + footprint + proxy family, refreshed after each catalog run. It keeps layout queries simple and fast.
- **Minimum catalog for launch:** ≥ 40 active products in each of the ~12 most-used categories, spread across price bands. The layout engine degrades gracefully (skips a slot and says so) when a category is thin.

---

## 5. AI Furniture Placement

### 5.1 Separation of Responsibilities

| Decision | Owner | Can the AI break the room? |
|---|---|---|
| Room type (living/bedroom/office/dining) | LLM (vision on 3 keyframes + user override) | No |
| Which categories to include and their priority | LLM, constrained to the closed vocabulary | No |
| Budget split across categories | LLM proposes; deterministic code clamps and rebalances | No |
| Style direction | User choice (optional) or LLM inference | No |
| Product choice per slot | LLM picks **from a pre-filtered shortlist** (every option already fits the budget and free space) | No |
| Spatial intents ("against W2", "facing O1", "centered on sofa") | LLM, using only IDs from the room summary | No |
| Exact coordinates and rotations | **Deterministic solver** | — |
| Legality (collisions, clearances, doors, bounds, budget) | **Deterministic validator (final authority)** | — |

**Fallback:** If the LLM call fails, times out, returns an invalid schema twice, or its plan cannot be solved, the engine uses a **rule-based template plan** for the room type (fixed category list, fixed budget ratios, default intents). A layout is always produced.

### 5.2 Room Representation for Layout

**Derived deterministically from `RoomModel` (L1 room analysis):**
- `usable_floor`: floor polygon minus fixed obstacles minus door-swing keep-outs (shapely geometry, mm).
- **Door keep-out:** For each door, a rectangle `width × width` into the room from the opening (both sides of the swing when `swing = unknown`), plus a 900 mm-deep approach zone.
- **Window zones:** For each window, the rectangle in front of it, 600 mm deep. Items with height > `sill_mm − 50` placed there violate a soft constraint (blocks the window).
- **Wall runs:** For each solid wall, the free intervals along it not interrupted by openings/obstacles, with lengths.
- **Circulation graph:** Occupancy grid at 50 mm cells, dilated by a 380 mm "person radius". Used to check that each door connects to every other door and to the room's central free region.
- **LLM room summary** (compact JSON, not geometry dumps):
  ```
  { room_type_guess, area_m2, ceiling_mm, shape: "rectangular"|"L"|"irregular",
    walls: [{id, length_mm, free_runs_mm:[...], has:["O1:window"], faces:"W4"}],
    openings: [{id, type, wall, width_mm}],
    fixed: [{id, label, near_wall}],
    focal_candidates: ["W3 (window wall)", "W1 (longest free run 3.8m)"] }
  ```

### 5.3 Furniture Representation

- **Catalog item (from `layout_candidates`):** id, category, proxy family, `width_mm/depth_mm/height_mm`, price, style tags, color, embedding.
- **Category rules** (versioned config file `layout/category_rules.yaml`, not LLM-generated):
  ```
  sofa:
    placement: against_wall_preferred     # or float_allowed
    back_to_wall_gap_mm: [0, 150]
    front_clearance_mm: 450               # to nearest item in front
    side_clearance_mm: 0
    faces: focal
    max_height_under_window_mm: sill-50
  coffee_table:
    relation: in_front_of(sofa), gap_mm: [350, 500], center_aligned: true
    size_ratio_to_sofa_width: [0.45, 0.75]
  rug:
    layer: floor_covering                 # may overlap other items
    relation: under(sofa|coffee_table) front legs / under(bed) lower 2/3
  bed_frame:
    placement: headboard_to_wall
    side_access_mm: 600 (both sides for queen+, one side for twin/full)
    foot_clearance_mm: 900
  dining_chair:
    relation: around(dining_table), pullback_mm: 900
  ...
  ```

### 5.4 Placement Generation

**L2: AI plan (Claude Sonnet, one call, tool-schema output `LayoutPlan`):**
- **Input:** room summary, room type (user-selected or inferred), budget, currency, optional style, the category vocabulary with **price percentiles per category from the live catalog**, and default budget ratios for the room type.
- **Output:**
  ```
  LayoutPlan {
    room_type, style,
    slots: [{ slot_id, category, priority: "must"|"should"|"nice",
              budget_share, intent: { anchor: {wall|opening|slot}, relation,
              facing, notes } }],
    rationale (short, shown to user)
  }
  ```
- **Deterministic post-checks:**
  - Categories are in the vocabulary.
  - Budget shares are renormalized to 100% with a 5% unallocated reserve.
  - Each share is clamped so its allocation is ≥ that category's 10th-percentile price (otherwise the slot is demoted or dropped, lowest priority first).
  - Intents reference only existing IDs.
  - Slot counts are capped (≤ 10 items in V1).

**L3: Shortlist (SQL, deterministic).** For each slot:
- Category matches, `price_cents ≤ allocation × 1.15`.
- Footprint fits the target wall run / free region: `width_mm ≤ free_run − 2 × side_clearance`, `depth_mm ≤ available_depth`, `height_mm ≤ ceiling − 100` (and ≤ sill if the intent is under a window).
- Relation-dependent size rules (e.g., coffee table 45–75% of the chosen sofa's width, so slots are processed in dependency order: anchors first).
- Ranked by style-tag overlap + embedding similarity to the style centroid + price closeness to allocation. **Top K = 8.**

**L4: Product pick.** A Haiku call per layout (all slots in one call) selects one product ID per slot from the shortlists, favoring visual coherence (it gets titles, tags, colors, prices; optionally thumbnails for anchors). Its choice must be an ID from the shortlist; otherwise the top-ranked item is used. **Budget pass:**
- If the total exceeds the budget, repeatedly swap the item with the best "price saved per score lost" for a cheaper shortlist member, and if still over, drop the lowest-priority slot.
- If there is money left over (> 10% of budget), upgrade "must" slots within their shortlists.

**L5: Solver (deterministic, Python).**
1. **Order slots:** anchors (sofa/bed/dining table/desk) → dependents → floor coverings → accents.
2. **Candidate poses per slot, generated from intent:**
   - `against_wall(W)`: positions along each free run of W at 100 mm steps, back flush with the wall + gap, rotation facing into the room.
   - `in_front_of(slot)`: centered on the anchor's front axis at gap ∈ [min, max] in 50 mm steps.
   - `under(slot)`: rug centered/offset per rule.
   - `around(table)`: chairs at evenly spaced positions along table sides.
   - `float(region)`: grid positions in the free region at 200 mm steps × 4 rotations.
   - If the intent is unsatisfiable, relax it: preferred wall → any wall → float.
3. **Score** each candidate that passes the **hard** validator checks incrementally (fast OBB/polygon tests):
   - `+` intent satisfaction (right wall, facing target)
   - `+` clearance margin above minimums
   - `+` alignment (centered on wall run / focal point, parallel edges)
   - `−` soft violations (window blocked, walkway narrowed toward minimum)
   - `−` circulation penalty (grid BFS connectivity loss)
4. **Beam search** (width 5) over slots in order. If a slot has no valid pose, try the next product in its shortlist (smaller footprint first). If still none and priority ≠ must, drop the slot and record the reason ("No room for an accent chair that keeps the doorway clear").
5. **Seeded and deterministic:** the same plan + seed gives the same layout. "Regenerate" changes the seed and optionally asks the LLM for an alternative plan.

**L6: Final validation** runs the full validator (§5.5) on the complete layout. Any hard violation here is a bug → retry the solver with the violating slot removed, and log it to Sentry.

### 5.5 Deterministic Validator

Implemented twice with **one shared test suite**:
- **Python** (`workers/layout/validator.py`): used by the solver and final validation.
- **TypeScript** (`packages/geometry/validator.ts`): used by the browser for live editing and by `PATCH /layouts/:id/items` server-side.
- **Parity:** `fixtures/validation/*.json` contain (room, items, expected report) cases. Both implementations must produce identical reports in CI.

**Geometry primitives:** 2D oriented rectangles (footprints) + height intervals, with the room as a polygon. Separating Axis Theorem for OBB–OBB and polygon containment with a tolerance of 5 mm.

**Hard constraints (block AI; flagged red in manual edit):**

| ID | Rule |
|---|---|
| H1 | Footprint fully inside the floor polygon (tolerance 5 mm) |
| H2 | No 3D overlap between items unless the pair is allowed (floor coverings under anything; rug–rug not allowed) |
| H3 | No overlap with fixed obstacles or kept detected objects |
| H4 | No item (except floor coverings) inside a door keep-out zone |
| H5 | Height ≤ ceiling height − 50 mm |
| H6 | Every door remains reachable: circulation grid connects all doors and ≥ 60% of free floor area, with a 380 mm person radius (≈ 760 mm walkway) |
| H7 | Total layout price ≤ budget (**AI layouts only**; manual edits may exceed it, shown clearly as over budget) |

**Soft constraints (warnings with messages):**

| ID | Rule |
|---|---|
| S1 | Main walkway ≥ 900 mm (comfortable); H6 is the 760 mm minimum |
| S2 | Category clearances from `category_rules.yaml` (sofa–coffee table gap 350–500; bed side access 600; dresser/drawer front 900; dining chair pullback 900; desk chair 900) |
| S3 | Item taller than window sill standing within the 600 mm window zone |
| S4 | Against-wall items with the back not facing the wall (e.g., sofa rotated backwards) |
| S5 | Item over-sized for room (e.g., sofa width > 70% of its wall run, rug > 90% of room dimension) |
| S6 | Price stale (> 7 days) or product unavailable |

**Report output (`ValidationReport`):** per-item flags, global flags, measured values ("walkway narrows to 710 mm between sofa and bookshelf"), and a `fits: bool` headline. The report is what the UI shows. It is never an opaque AI opinion.

### 5.6 Manual Customization Mechanics

- **Move:** Drag on the floor plane (raycast against y = 0), snap to walls within 100 mm, snap rotation to 15°. The TS validator runs on every pointer-move (throttled to ~30 Hz; incremental: only the moved item vs. others + circulation grid recomputed on drop).
- **Swap:** `GET /layouts/:id/items/:itemId/alternatives` → server runs L3 with the current pose's free footprint (grown by the space vacated) and returns ranked products that **fit at this position** and are within `remaining_budget + current item price`. Sort options: best match / cheapest / similar style (embedding).
- **Add:** Choose a category → alternatives for a synthetic slot → a TypeScript single-item placer (same candidate generators and scoring as the solver, restricted to one item) proposes the best pose given the current layout, synchronously (§7.1). The user then adjusts.
- **Remove:** Immediate; budget updates live.
- **Save:** Debounced autosave (2 s) with `layout.version` for optimistic concurrency. On a conflict (two tabs), reload and notify.

---

## 6. Data Model

Postgres (Supabase). All user-owned tables have **RLS: `owner_id = auth.uid()`** for select/insert/update/delete. Workers use a separate role that bypasses RLS only on pipeline/catalog tables. IDs are UUIDv7 (time-ordered) unless noted. All tables have `created_at`, `updated_at`.

### 6.1 Users & Rooms

**`profiles`** (1:1 with `auth.users`)
- `id` (= auth.users.id, PK), `display_name`, `is_anonymous` (mirrors auth), `default_currency` (`USD`), `unit_preference` (`imperial`|`metric`), `scan_quota_override`, `last_active_at`
- Index: `last_active_at` (anonymous-retention sweeper)

**`rooms`**
- `id`, `owner_id` → profiles, `name`, `room_type` (`living`|`bedroom`|`office`|`dining`|`other`, nullable until inferred), `current_scan_id` → scans (nullable), `current_layout_id` → layouts (nullable), `thumbnail_key`, `status` (`active`|`deleting`)
- Indexes: `(owner_id, created_at desc)`, `status` partial where `deleting`

**`scans`** (one room can be re-scanned; the Masterplan's "Room" video/model fields live here)
- `id`, `room_id` → rooms (cascade), `owner_id` (denormalized for RLS), `video_key`, `video_meta` jsonb, `motion_key` (nullable)
- `status` (see §7.3), `stage`, `progress_pct`, `error_code`, `error_detail` (user-safe message), `retryable` bool
- `pipeline_version`, `upload_id` (R2 multipart), `upload_expires_at`
- `metrics` jsonb (frames_used, coverage, timings per stage, gpu type, cost estimate)
- `started_at`, `completed_at`
- Indexes: `(room_id, created_at desc)`, `(status)` partial where status in active states

**`room_models`** (the measured geometry; versioned because calibration/reprocessing creates new versions)
- `id`, `scan_id` → scans (cascade), `owner_id`, `version` int, `geometry` jsonb (`RoomModel`), `schema_version`
- `scale_factor`, `scale_confidence` (`high`|`medium`|`low`), `scale_sources` jsonb, `user_calibrated` bool
- `floor_area_mm2` bigint, `ceiling_height_mm` int (denormalized for listing)
- `shell_key`, `objects_key`, `is_current` bool
- Unique: `(scan_id, version)`. Partial unique: `(scan_id) where is_current`

**`measurements`** (user calibration input)
- `id`, `room_model_id` → room_models, `owner_id`, `kind` (`wall_length`|`ceiling_height`|`door_width`|`door_height`), `target_ref` (e.g., `W2`, `O1`), `value_mm`, `applied` bool

**`detected_objects`**
- `id`, `scan_id` → scans (cascade), `owner_id`, `label`, `label_group` (`removable`|`fixed`), `score`, `center_x_mm`, `center_z_mm`, `width_mm`, `depth_mm`, `height_mm`, `yaw_deg`, `disposition` (`removed`|`kept`), `crop_key`
- Index: `(scan_id)`

**`scan_artifacts`**
- `id`, `scan_id` → scans (cascade), `stage`, `kind` (`keyframes`|`recon`|`masks`|`points`|`shell`|`textures`|`objects`|`room_model`|`thumb`), `object_key`, `bytes`, `sha256`, `pipeline_version`, `expires_at` (nullable, for debug artifacts)
- Index: `(scan_id, kind)`

**`scan_events`** (append-only audit/debug trail)
- `id` bigserial, `scan_id`, `at`, `from_status`, `to_status`, `stage`, `message`, `data` jsonb
- Index: `(scan_id, at)`

### 6.2 Layouts

**`layouts`** (the Masterplan's "Budget" entity is folded in here: each layout carries its own budget. A room may have several layouts, e.g., "cheap" vs. "splurge".)
- `id`, `room_id` → rooms (cascade), `owner_id`, `room_model_id` → room_models (layout is bound to a geometry version)
- `name`, `source` (`ai`|`ai_edited`|`manual`), `status` (`generating`|`ready`|`failed`)
- `budget_cents`, `currency`, `style` (nullable), `room_type`
- `total_cents` (cached; recomputed on every item change), `fits` bool, `validation_report` jsonb
- `plan` jsonb (the `LayoutPlan`, for explainability and regeneration), `generation_meta` jsonb (model IDs, prompt version, seed, solver stats, fallback_used)
- `version` int (optimistic concurrency), `error_code`
- Indexes: `(room_id, created_at desc)`, `(owner_id)`

**`placed_items`**
- `id`, `layout_id` → layouts (cascade), `owner_id`, `product_id` → products (**restrict** delete; products are archived, never hard-deleted while referenced)
- `slot_id` (nullable for manual adds), `category`
- `x_mm`, `z_mm`, `elevation_mm`, `rotation_deg`
- `price_cents_at_placement`, `locked` bool (user pinned: regenerate keeps it)
- `validation_flags` text[]
- Index: `(layout_id)`, `(product_id)` (price-refresh prioritization and "unavailable" propagation)

### 6.3 Catalog

**`retailers`**
- `id` (smallint), `name`, `domain`, `allowed_hosts` text[], `crawl_policy` jsonb (robots notes, ToS decision, rate, requires_js), `extraction_config` jsonb (selectors, dim-order convention), `affiliate_config` jsonb, `status` (`active`|`paused`|`disabled`), `last_crawl_at`

**`product_pages`** (raw fetch ledger)
- `id`, `retailer_id`, `url`, `url_hash` (unique), `http_status`, `content_hash`, `raw_key`, `fetched_at`, `extract_status`, `extract_error`
- Index: `(retailer_id, fetched_at)`

**`products`**
- `id`, `retailer_id`, `retailer_sku`, `gtin` (nullable), `canonical_url`, `title`, `brand`
- `category`, `category_confidence`, `proxy_family`, `style_tags` text[], `color_hex`, `material`
- `width_mm`, `depth_mm`, `height_mm`, `dimension_raw`, `dimension_source`, `dimension_confidence`
- `price_cents`, `list_price_cents`, `currency`, `price_checked_at`
- `availability` (`in_stock`|`out_of_stock`|`preorder`|`unknown`)
- `image_url`, `thumbnail_key`, `image_embedding` vector(768) **[VERIFY dim for chosen model]**, `glb_key` (nullable, image-to-3D)
- `status` (`active`|`pending_review`|`unavailable`|`archived`|`rejected_*`), `status_reason`
- `duplicate_group_id` (nullable), `consecutive_failures`, `first_seen_at`, `last_seen_at`
- Unique: `(retailer_id, retailer_sku)`. Unique: `canonical_url`
- Indexes:
  - `(status, category, price_cents)`: primary shortlist filter
  - `(status, category, width_mm, depth_mm)`: footprint filter
  - GIN on `style_tags`
  - HNSW on `image_embedding`
  - `(price_checked_at)` partial where active: refresh scheduler
  - `(gtin)` partial where not null

**`price_history`**
- `product_id`, `at`, `price_cents`, `availability`. PK `(product_id, at)`.

**`crawl_runs`**
- `id`, `retailer_id`, `kind` (`discover`|`refresh`|`linkcheck`), `status`, `stats` jsonb (fetched, new, updated, rejected by reason, errors), `started_at`, `finished_at`

**Materialized view `layout_candidates`:** active, non-duplicate-hidden products joined with retailer status. Refreshed concurrently after catalog runs.

### 6.4 Jobs & Operations

**`jobs`**
- `id`, `type` (`process_scan`|`rescale_scan`|`generate_layout`|`purge_room`|`purge_user`|`catalog_discover`|`catalog_refresh`|`catalog_tag`|`product_assets`)
- `subject_type`, `subject_id`, `owner_id` (nullable for system jobs)
- `status` (`queued`|`running`|`succeeded`|`failed`|`canceled`), `attempt`, `max_attempts`
- `payload` jsonb, `result` jsonb, `error_code`, `error_detail`
- `dispatch_id` (Modal call ID), `locked_at`, `heartbeat_at`, `run_after` (backoff)
- Indexes: `(status, run_after)`, `(subject_type, subject_id)`, `(type, created_at)`
- **Idempotency:** partial unique `(type, subject_id) where status in ('queued','running')`. The same scan can't be processed twice concurrently.

**`usage_counters`**
- `owner_id`, `day`, `scans_started`, `layouts_generated`, `gpu_seconds`. PK `(owner_id, day)`. Also a global row for the daily GPU cap.

### 6.5 Relationships Summary

```
auth.users 1─1 profiles 1─* rooms 1─* scans 1─* room_models 1─* layouts 1─* placed_items *─1 products *─1 retailers
                                   │         └─* detected_objects        │
                                   │         └─* scan_artifacts          └─* measurements (via room_models)
                                   │         └─* scan_events
                                   └─ current_scan_id / current_layout_id pointers
products 1─* price_history ; retailers 1─* product_pages ; retailers 1─* crawl_runs
```

**Deletion semantics:** DB rows cascade from `rooms`. R2 objects are deleted by the purge job **before** the DB rows (§7.4), so we never lose track of stored files.

---

## 7. API + Processing

### 7.1 Synchronous vs. Asynchronous

| Operation | Mode | Reason |
|---|---|---|
| Auth, room CRUD, listing | Sync | Simple DB reads/writes |
| Presign upload parts, complete upload | Sync | Fast. The heavy work is dispatched. |
| Scan processing (S1–S9) | **Async (GPU)** | Minutes |
| Calibration rescale | **Async (CPU)**, typically ≤ 20 s | Recomputes geometry + shell mesh |
| Layout generation | **Async (CPU + LLM)**, typically 10–40 s | LLM latency + solver |
| Validate layout edits | **Sync** (TS in the browser, plus TS in the API on save) | Must feel instant |
| Alternatives / swap options | **Sync** | Indexed SQL + TS fit check, < 300 ms |
| Single-item auto-placement on "Add" | **Sync** (TS single-item placer in `packages/geometry`) | Interactive. The full beam search stays in Python. |
| Product search | Sync | Indexed SQL |
| Outbound buy link | Sync redirect | |
| Room/account deletion | Sync accept (hide immediately) + **async purge** | R2 deletes can be slow |
| Catalog crawl/refresh/tag | **Async (cron)** | Batch |

### 7.2 Endpoints (`/api/v1`, JSON, all require a Supabase session, anonymous or Google)

**Rooms & scans**

| Method & path | Purpose | Notes |
|---|---|---|
| `POST /rooms` | Create room `{name?, room_type?}` | Returns room |
| `GET /rooms` | List own rooms | Cursor pagination, includes thumbnail URL + current scan status |
| `GET /rooms/:id` | Room detail + current scan + current room model + layouts summary | |
| `PATCH /rooms/:id` | Rename, set room_type, set current layout | |
| `DELETE /rooms/:id` | Mark `deleting`, enqueue `purge_room` → 202 | |
| `POST /rooms/:id/scans` | Create scan, **check quota**, init R2 multipart → `{scan_id, upload_id, part_size, part_urls[1..k]}` | Checks per-user daily limit and global GPU cap |
| `POST /scans/:id/upload/parts` | Presign more part URLs `{part_numbers}` | |
| `POST /scans/:id/upload/complete` | `{parts:[{n, etag}], motion?}` → complete multipart, HEAD verify size ≤ limit, set `uploaded`, insert + dispatch `process_scan` → 202 | Idempotent |
| `POST /scans/:id/upload/abort` | Abort multipart, delete scan | |
| `GET /scans/:id` | Status, stage, progress, error (user-safe), metrics subset | Also available via Realtime |
| `POST /scans/:id/retry` | Only if `retryable` → re-dispatch from last completed stage | |
| `POST /scans/:id/cancel` | Set cancel flag. The worker checks between stages. | |
| `GET /scans/:id/assets` | Presigned GET URLs: shell GLB, objects.json, room_model.json, thumb | 15-minute TTL |
| `PATCH /scans/:id/objects` | `[{object_id, disposition: kept|removed}]` → new room_model version (fixed obstacles updated), sync (JSON-only change, no mesh rebuild) | |
| `POST /room-models/:id/measurements` | `{kind, target_ref, value_mm}` → enqueue `rescale_scan` → 202 | |

**Layouts**

| Method & path | Purpose | Notes |
|---|---|---|
| `POST /rooms/:id/layouts` | `{budget_cents, currency, style?, room_type?, keep_locked_from_layout_id?}` → insert layout `generating`, dispatch `generate_layout` → 202 | Quota-checked |
| `GET /layouts/:id` | Layout + items (with product summary + proxy params) + validation report | |
| `PATCH /layouts/:id` | Rename, change budget (re-validates H7 as a warning for edited layouts) | |
| `PATCH /layouts/:id/items` | Batch ops `{version, ops:[{op: add|move|swap|remove|lock, ...}]}` → server TS validation → persist → returns new version + report | 409 on version mismatch |
| `GET /layouts/:id/items/:itemId/alternatives` | Fitting swaps `?sort=match|price|style&cursor` | |
| `GET /layouts/:id/alternatives` | For "Add": `?category=` → fitting products + suggested pose | |
| `POST /layouts/:id/regenerate` | New seed / new plan, keeps locked items → new layout, 202 | |
| `DELETE /layouts/:id` | | |

**Catalog & shopping**

| Method & path | Purpose |
|---|---|
| `GET /products/:id` | Card detail: title, retailer, price + "as of", dimensions (in user's units), images, availability, "also sold at" |
| `GET /products/search` | `?category&max_price&max_w&max_d&style&q&cursor` |
| `GET /out/:productId` | Allowlist-checked 302 to retailer (not under `/api`, no auth required; logs click count only) |

**Account**

| Method & path | Purpose |
|---|---|
| `GET /me` | Profile, usage today, is_anonymous |
| `PATCH /me` | Units, currency |
| `DELETE /me` | Mark all rooms deleting, enqueue `purge_user` (purges all rooms, then deletes the auth user) → 202 |
| *(client SDK)* | Google sign-in / `linkIdentity` for anonymous → Google upgrade (no custom endpoint) |

**Internal (not user-facing)**
- Modal web endpoint `POST /dispatch` (bearer token): `{job_id}` → spawns the right function.
- Next.js `POST /api/internal/revalidate` is not needed: Realtime pushes row changes.

**Standard error envelope:** `{ error: { code, message, retryable, details? } }`. `message` is always user-safe and never includes stack traces or internal paths.

**Rate limiting:** Postgres-backed (`usage_counters`). V1 defaults:
- Anonymous: 3 scans/day, 10 layout generations/day.
- Google-signed-in: 10 scans/day, 50 layout generations/day.
- Global: daily GPU-seconds cap, after which scans queue until the next day with a clear message.
- Generic API: per-IP limits via Vercel's built-in firewall rules.

### 7.3 Scan Processing States

```
created ──▶ uploading ──▶ uploaded ──▶ queued ──▶ processing ─────────────▶ ready
   │            │                         │           │ (stage: ingest │ frames │
   │            └─▶ upload_failed         │           │  reconstruct │ segment │
   │                (expired/aborted)     │           │  fuse │ geometry │ scale │
   └─▶ canceled ◀──────────────────────────┴───────────┤  shell │ publish)    │
                                                       ├─▶ failed          ready_with_warnings
                                                       │   (error_code,     (low scale confidence,
                                                       │    retryable)       open boundaries,
                                                       └─▶ canceled          no openings found)

any state ──(user deletes room)──▶ (row hidden; purge job removes everything)
ready* ──(measurement / object disposition change)──▶ rescaling ──▶ ready*
```

`ready_with_warnings` is a sub-flag (`quality.warnings` non-empty) on `ready`, not a separate terminal state. The UI shows a banner with actions ("Add a measurement", "Mark a door").

### 7.4 Failure Handling

**Stage checkpointing:** Each stage writes its outputs under `…/{pipeline_version}/{stage}/` plus a `_SUCCESS` marker. A retry starts at the first stage without a marker. Stages are pure functions of their inputs, so reruns are safe.

**Error taxonomy:**

| Code | Stage | Class | Automatic action | User sees |
|---|---|---|---|---|
| `VIDEO_UNREADABLE` | ingest | User-fixable | None | "We couldn't read this video. Try recording again or uploading an MP4." |
| `VIDEO_TOO_SHORT_OR_BLURRY` | frames | User-fixable | None | Capture tips + "Record again" |
| `VIDEO_TOO_DARK` | frames | User-fixable | None | Lighting tips |
| `INSUFFICIENT_COVERAGE` | reconstruct/geometry | User-fixable | After 1 internal retry with alternate keyframe spacing | "We couldn't see enough of the walls…" with the specific missing-area hint if available |
| `RECON_FAILED` | reconstruct | System | Retry ×1 with alternate keyframes, then fail as `INSUFFICIENT_COVERAGE` | — |
| `GPU_OOM` | any GPU stage | System | Retry with N × 0.7 frames (min 40), max 2 times | — |
| `LAYOUT_EXTRACTION_FAILED` | geometry | System | Retry with alternate path (classical ↔ SpatialLM) | "Something went wrong; retry" |
| `MODEL_UNAVAILABLE` / infra 5xx | any | Transient | Exponential backoff 30 s, 2 min, 10 min (max 3 attempts) | "Taking longer than usual" |
| `TIMEOUT` | any | System | Retry ×1 from checkpoint | — |
| `CANCELED` | any | — | Stop at next stage boundary, delete partial artifacts | — |
| `INTERNAL` | any | System | Retry ×1, then fail + Sentry alert | "Something went wrong; retry" |

**Sweeper (Modal cron, every 5 min):**
- `running` jobs with `heartbeat_at` older than 10 min → mark the attempt failed → re-queue if attempts remain (the job resumes from its checkpoint).
- `queued` jobs older than 15 min with no dispatch → re-dispatch.
- Scans stuck in `uploading` past `upload_expires_at` (24 h) → `upload_failed`. An R2 lifecycle rule aborts incomplete multipart uploads after 1 day.

**Layout job failures:**
- LLM errors → template fallback (never a user-visible failure).
- Solver can't place any "must" slot → `failed` with `ROOM_TOO_SMALL_FOR_BUDGET_OR_TYPE` and suggestions (different room type, higher budget, remove kept objects).
- Catalog too thin for a category → skip the slot with an explanation.

**Deletion (`purge_room`):**
1. Room is already hidden (status `deleting`) and RLS excludes it from all reads.
2. Cancel any running jobs for its scans and wait up to 2 min for stage-boundary stop.
3. Delete R2 prefix `rooms/{room_id}/` with paginated list + batch delete, verified by re-listing to zero.
4. Delete DB rows (cascade).
5. On failure, retry with backoff. After 3 failures, alert. The room stays hidden meanwhile.

**Anonymous retention:** See §10 (proposed: purge anonymous users inactive > 90 days, with the policy stated in the UI before recording).

### 7.5 Security Notes Specific to Processing

- Videos are untrusted input. FFmpeg/OpenCV decode only inside Modal containers with no DB superuser credentials, runs with resource/time limits, and uses a pinned, patched FFmpeg build.
- R2 bucket is private. No public URLs. Object keys include UUIDs. Presigned GETs are issued only after an RLS-checked ownership read.
- The worker DB role cannot read `auth` schema tables and can only write pipeline/catalog tables.
- Secrets live in Vercel env and Modal secrets only; `.env.example` in the repo, nothing real committed.
- LLM prompts receive only room geometry summaries and catalog data, **never video frames of the user's home**. The one exception is floor-material classification crops and room-type inference keyframes (Tier 0 appearance), which are sent to the Claude API. Disclose this in the privacy notice, or replace with a local classifier (§10).

---

## 8. Development Plan

Phases are ordered by **technical risk**, not by the Masterplan's feature grouping. Two tracks run in parallel where dependencies allow:
- **Track CV** (person A): video → RoomModel.
- **Track Product** (person B): catalog, then layout, then web.

Durations are rough sizing for two part-time developers and will be revised after Phase 1.

```
Week:   0    2    4    6    8    10   12   14   16   18   20   22
CV:     [P0][====P1 Recon Gate====][==P2 Objects+Shell==][==P5 Productionize==]
Prod:   [P0][==P3 Catalog Pipeline==][===P4 Layout Engine===][==P6 Web App===]
                                                                 [==P7 Beta==]
```

---

### Phase 0 — Foundations & Evaluation Dataset (~1–2 weeks)

**Objective:** Everything needed to run experiments and measure results objectively.

**Tasks**
- Create the monorepo skeleton (pnpm + uv), lint/format/typecheck, GitHub Actions CI.
- Write `packages/schemas`: `RoomModel`, `ObjectsFile`, `LayoutPlan`, `ValidationReport` v0 + TS/Pydantic codegen + CI staleness check.
- Hand-author **6 fixture RoomModels** from real ground-truth measurements (rectangular, L-shaped, small bedroom, open boundary, many openings, narrow). These unblock Track Product.
- Assemble the **Tier A evaluation dataset** (§3.10): 12–15 **ARKitScenes** scenes converted
  to `eval/ground_truth/*.yaml` by `eval/adapters/arkitscenes.py`. Free, open download, no
  application, no measuring, no self-capture.
- Build the `eval/` harness: run pipeline → compare against ground truth → metrics table (CSV + markdown report).
- Complete the license verification table (§1.2) in `docs/model-licenses.md`, **including the
  terms of any public dataset used** — they are evaluation-only and must be recorded like a
  non-commercial checkpoint.
- **E9 browser recording spike:** a throwaway HTML page testing MediaRecorder, run on whatever
  target devices are actually to hand. iOS Safari is the deciding case.

**Moved out of Phase 0** (revised 2026-09-17):
- **Infrastructure accounts** (Modal workspace, R2 dev/prod buckets, Supabase dev/prod
  projects, Sentry) → **start of Phase 1**, which is the first thing that reads them. The
  runbook is written and waiting in `docs/infrastructure-setup.md`, so this is an hour of
  signups on the day, not a research task. The one exception is **requesting Hugging Face
  access to the gated checkpoints** (`facebook/VGGT-1B-Commercial`, `facebook/sam3`), which is
  free, has approval latency, and should be clicked during Phase 0.
- **Tier B self-capture** (5–8 of our own rooms) → **after the Phase 1 gate**, per §3.10.
  It now carries E5 as well as E2, E3 and E8, because ARKitScenes has no opening
  annotations.

**Dependencies:** None.

**Tests:** Schema round-trip tests (TS ↔ Python ↔ JSON); ground-truth file validation; adapter
conversion tests; the eval harness end-to-end on the committed dummy run.

**Definition of done:** 12–15 Tier A ground-truth files committed and passing validation; eval
harness runs end to end on a dummy result; fixtures committed; license table filled (models +
dataset terms); E9 findings written up with at least one real device measured.

---

### Phase 1 — Video → 3D Room Proof of Concept (THE GATE) (~4–6 weeks)

**Objective:** Prove that a phone room video reliably produces an accurate `RoomModel` (walls, floor, ceiling, openings, scale) within time and cost budgets. **No web app, no database, no product features.** Scripts + Modal functions + a bare debug viewer.

**Tasks**
1. S1–S2 ingest/frames module with quality metrics; unit tests on synthetic and real clips.
2. S3 reconstruction adapters for candidates A, B, C (common interface: frames → poses, intrinsics, depth, confidence) + D baseline run offline.
3. Profile VRAM and time per candidate at N = 40/80/100/150 on L40S and A100-80 (E7).
4. S4 minimal: SAM 3 floor/wall/ceiling/door/window + furniture prompts (needed for structure point partitioning and openings).
5. S5 gravity + Manhattan alignment, structure vs. object point partition.
6. S6 classical geometry extraction (floor/ceiling heights, wall polygon, openings) + sanity checks.
7. S6 SpatialLM candidate adapter (E4).
8. S7 scale fusion (model, MoGe-2, door prior, user measurement) (E2, E3).
9. S8 minimal shell (Tier 0 flat grey + median colors) → GLB.
10. **Debug viewer:** a single static page (three.js) showing point cloud + fitted RoomModel + shell side by side, loaded from R2.
11. Run the full bake-off E1–E8 on the whole dataset; write `eval/reports/phase1.md` with the chosen model, metrics, failure cases, and the go/no-go call.

**Dependencies:** Phase 0 Tier A dataset + harness + license table. **Phase 1 opens by doing the infrastructure setup moved out of Phase 0** (`docs/infrastructure-setup.md`): Modal workspace, R2 buckets and Sentry are needed before the first GPU run; Supabase is not needed until Phase 5.

**Tests**
- Unit: frame scoring, keyframe selection determinism, alignment on synthetic point clouds (known rotation recovered within 1°), polygon extraction on synthetic density maps (rect, L, with furniture blobs against walls), opening rectangle fitting, scale fusion math.
- Integration: a 20 s fixture video runs end to end on Modal in CI (manual/weekly trigger, GPU cost) and produces a schema-valid RoomModel.
- Evaluation: the E1–E8 metrics table is the acceptance test.

**Definition of done**
- The go/no-go criteria in §3.10 are evaluated and documented.
- One reconstruction model and one geometry path are chosen (or both paths kept with a defined fallback order).
- The pipeline produces schema-valid RoomModels for ≥ 80% of captures (≥ 90% of "careful" captures).
- Median uncalibrated wall-length error ≤ 5% (or the "required calibration" branch is triggered and flagged to the Masterplan).
- p95 time ≤ 15 min, cost ≤ $1/scan, measured.

---

### Phase 2 — Furniture Detection, Removal & Room Shell (~3–4 weeks, Track CV)

**Objective:** Produce the clean, viewable, furniture-free room with detected furniture as removable objects, and finalize the pipeline outputs.

**Tasks**
- Full S4 prompt set (removable/fixed groups), track merging, 3D OBB fitting, `objects.json`.
- Verify that furniture points never pull walls inward (E6) and harden the S6 exclusion.
- Tier 0 appearance: plane-consistent color sampling, white-balance normalization, floor material classification + CC0 material library.
- Shell mesh with opening cutouts, window glass, door frames; gltfpack; size budget checks.
- Tier 1 projected textures + LaMa hole fill (time-boxed to 1.5 weeks) → E10 blind comparison → adopt or defer.
- `rescale_scan` CPU path from stored intermediates.
- Finalize RoomModel schema v1 (freeze; subsequent changes require migration notes).

**Dependencies:** Phase 1 go decision; chosen model.

**Tests:** OBB fitting unit tests; mesh validity (watertight walls, cutouts inside walls, correct normals); GLB size budget test; rescale produces proportionally correct geometry (×1.1 measurement → all lengths ×1.1 ± 0.5%); visual snapshot of the shell render for 6 fixture scans (headless three.js screenshot, reviewed on change); E6 metrics.

**Definition of done:** For every Phase 1 capture that passed, the pipeline outputs a shell GLB within budget, objects.json meeting E6 recall, and correct opening cutouts. Tier 1 decision documented. RoomModel schema v1 frozen.

---

### Phase 3 — Furniture Catalog Pipeline (~4–5 weeks, Track Product, parallel with P1–P2)

**Objective:** A trustworthy, dimension-verified, deduplicated catalog of ≥ 500 active products across the core categories, refreshed automatically.

**Tasks**
- Retailer shortlisting: robots/ToS/affiliate review → pick 5–8 → `retailers` config.
- Supabase catalog migrations (retailers, product_pages, products, price_history, crawl_runs) + worker role.
- Discover (feeds/sitemaps), polite fetcher with circuit breaker, raw page storage.
- JSON-LD/selector extraction; **dimension parser with ≥ 300-string test corpus**; grounded LLM fallback.
- Normalization (units, axes, price, category mapping), validation rules, reject reasons.
- Haiku vision tagging via Batch API; k-means color; SigLIP 2 embeddings; thumbnails.
- Dedupe (within retailer + cross-retailer grouping).
- Scheduled refresh + link health; `layout_candidates` materialized view.
- Internal admin page (can be a protected Next.js route): reject-reason dashboard, product inspector, re-run extraction.
- **E13 (optional, time-boxed):** image-to-3D on 50 products vs. parametric proxies.

**Dependencies:** Phase 0 infra only. Independent of CV.

**Tests**
- Unit: dimension parser corpus (target ≥ 98% correct on the labeled corpus, 0% silently wrong: ambiguous → reject, not guess), unit conversions, URL canonicalization, category mapping.
- Snapshot tests: saved HTML per retailer → expected extracted record (detects retailer markup changes).
- LLM grounding check tests (fabricated numbers rejected).
- **Data audit:** manually verify 100 random active products against their live pages → **dimension accuracy ≥ 97%, price accuracy ≥ 95%, link validity ≥ 98%**.

**Definition of done:** Audit thresholds met; ≥ 40 active products in each of the 12 core categories; nightly refresh running unattended for 1 week with the reject-rate dashboard stable.

---

### Phase 4 — Layout Engine (~4–5 weeks, Track Product)

**Objective:** Given a RoomModel, budget, and catalog, generate layouts that always pass hard constraints, respect the budget, and look sensible to a human.

**Tasks**
- `category_rules.yaml` for all categories.
- L1 room analysis (usable floor, door keep-outs, window zones, wall runs, circulation grid).
- Python validator (H1–H7, S1–S6) + **TS validator** + shared parity fixtures.
- L2 LLM planner (prompt, tool schema, post-checks) + template fallback plans per room type.
- L3 shortlist SQL + ranking; L4 product pick + budget rebalancing.
- L5 solver (candidate generators, scoring, beam search, relaxation, product downsizing).
- TS single-item placer (for "Add").
- Parametric furniture proxy generators (`packages/furniture-proxies`) for all proxy families.
- **Layout review tool:** a local page rendering fixture room + layout top-down and in 3D, with the validation report, for fast human review.
- **Layout eval set:** 6 fixture rooms + real Phase 1/2 RoomModels × 3 budgets (low/mid/high) × 2 room types = ~60 cases.

**Dependencies:** Phase 0 fixtures (can start before P1 finishes); Phase 3 catalog (can start with a seeded catalog of ~150 hand-entered products, then switch).

**Tests**
- Unit: SAT collision, containment, door keep-out geometry, circulation BFS, every H/S rule with pass/fail fixtures.
- **Parity:** Python and TS produce identical reports on ≥ 100 validation fixtures (including randomized property-based cases generated by Hypothesis and replayed in TS).
- **Property tests:** for random rooms/budgets, every generated layout has zero hard violations and total ≤ budget.
- **Chaos tests:** the LLM returns invalid JSON / nonexistent IDs / absurd budget shares → the engine still returns a valid layout via clamping or template.
- **Human eval:** both of us score the 60-case eval set 1–5 on "would I actually arrange it this way"; target mean ≥ 3.5, no case scoring 1 because of a rule bug.
- Performance: generation p95 ≤ 40 s (including LLM); solver alone ≤ 5 s.

**Definition of done:** 100% of eval cases produce a layout with zero hard violations and within budget; parity suite green; human eval target met; LLM cost per layout measured and ≤ $0.10.

---

### Phase 5 — Productionize Backend & Pipeline (~3 weeks, Track CV)

**Objective:** Turn the scripts into a reliable, observable, secure service.

**Tasks**
- All remaining migrations (rooms, scans, room_models, measurements, detected_objects, scan_artifacts, scan_events, layouts, placed_items, jobs, usage_counters) + **RLS policies** + worker role grants.
- Supabase Auth: Google provider, anonymous sign-in, identity linking.
- Modal app: dispatch endpoint, `process_scan` with stage checkpoints, heartbeats, cancellation checks, error taxonomy, `rescale_scan`, `generate_layout` wrapper, `purge_room`/`purge_user`, sweeper cron, catalog crons.
- Cold-start optimization (weights on Volume, memory snapshots) and GPU type finalization.
- Quotas and global GPU cap.
- R2 lifecycle rules (multipart abort, debug artifact expiry).
- Sentry + structured logs + `scan_events`.
- API route handlers for all §7.2 endpoints with Zod schemas + error envelope.

**Dependencies:** Phase 2 (final pipeline), Phase 4 (layout engine).

**Tests**
- **RLS tests** (local Supabase via CLI): user A cannot read/update/delete user B's rooms, scans, layouts, items, or presign their assets; anonymous user data survives Google linking.
- API integration tests for every endpoint (happy path + auth + validation + 409 concurrency).
- **Failure injection:** kill a worker mid-stage → sweeper resumes from checkpoint; forced OOM → frame-reduction retry; corrupt video → correct user-fixable error; cancel mid-reconstruction → stops and cleans up.
- **Deletion test:** create a room with all artifacts → delete → R2 prefix empty and DB rows gone; the same for `DELETE /me`.
- Idempotency: double `upload/complete` → one job.
- Load sanity: 10 concurrent scans → all complete, GPU cap respected.

**Definition of done:** A scan submitted via API (curl script) goes all the way to a ready layout on the prod-like environment; all failure-injection tests pass; RLS suite green; deletion verified.

---

### Phase 6 — Web App Experience (~4–5 weeks, Track Product, then both)

**Objective:** The complete V1 user journey in the browser on desktop and mobile.

**Tasks**
- App shell, routing, anonymous session bootstrap, Google sign-in / upgrade prompt ("Save your rooms").
- **Capture flow:** coach screens, `getUserMedia` recorder with timer and rotation-speed hint, motion sidecar, preview, file upload alternative, Uppy multipart upload with resume and progress.
- **Status screen:** Realtime stage progress, estimated time remaining, "we'll keep working if you leave" messaging, user-fixable error states with guidance, retry.
- **Room viewer (R3F):** GLB loading with progress, orbit + first-person walk modes, top-down 2D mode (orthographic, best for editing on phones), ghost-object toggle, keep/remove object controls, dimension overlay (wall lengths in the user's units), scale-confidence banner + measurement input.
- **Layout setup:** budget input (currency formatted), optional style chips, room type confirmation → generate → progress.
- **Layout editor:** proxies instantiated from params; select/drag/rotate; live TS validation with red outlines + clearance callouts; furniture list panel; **always-visible budget tracker**; swap drawer (alternatives), add-by-category, remove, lock, regenerate; autosave with conflict handling.
- **Tap-to-shop product card:** image, title, retailer, price + "as of", dimensions, availability, "Buy at <retailer>" via `/out/:id`, "also sold at".
- **My rooms:** list, rename, delete (confirm), account deletion in settings.
- Performance work: meshopt/KTX2 decoders, `frameloop="demand"`, DPR cap, instancing for repeated items (dining chairs), BVH raycasting, code-split the 3D bundle.
- Accessibility: keyboard alternatives for select/move/rotate in the editor, focus management, contrast, reduced motion.

**Dependencies:** Phase 5 APIs (can start earlier against mocked APIs + fixture RoomModels/layouts).

**Tests**
- Component/unit tests for budget math, unit formatting, editor store reducers.
- **Playwright E2E:**
  1. Anonymous user uploads a fixture video (pipeline mocked to return a fixture result quickly) → sees status → room → generates layout → swaps an item → budget updates → buy link redirects correctly.
  2. Anonymous → Google link → rooms retained.
  3. Delete room.
  4. User-fixable error displays guidance.
  5. Nightly, against the real pipeline: 1 real short video.
- **Real-device capture test** on iOS Safari + Android Chrome (E9 re-verified in the real UI).
- **Performance tests (§9.2 budgets)** on a mid-range Android and an older iPhone.
- Visual regression screenshots at 375 / 768 / 1440 widths for key screens.

**Definition of done:** Both of us complete the full journey on our own phones and laptops with real rooms; E2E suite green; performance budgets met on the reference devices.

---

### Phase 7 — Personal Beta & Hardening (~3–4 weeks, both)

**Objective:** Real usage by a small trusted group (Masterplan Phase 5); fix what matters.

**Tasks**
- Onboard 5–15 testers; privacy notice and data-use disclosure; in-app "report a problem with this scan/layout" (attaches scan_id/layout_id).
- Weekly review of `scan_events` failure codes, scale confidence distribution, measurement-correction magnitudes (ground truth by proxy), layout regenerate/swap rates (a proxy for layout quality), buy-link clicks.
- Tune capture coaching, keyframe counts, solver scoring weights, category rules, catalog gaps.
- Cost review against §1.3; adjust quotas and GPU type.
- Decide on post-beta items (Tier 1 textures, image-to-3D, retailers to add).

**Dependencies:** Phase 6.

**Tests:** Regression eval (Phase 1 metrics) re-run on every pipeline change; layout eval re-run on every solver/rules change; beta scan success rate tracked.

**Definition of done:** Beta scan success rate ≥ 80%, zero data-access incidents, deletion verified on real accounts, and a prioritized post-beta backlog written.

---

## 9. Testing + Risks

### 9.1 Most Important Tests (Cross-Phase)

| Test | Type | Why it matters | Gate |
|---|---|---|---|
| Reconstruction eval (E1–E8) on ground-truth dataset | Evaluation harness | Proves the core promise ("fits your space") | Phase 1 go/no-go; re-run on every pipeline change |
| Dimension parser corpus + 100-product live audit | Unit + manual audit | Wrong furniture dimensions silently break the core promise | Phase 3 DoD; audit repeated monthly |
| Validator parity (Python ↔ TS) | Contract/property | The AI and the editor must agree on what "fits" means | Every CI run |
| Layout property tests (zero hard violations, within budget) | Property-based | AI output never reaches the user unvalidated | Every CI run |
| LLM chaos tests | Fault injection | LLM misbehavior must degrade gracefully | Every CI run |
| RLS isolation suite | Integration | Home videos are sensitive; cross-user access is the worst-case bug | Every CI run touching DB/API |
| Deletion completeness (DB + R2) | Integration | Masterplan privacy commitment | Every release |
| Pipeline failure injection (kill/OOM/corrupt/cancel) | Integration | Background jobs must never hang silently | Phase 5 DoD; weekly |
| Real-device capture (iOS Safari, Android Chrome) | Manual + E2E | If recording/upload fails on phones, nothing else matters | Phase 6 DoD; each browser major version |
| 3D performance budgets | Automated (Playwright + Chrome tracing) + manual on devices | Mobile viewers must stay usable | Phase 6 DoD; CI bundle/asset size check every PR |
| Full-journey E2E | Playwright | Everything connected | Every PR (mocked pipeline); nightly (real pipeline) |

**Coverage target:** ≥ 80% line coverage for library code (`packages/*`, `workers/*` excluding Modal wrappers and model adapters that only call third-party inference). The evaluation harnesses, not line coverage, are the real quality gates for CV and layout quality.

### 9.2 Browser Performance Budgets

| Metric | Budget |
|---|---|
| Room shell GLB | ≤ 3 MB (Tier 0) / ≤ 12 MB (Tier 1) |
| Per-furniture proxy | ≤ 2k triangles (proxy) / ≤ 10k (image-to-3D) |
| Full scene | ≤ 150 draw calls, ≤ 100k triangles |
| Frame rate while navigating/dragging | ≥ 30 fps on reference mid-range Android (e.g., Pixel 6a class) and iPhone 12 class; ≥ 60 fps desktop |
| Time to interactive 3D (room loaded, 4G) | ≤ 5 s p75 |
| Initial JS (non-3D routes) | ≤ 200 kB gzipped; 3D bundle lazy-loaded |
| Validation latency per drag update | ≤ 8 ms p95 in browser |

### 9.3 Technical Risks

| # | Risk | Likelihood | Impact | Mitigation | Early warning signal |
|---|---|---|---|---|---|
| R1 | **Reconstruction quality insufficient** on real phone videos (clutter, blank walls, low light) | Medium | **Critical** | Phase 1 gate before any product build; multiple candidate models; capture coaching; classical + learned geometry paths; manual corner-tap fallback designed if needed | E1/E4 success rate < 80% |
| R2 | **Dimension accuracy** without a user measurement is too poor to trust "fits" | **High** | **Critical** | Multi-source scale fusion with confidence; measurement input promoted to required if E2 fails; clear confidence messaging; clearance margins in validator absorb small errors | E2 median error > 5% |
| R3 | **Furniture removal** leaves wrong walls (furniture against walls pulls wall geometry) or ugly surfaces | Medium | High | Shell-from-geometry approach avoids 3D inpainting; object-point exclusion before wall fitting; Tier 0 flat materials as guaranteed fallback; users can mark kept/removed objects | E6 wall pull-in cases; Tier 1 artifacts |
| R4 | **GPU memory** exceeds a single affordable GPU at useful frame counts | Low–Medium | High | Frame-count adaptation, chunked reconstruction with Sim(3) alignment, sequential stage loading/unloading, 80 GB fallback tier | E7 VRAM > 45 GB at N = 100 |
| R5 | **Processing time / cold starts** make the wait feel broken | Medium | Medium | Async UX with Realtime stages; memory snapshots; weights on Volume; parallelize CPU stages; optional warm container during beta hours | p95 > 15 min |
| R6 | **GPU cost** runs away (abuse, retries, loops) | Low–Medium | Medium | Per-user quotas, global daily GPU cap, max retries, hard timeouts, idempotent job keys, cost dashboard from `metrics` | Daily GPU-seconds trend |
| R7 | **Furniture data quality:** wrong dimensions (package vs. assembled, swapped axes), stale prices | **High** | **Critical** | Grammar parser + grounded LLM + plausibility checks + reject-on-ambiguity; monthly live audit; price refresh; "as of" labeling | Audit accuracy < 97%; rising reject rate for a retailer |
| R8 | **Scraping access / legal:** retailers block the crawler or ToS forbids it | Medium | High | Choose retailers by policy review; prefer affiliate feeds; polite crawling; circuit breaker; keep retailer set diversified; no evasion | 403/429 rates, crawl circuit-breaker trips |
| R9 | **3D browser performance** on mid-range phones | Medium | Medium | Parametric low-poly proxies, meshopt/KTX2, demand rendering, DPR caps, 2D top-down editing mode | FPS budget failures in Phase 6 device tests |
| R10 | **In-browser recording** inconsistent (iOS Safari codecs, backgrounding kills recording, huge files) | Medium | High | E9 spike in Phase 0; upload-a-file fallback; chunked resumable upload; client-side duration/size checks | E9 failures on any target browser |
| R11 | **Model licenses** prevent commercial use later | Medium | Medium | License gate (§1.2) before adoption; commercially licensed checkpoints only in production | Any candidate that is NC-only |
| R12 | **LLM layouts look unnatural** even when valid | Medium | Medium | Rules and relations do the heavy lifting; LLM chooses among valid options; human eval set; template fallback; easy manual editing | Human eval < 3.5; high regenerate rate in beta |
| R13 | **Proxy furniture undermines trust** ("that's not what the sofa looks like") | Medium | Medium | Product photo prominent in card; dominant color + material tint; E13 image-to-3D as upgrade path | Beta feedback |
| R14 | **Open-plan / irregular rooms** break the polygon model | Medium | Low–Medium | `open` wall segments, warnings, V1 scope messaging ("works best for enclosed rooms") | Beta failure codes |
| R15 | **Model ecosystem churn** (better models monthly) tempts constant rework | High | Low | Adapter interface for S3/S4; eval harness makes swaps a measured, one-day decision; `pipeline_version` on all artifacts | — |

---

## 10. Open Decisions

| # | Decision | Why it can't be made now | What resolves it | When |
|---|---|---|---|---|
| D1 | **Reconstruction model** (MapAnything vs. VGGT + MoGe-2 vs. Depth Anything 3) | Published benchmarks don't reflect cluttered phone videos of real homes; memory/time on our GPUs is unmeasured; licenses must be verified | Phase 1 bake-off E1, E2, E7 + license table | End of Phase 1 |
| D2 | **Geometry extraction path** (classical vs. SpatialLM vs. both with fallback) | SpatialLM accuracy on our point clouds (which come from monocular reconstruction, not LiDAR) is unknown | E4 on the eval dataset | End of Phase 1 |
| D3 | **Is a user measurement optional or required?** | Depends entirely on uncalibrated scale accuracy | E2 vs. E3 results; if required, update the Masterplan (a product change) | End of Phase 1 |
| D4 | **GPU tier & provider** (L40S vs. A100-80 on Modal; alternatives like RunPod) | Needs VRAM/time profile of the chosen stack and real per-scan cost | E7 profiling + one week of cost data | Phase 1 → Phase 5 |
| D5 | **Tier 1 projected textures in V1, or flat materials only** | Visual quality vs. processing time/complexity is subjective and data-dependent | E10 blind comparison | End of Phase 2 |
| D6 | **Furniture 3D representation:** parametric proxies only vs. image-to-3D for some categories | Image-to-3D quality/consistency and cost at catalog scale is unknown | E13: 50 products, blind comparison, cost per product, failure rate | Phase 3–4 (optional; can slip to post-beta) |
| D7 | **Which retailers, and scraping vs. affiliate feeds** | Requires ToS/robots review, affiliate program applications (approval isn't guaranteed), and checking which sites publish usable dimensions | Retailer review worksheet in Phase 3 week 1; affiliate applications | Early Phase 3 |
| D8 | **Product image handling** (hotlink vs. cached thumbnails) | Legal/ToS question per retailer; affiliate programs often grant image usage rights | Retailer/affiliate terms review (D7) | Early Phase 3 |
| D9 | **Market scope: US/USD + imperial display only for V1?** | A product decision that shapes retailer choice, currency handling, and unit defaults | Your call. Recommended: US-only V1, metric stored internally, imperial/metric display toggle. | Before Phase 3 |
| D10 | **Anonymous data retention** | The Masterplan says "persistent by default" but anonymous sessions can be lost (cleared cookies), leaving orphaned home videos we can't return to anyone | Your call. Recommended: purge anonymous accounts inactive > 90 days, disclosed before recording, with a "sign in to keep forever" prompt. | Before Phase 5 |
| D11 | **Sending room imagery to a third-party LLM** (floor-material and room-type classification) | Privacy stance vs. accuracy/effort | Test whether a local classifier (e.g., SigLIP 2 zero-shot on floor crops) is accurate enough; if yes, keep all home imagery off third-party APIs | Phase 2 |
| D12 | **LLM provider for layout planning** (Claude vs. an open-weights model) | Quality difference on spatial planning and real cost per layout are unmeasured | Run the Phase 4 layout eval set with `claude-sonnet-5`, `claude-haiku-4-5`, and one open-weights model; compare human scores vs. cost | Phase 4 |
| D13 | **V1 room corrections scope:** can users add/move/delete doors and windows, or only calibrate scale and keep/remove objects? | Depends on E5 opening-detection accuracy; editing openings adds UI scope | E5 results: if door recall < 85%, a minimal opening editor becomes required | End of Phase 1 |
| D14 | **Original-room photoreal view** (Gaussian splat "before" view using gsplat + a web splat renderer) | Nice-to-have that adds GPU time, storage, and mobile performance risk | Revisit after beta feedback | Post-beta |
| D15 | **Local GPU for development** | Unknown whether either of us has a ≥ 24 GB NVIDIA GPU; affects iteration speed vs. Modal dev spend | Inventory hardware in Phase 0; otherwise budget Modal dev credits | Phase 0 |
| D17 | **Apple RoomPlan as the capture path** — **CLOSED 2026-09-17: rejected.** RoomPlan outputs walls, dimensions, openings and furniture parametrically, which is essentially a finished `RoomModel`, and would remove the Phase 1 gate entirely. Rejected on two grounds. (1) **Product:** it is native Swift, iOS-only, and requires a LiDAR device (iPhone 12 Pro or later Pro, iPad Pro) — it cannot run in a browser at all, which contradicts masterplan.md line 28 ("directly through the web app, usable on both desktop and mobile browsers") and would cut the audience to recent iPhone Pro owners. (2) **Practical:** we have neither a Mac nor a LiDAR device, so it is not even buildable here. | — | — | Closed |
| D16 | **Open-plan / multi-room support** | Scope depends on how common it is among testers and how badly `open` segments perform | Beta usage data | Post-beta |

---

**On D17, for the record.** The interesting variant considered was not "ship RoomPlan" but "use RoomPlan to de-risk": build the downstream product (catalog, layout engine, 3D editor) against RoomPlan's near-perfect room output, prove people want *that*, and treat video→3D as the thing that later widens the audience. It inverts the plan's risk ordering in a defensible way. It is closed only because of the hardware, so if a Mac and a LiDAR device ever appear, this is worth reopening before committing another six weeks to reconstruction.

---

*This plan changes as experiments report back. Every open decision that gets resolved should be written back into the relevant section, with a link to the evaluation report that justified it.*
