# Infrastructure runbook

`implementation-plan.md` §8 Phase 0 task 6: *"Set up Modal account/workspace, R2 buckets
(dev/prod), Supabase projects (dev/prod), Sentry."*

Account creation needs a human with a card and an email, so **this file is the part that
could be written ahead of time**: every step, in order, with the exact names to use and the
exact environment variables each step produces. Work through it once and
[`.env.example`](../.env.example) fills itself in.

Nothing in the codebase reads these variables yet — Phase 1 is scripts and Modal functions
with no web app (§8). The point of doing it now is that Phase 1 cannot start without R2 and
Modal, and an hour of signups should not be discovered on the day the pipeline is ready.

---

## Conventions used throughout

| | dev | prod |
| --- | --- | --- |
| Supabase project | `roomfittr-dev` | `roomfittr-prod` |
| R2 bucket | `roomfittr-dev` | `roomfittr-prod` |
| Modal environment | `dev` | `main` |
| Sentry environment tag | `development` | `production` |

**Two hard rules.**

1. **Prod credentials never touch a laptop.** They exist in Vercel's and Modal's secret
   stores and nowhere else. `.env.local` is dev-only. A prod service-role key on a dev
   machine is a data breach waiting for a stolen laptop.
2. **Every bucket is private.** Room videos are the inside of someone's home. Browser access
   is via presigned URLs with short expiry, never a public bucket.

---

## 1 · Supabase (Postgres, Auth, Realtime)

**Cost:** free tier to start; ~$25/mo Pro when the free tier's limits or project pausing
bite (§1.3). Free projects pause after inactivity — fine for dev, not for prod once there
are real users.

