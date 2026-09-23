# Phase 5 progress

Live status for `implementation-plan.md` §8 Phase 5 (Productionize Backend &
Pipeline). Updated as work lands. **If work stopped partway, the "Stopped at"
line below is the resume point.**

**Stopped at:** _**The data layer is done and its access control is tested,
2026-09-22.** Every table in §6 exists as a migration, RLS is on everywhere,
and the isolation suite §9.1 gates runs in CI against a real Supabase
Postgres. What remains is the API and the Modal app — neither of which is
blocked, both of which are large._

---

## Task status

| Task (from the plan) | Status | Notes |
|---|---|---|
| All remaining migrations | **done** | §6.1–6.4 in six files under `supabase/migrations/`. Every table, index and partial-unique constraint the plan names, plus the `layout_candidates` materialized view §4.11 and the layout engine read against. |
| **RLS policies** | **done** | `20260922000600_rls.sql`. Every user-owned table, `owner_id = (select auth.uid())`. |
| Worker role grants | **done** | `roomfittr_worker`, deliberately **not** `BYPASSRLS` — see below. |
| **RLS isolation tests** | **done** | 37 tests, in CI as its own job. Proven to catch a real hole, not just to pass. |
| Supabase Auth: Google, anonymous, linking | **partial** | The database half is done and tested: the profile trigger, the `is_anonymous` mirror, and a test that anonymous data survives linking. Configuring the Google provider is a console task — see §"What only you can do". |
| Modal app (`process_scan`, crons, dispatch) | **not started** | Needs Phase 2's final pipeline to wrap, and a GPU to run it on. |
| Cold-start optimization, GPU type finalization | **not started** | D4. Blocked on E7 profiling, which needs a GPU. |
| Quotas and global GPU cap | **schema only** | `usage_counters` exists with the global-row sentinel §6.4 asks for. The enforcement lives in the dispatch path, which does not exist yet. |
| R2 lifecycle rules | **not started** | Console task, plus `scan_artifacts.expires_at` which is already there to agree with it. |
| Sentry + structured logs + `scan_events` | **partial** | Sentry is wired into `apps/web` (earlier commit); `scan_events` exists and is append-only. The structured logging in the worker is not written. |
| API route handlers for §7.2 | **not started** | The largest remaining piece. `apps/web` is still only the marketing page. |

---

## Definition of done (from the plan)

| Clause | State |
|---|---|
| A scan submitted via API goes all the way to a ready layout | ❌ no API yet, and no pipeline to run |
| All failure-injection tests pass | ❌ needs the Modal app |
| **RLS suite green** | ✅ **37 tests, in CI, against Supabase Postgres 17.6** |
| Deletion verified | ⚠️ the database half is tested (`deleting` hides a room from every read, the FK ordering is exercised). The R2 half needs the purge job. |

---

## What was found while building

Recorded because each was invisible to reading the code, which is the pattern
this project keeps hitting.

1. **The isolation suite was wrong before it was right — twice, and the
   second one mattered.**
   - Removing a `with check` from `rooms_update_own` left the suite green.
     That turned out to be correct Postgres behaviour — an UPDATE policy with
     no `with check` reuses `using` — so the *comment* claiming a hole was
     the thing at fault, and it now says what is true.
   - Widening `rooms_update_own` all the way to `using (true)` **also** left
     the suite green. `update rooms … where id = $1` has to read `id`, so the
     SELECT policy runs first and a stranger matches nothing however wide the
     UPDATE policy is. `update rooms set name = 'x'` reads no column, skips
     the SELECT policy entirely, and takes every row the UPDATE policy
     admits. The suite was testing the select policy and reporting on the
     update one. The unqualified-form tests that now exist fail against that
     mutation and pass against the real policy.

   The general lesson, written at the top of the migration: a wide `using` on
   UPDATE or DELETE is *not* covered by a narrow SELECT policy, and an
   attacker does not write the qualified form.

2. **Postgres has no `min(uuid)` before 18.** The `layout_candidates` view
   picked one product per duplicate group with `min(d.id)` and failed to
   create. `order by d.id limit 1` is the fix and is also the better rule:
   ids are UUIDv7, so the lowest is the one seen first, and the
   representative stays stable as the group grows instead of changing
   whenever a retailer with a smaller id turns up.

3. **Counting assertions need a clean database.** Two worker tests failed on
   rows left by earlier tests — and the failure direction was luck. A test
   asserting "the worker can see no rooms" passes or fails depending on what
   ran before it, which makes it worthless in both directions. Every test now
   starts from an empty database, and the truncation order (users before
   catalog) is the same ordering the purge job has to get right.

## Deliberate decisions, all recorded at the code

- **`roomfittr_worker` is not `BYPASSRLS`.** §6 says the worker "bypasses RLS
  only on pipeline/catalog tables" and Postgres has no per-table bypass, so
  it is expressed the honest way: table grants on exactly those tables plus
  policies naming the role. It cannot read `profiles` at all, and it sees a
  room only once that room is marked `deleting`. A leaked worker credential
  is therefore not a directory of everyone's homes, which is what §9.3 calls
  the worst-case bug. There is a test for each of those three claims.
- **`usage_counters` has no FK to `profiles`.** §6.4 wants a global row for
  the daily GPU cap, a nullable column cannot be part of a primary key, and
  the per-user rows must outlive an account deletion long enough for the cap
  to be right on that day (§1.3 enforces it "before dispatch, not after the
  bill"). The all-zero UUID is the sentinel and is documented on the column.
- **`products.image_embedding` is `vector(768)`.** §6.3 marks the dimension
  "[VERIFY dim for chosen model]"; §1.2 chose SigLIP 2, whose base
  checkpoints embed at 768. A different checkpoint means a migration and an
  index rebuild, so the number is stated rather than inferred.
- **Plausibility bounds on product dimensions.** §9.3 rates wrong catalog
  dimensions R7, "High" likelihood and "Critical" impact. A check constraint
  is not a substitute for the parser, but a zero-width sofa should not be
  able to reach the solver at all.

---

## What only you can do

1. **Supabase Auth configuration.** The Google provider, the redirect URLs
   and anonymous sign-in are console settings on the dev and prod projects.
   The database side is done and tested — including that anonymous data
   survives linking, which is the part that could have been wrong.
2. **Apply the migrations to the dev project.** They have only ever run
   against a local container. Nothing here has touched your Supabase project,
   deliberately: a migration that creates seventeen tables is not something
   to run against someone's database without being asked.
3. **R2 lifecycle rules** (multipart abort, debug artifact expiry). A console
   task that has to agree with `scan_artifacts.expires_at`.
4. **A GPU**, for everything the Modal app wraps. Unchanged from Phase 1.

## What the API work should know

- **RLS is the access control, not a filter to remember.** Use `supabase-js`
  with the user's JWT (§1.1 line 142) and let the policies do it. The one
  thing the API must still do is 9.4's rule that a presigned GET is issued
  only after an RLS-checked ownership read — the `scan_artifacts` select
  policy is that check.
- **Anything that writes as the worker must use `roomfittr_worker`**, not the
  service role. The service key bypasses RLS entirely; that is what makes it
  the wrong credential for a route handler, and the reason it is the one key
  in `.env.example` with a warning next to it.
- **`layouts.total_cents` and `updated_at` maintain themselves.** Do not set
  them from the API; the triggers will win and the two answers would differ.