1. Create two projects at [supabase.com/dashboard](https://supabase.com/dashboard):
   `roomfittr-dev` and `roomfittr-prod`. Same region for both; pick the one nearest you,
   since latency to Vercel matters more than anything else at this scale.
2. Save the generated database password immediately — it is shown once.
3. From **Project Settings → API**, copy into `.env.local` (dev project only):
   - Project URL → `NEXT_PUBLIC_SUPABASE_URL`
   - `anon` / publishable key → `NEXT_PUBLIC_SUPABASE_ANON_KEY`
   - `service_role` key → `SUPABASE_SERVICE_ROLE_KEY`
4. From **Project Settings → Database**, copy the connection string → `SUPABASE_DB_URL`.
   Use port **5432** (direct) for migrations and **6543** (pooled) for serverless runtime.
5. Enable **pgvector** (§1.1, for SigLIP 2 style embeddings) — it is an extension toggle in
   **Database → Extensions**, or the first migration can `create extension if not exists vector;`.
6. Enable auth providers (**Authentication → Providers**): **Google OAuth** and **anonymous
   sign-in**. Anonymous sign-in is what makes "usable without an account" work while RLS
   still applies (§1.1), so it is not optional.
7. Install the CLI and link the dev project. Migrations live in `supabase/migrations/`
   (directory already exists, empty until the Phase 5/6 data model lands):

   ```sh
   npm i -g supabase
   supabase link --project-ref <dev-ref>
   supabase db push
   ```

**Produces:** `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY`,
`SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_DB_URL`.

**Do not skip:** RLS is off by default on new tables. §6 assumes it is on everywhere. Turn
it on in the same migration that creates each table, never as a follow-up.

---

## 2 · Cloudflare R2 (object storage)

**Cost:** 10 GB free, ~$0.015/GB-month after, **zero egress** — which is the whole reason
for choosing it over S3 (§1.1), since GLBs get downloaded constantly.

1. In the Cloudflare dashboard → **R2**, create buckets `roomfittr-dev` and
   `roomfittr-prod`. Leave both **private**.
2. **R2 → Manage API Tokens** → create a token scoped to **Object Read & Write** on the dev
   bucket only. Create a separate token for prod later, scoped to prod only. One token for
   both buckets means a leaked dev token loses prod data.
3. Copy into `.env.local`: account id → `R2_ACCOUNT_ID`, access key → `R2_ACCESS_KEY_ID`,
   secret → `R2_SECRET_ACCESS_KEY`. The S3 endpoint is
   `https://<account-id>.r2.cloudflarestorage.com` → `R2_ENDPOINT`.
4. **CORS** on each bucket, so browser-to-bucket multipart upload works (§1.1, Uppy S3
   multipart). Allow only the origins that need it:

   ```json
   [
     {
       "AllowedOrigins": ["http://localhost:3000", "https://<your-vercel-domain>"],
       "AllowedMethods": ["GET", "PUT", "POST", "HEAD"],
       "AllowedHeaders": ["*"],
       "ExposeHeaders": ["ETag"],
       "MaxAgeSeconds": 3600
     }
   ]
   ```

   `ExposeHeaders: ["ETag"]` is the one people miss — S3 multipart cannot complete without
   reading each part's ETag, and the upload fails at the last step with no useful error.
5. **Lifecycle rules.** Intermediate pipeline artefacts (frames, dense point clouds) are
   large and only needed while debugging a scan. Expire `**/frames/` and `**/points/` after
   30 days; keep the source video and the final GLB. This is the difference between a $3/mo
   bill and a $30/mo one.

**Key layout** — fixed by §2.4, and worth getting right from the first upload because
lifecycle rules and debugging both depend on it:

```
rooms/{room_id}/scans/{scan_id}/{pipeline_version}/{stage}/…
```

**Produces:** `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`,
`R2_ENDPOINT`.

---

## 3 · Modal (GPU workers, cron)

**Cost:** per-second billing, scales to zero; monthly free credits cover early Phase 1
(§1.3). Budget ~$0.20–0.60 per scan.

1. Sign up at [modal.com](https://modal.com) and create a workspace.
2. `pip install modal && modal token new` — this writes `~/.modal.toml`. For CI, take the
   token id/secret into `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET`.
3. Create the two environments:

   ```sh
   modal environment create dev
   # `main` exists by default and is used for prod
   ```

4. Create the secrets the worker image needs. Modal secrets are the only place prod
   credentials belong:

   ```sh
   modal secret create roomfittr-r2 \
     R2_ACCOUNT_ID=… R2_ACCESS_KEY_ID=… R2_SECRET_ACCESS_KEY=… R2_BUCKET=… R2_ENDPOINT=…
   modal secret create roomfittr-supabase \
     SUPABASE_DB_URL=… SUPABASE_SERVICE_ROLE_KEY=…
   modal secret create roomfittr-hf HF_TOKEN=…
   modal secret create roomfittr-sentry SENTRY_DSN=…
   ```

5. **Confirm GPU access** for the classes §3.9 needs (L40S and A100-80). New workspaces are
   sometimes limited until a card is on file, and discovering that during the E7 profiling
   run wastes a day.
6. `HF_TOKEN` must belong to an account whose access requests for
   `facebook/VGGT-1B-Commercial` and the SAM 3 weights have been **approved** — both are
   gated (see [`model-licenses.md`](./model-licenses.md)). Request access now; approval is
   automatic but not instant, and the failure otherwise happens at image-build time.

**Produces:** `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET`, `MODAL_ENVIRONMENT`.
Generate `MODAL_DISPATCH_SECRET` yourself: `openssl rand -hex 32`.

---

## 4 · Sentry (errors, web + workers)

**Cost:** free tier is enough at personal-beta scale (§1.1).

1. Create an org and **two projects**: one **Next.js** (`roomfittr-web`) and one **Python**
   (`roomfittr-workers`). Separate projects, because a GPU worker traceback and a browser
   error need different alert rules.
2. Copy the web DSN → `NEXT_PUBLIC_SENTRY_DSN`, the worker DSN → `SENTRY_DSN`.
3. Set `SENTRY_ENVIRONMENT` per deployment (`development` / `production`) so dev noise does
   not page you.
4. For source maps at build time, create an org auth token → `SENTRY_AUTH_TOKEN`, plus
   `SENTRY_ORG` and `SENTRY_PROJECT`. Not needed to run locally.
5. **Scrub PII.** Scans are video of people's homes; a captured request body or an R2 URL in
   a breadcrumb is a privacy incident. Enable server-side data scrubbing and make sure no
   presigned URL is attached to an event.

**Produces:** `NEXT_PUBLIC_SENTRY_DSN`, `SENTRY_DSN`, `SENTRY_ENVIRONMENT`,
`SENTRY_AUTH_TOKEN`, `SENTRY_ORG`, `SENTRY_PROJECT`.

---

## 5 · Vercel (web + API)

Not in the Phase 0 task list — Phase 1 has no web app (§8) — but the account matters for one
reason recorded in §1.3: **the Hobby plan is non-commercial.** If RoomFittR ever monetises,
the plan must move to Pro. Same class of problem as a non-commercial model checkpoint, and
it is in [`model-licenses.md`](./model-licenses.md) for that reason.

When the time comes: import the repo, set the root directory to `apps/web`, and add every
non-`NEXT_PUBLIC_` variable as an encrypted environment variable per environment.

---

## 6 · Verifying it works

Run these after filling in `.env.local`. Each one fails loudly if a credential is wrong,
which is much better than finding out mid-pipeline.

```sh
# Supabase: should print the server version
psql "$SUPABASE_DB_URL" -c 'select version();'

# R2: should list the (empty) bucket without error
aws s3 ls "s3://$R2_BUCKET" --endpoint-url "$R2_ENDPOINT"

# Modal: should print the workspace and environments
modal profile current
modal environment list

# Hugging Face gated access: should download, not 403
huggingface-cli download facebook/VGGT-1B-Commercial --revision main --dry-run
```

## 7 · Cost guardrails

§1.3 requires these from day one, not after the first surprise bill. They are environment
variables (`.env.example`) rather than constants so they can be tightened without a deploy:

- `MAX_SCANS_PER_USER_PER_DAY` — per-user daily quota.
- `MAX_GPU_MINUTES_PER_DAY` — global cap, checked **before** dispatch. A runaway retry loop
  is the realistic failure mode, and a cap checked after dispatch does not stop it.
- `SCAN_JOB_TIMEOUT_S` — hard per-job timeout, enforced by Modal itself so a hung job cannot
  outlive it.

Also set a **billing alert** in Cloudflare, Modal and Supabase. They are free and they are
the only thing that catches a mistake nobody predicted.

---

## Status

| Service | Provisioned | Credentials in `.env.local` | In Modal/Vercel secrets |
| --- | --- | --- | --- |
| Supabase dev | ☐ | ☐ | n/a |
| Supabase prod | ☐ | never | ☐ |
| R2 dev | ☐ | ☐ | ☐ |
| R2 prod | ☐ | never | ☐ |
| Modal workspace | ☐ | ☐ | ☐ |
| Sentry web | ☐ | ☐ | ☐ |
| Sentry workers | ☐ | ☐ | ☐ |
| HF gated access approved | ☑ 2026-09-19 | ☑ | ☐ Phase 1 |

Tick these off as you go; `phase-0-progress.md` task 6 closes when the table is full.
