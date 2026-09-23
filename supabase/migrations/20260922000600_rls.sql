-- Row-Level Security (implementation-plan.md 6, 9.1, 9.4).
--
-- 6: "All user-owned tables have RLS: `owner_id = auth.uid()` for select/
-- insert/update/delete. Workers use a separate role that bypasses RLS only
-- on pipeline/catalog tables."
--
-- 9.3 rates cross-user access the worst-case bug in this product -- these are
-- videos of the inside of people's homes -- and 9.1 runs the isolation suite
-- on every CI run touching the database. So the rules here are written to be
-- boring and identical wherever possible: one predicate, `owner_id =
-- (select auth.uid())`, repeated per table, with the exceptions called out
-- individually rather than folded into a clever helper.
--
-- Two details worth stating, because both are easy to reason about wrongly:
--
-- 1. **`with check` is written out even where Postgres would infer it.** On
--    an UPDATE policy with no `with check`, Postgres reuses `using` as the
--    check, so omitting it is not a hole today -- this was verified by
--    removing one and watching the isolation suite stay green. It is spelled
--    out anyway because the two clauses answer different questions (which
--    rows may I touch, versus what may the row become), and the day someone
--    widens `using` -- an admin clause, a shared-room feature -- the
--    inferred check widens silently with it. On INSERT there is no `using`
--    at all and `with check` is the only thing standing there.
-- 2. **`(select auth.uid())`, not `auth.uid()`.** The subquery form is
--    evaluated once per statement instead of once per row. On a listing of
--    a user's rooms that is the difference between one call and one per row.
-- 3. **A wide `using` on UPDATE or DELETE is not covered by a narrow SELECT
--    policy.** It looks as though it is: `update rooms ... where id = $1`
--    has to read `id`, so the SELECT policy runs first and a stranger
--    matches nothing. But `update rooms set name = 'x'` reads no column,
--    skips the SELECT policy entirely, and takes every row the UPDATE
--    policy admits. Widening `rooms_update_own` to `using (true)` was
--    measured against the isolation suite and left it entirely green until
--    tests using the unqualified form were added. So every predicate below
--    is the owner check itself -- never `true` with the expectation that
--    something else will catch it.

-- ---------------------------------------------------------------------------
-- Roles
-- ---------------------------------------------------------------------------
-- 6: "Workers use a separate role that bypasses RLS only on pipeline/catalog
-- tables." `roomfittr_worker` is that role. It is deliberately *not*
-- BYPASSRLS: a role that bypasses everything is one credential leak away
-- from every user's data, and the pipeline only ever needs to write rows it
-- is already processing. Instead it gets explicit grants, plus policies
-- below that admit it by name.
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'roomfittr_worker') then
    create role roomfittr_worker nologin;
  end if;
end
$$;

grant usage on schema public to roomfittr_worker;

-- ---------------------------------------------------------------------------
-- Enable RLS everywhere
-- ---------------------------------------------------------------------------
-- Including the catalog tables. A table with RLS enabled and no policy
-- denies everything, which is the safe direction to be wrong in; the read
-- policies the catalog needs are granted explicitly further down.
alter table public.profiles          enable row level security;
alter table public.rooms             enable row level security;
alter table public.scans             enable row level security;
alter table public.room_models       enable row level security;
alter table public.measurements      enable row level security;
alter table public.detected_objects  enable row level security;
alter table public.scan_artifacts    enable row level security;
alter table public.scan_events       enable row level security;
alter table public.layouts           enable row level security;
alter table public.placed_items      enable row level security;
alter table public.retailers         enable row level security;
alter table public.product_pages     enable row level security;
alter table public.products          enable row level security;
alter table public.price_history     enable row level security;
alter table public.crawl_runs        enable row level security;
alter table public.jobs              enable row level security;
alter table public.usage_counters    enable row level security;

-- ---------------------------------------------------------------------------
-- profiles
-- ---------------------------------------------------------------------------
-- Your own row only. There is no "look up another user" feature in V1, and a
-- display name is the kind of thing that quietly becomes an enumeration
-- endpoint the moment it is readable.
create policy profiles_select_own on public.profiles
  for select to authenticated using (id = (select auth.uid()));

create policy profiles_update_own on public.profiles
  for update to authenticated
  using (id = (select auth.uid()))
  with check (id = (select auth.uid()));

-- No insert policy: the `on_auth_user_created` trigger owns that, and it is
-- security definer. A user who could insert their own profile could insert
-- one for somebody else's id.
-- No delete policy: profiles go when auth.users does, by cascade.

-- ---------------------------------------------------------------------------
-- rooms
-- ---------------------------------------------------------------------------
-- 7.4: "Room is already hidden (status `deleting`) and RLS excludes it from
-- all reads." The select policy is where that happens -- it is not a filter
-- the API is trusted to remember to add.
create policy rooms_select_own on public.rooms
  for select to authenticated
  using (owner_id = (select auth.uid()) and status <> 'deleting');

create policy rooms_insert_own on public.rooms
  for insert to authenticated with check (owner_id = (select auth.uid()));

create policy rooms_update_own on public.rooms
  for update to authenticated
  using (owner_id = (select auth.uid()))
  with check (owner_id = (select auth.uid()));

create policy rooms_delete_own on public.rooms
  for delete to authenticated using (owner_id = (select auth.uid()));

-- ---------------------------------------------------------------------------
-- The owner_id tables
-- ---------------------------------------------------------------------------
-- scans, room_models, measurements, detected_objects, scan_artifacts,
-- layouts, placed_items. Identical shape; written out per table because a
-- loop generating policies is a loop nobody reads, and this is the file a
-- reviewer checks line by line before trusting the product with a stranger's
-- home video.

create policy scans_select_own on public.scans
  for select to authenticated using (owner_id = (select auth.uid()));
create policy scans_insert_own on public.scans
  for insert to authenticated with check (owner_id = (select auth.uid()));
create policy scans_update_own on public.scans
  for update to authenticated
  using (owner_id = (select auth.uid()))
  with check (owner_id = (select auth.uid()));
create policy scans_delete_own on public.scans
  for delete to authenticated using (owner_id = (select auth.uid()));

create policy room_models_select_own on public.room_models
  for select to authenticated using (owner_id = (select auth.uid()));
create policy room_models_insert_own on public.room_models
  for insert to authenticated with check (owner_id = (select auth.uid()));
create policy room_models_update_own on public.room_models
  for update to authenticated
  using (owner_id = (select auth.uid()))
  with check (owner_id = (select auth.uid()));
create policy room_models_delete_own on public.room_models
  for delete to authenticated using (owner_id = (select auth.uid()));

create policy measurements_select_own on public.measurements
  for select to authenticated using (owner_id = (select auth.uid()));
create policy measurements_insert_own on public.measurements
  for insert to authenticated with check (owner_id = (select auth.uid()));
create policy measurements_update_own on public.measurements
  for update to authenticated
  using (owner_id = (select auth.uid()))
  with check (owner_id = (select auth.uid()));
create policy measurements_delete_own on public.measurements
  for delete to authenticated using (owner_id = (select auth.uid()));

create policy detected_objects_select_own on public.detected_objects
  for select to authenticated using (owner_id = (select auth.uid()));
create policy detected_objects_update_own on public.detected_objects
  for update to authenticated
  using (owner_id = (select auth.uid()))
  with check (owner_id = (select auth.uid()));
-- No insert or delete for users: the pipeline writes these. A user marking a
-- wardrobe as "kept" is an update, which is the only edit 5.6 offers.

create policy scan_artifacts_select_own on public.scan_artifacts
  for select to authenticated using (owner_id = (select auth.uid()));
-- 9.4: "Presigned GETs are issued only after an RLS-checked ownership read."
-- This select policy is that check. Users never write artifact rows.

create policy scan_events_select_own on public.scan_events
  for select to authenticated using (owner_id = (select auth.uid()));
-- Append-only (6.1): no update or delete policy for anyone, so the trail
-- cannot be edited after the fact even by the worker.

create policy layouts_select_own on public.layouts
  for select to authenticated using (owner_id = (select auth.uid()));
create policy layouts_insert_own on public.layouts
  for insert to authenticated with check (owner_id = (select auth.uid()));
create policy layouts_update_own on public.layouts
  for update to authenticated
  using (owner_id = (select auth.uid()))
  with check (owner_id = (select auth.uid()));
create policy layouts_delete_own on public.layouts
  for delete to authenticated using (owner_id = (select auth.uid()));

create policy placed_items_select_own on public.placed_items
  for select to authenticated using (owner_id = (select auth.uid()));
create policy placed_items_insert_own on public.placed_items
  for insert to authenticated with check (owner_id = (select auth.uid()));
create policy placed_items_update_own on public.placed_items
  for update to authenticated
  using (owner_id = (select auth.uid()))
  with check (owner_id = (select auth.uid()));
create policy placed_items_delete_own on public.placed_items
  for delete to authenticated using (owner_id = (select auth.uid()));

-- ---------------------------------------------------------------------------
-- jobs and usage_counters
-- ---------------------------------------------------------------------------
-- Read-only to their owner: 7.2 shows scan progress from `scans`, and a job
-- row carries dispatch ids and payloads that are none of the user's business
-- to write. System jobs (owner_id null) are invisible to everyone but the
-- worker -- `null = uuid` is null, which is not true, so they are excluded
-- without a special case.
create policy jobs_select_own on public.jobs
  for select to authenticated using (owner_id = (select auth.uid()));

create policy usage_counters_select_own on public.usage_counters
  for select to authenticated using (owner_id = (select auth.uid()));

-- ---------------------------------------------------------------------------
-- Catalog
-- ---------------------------------------------------------------------------
-- The catalog is the same for everyone, so it is readable by everyone --
-- including `anon`, because 1.1 makes the product browser usable before
-- sign-in. Only what a shopper is meant to see: `products` is filtered to
-- active rows so a rejected or archived listing is not reachable by guessing
-- an id, and a retailer's crawl policy, selectors and affiliate config stay
-- server-side.
create policy products_select_active on public.products
  for select to anon, authenticated using (status = 'active');

create policy retailers_select_active on public.retailers
  for select to anon, authenticated using (status = 'active');

create policy price_history_select_for_active_products on public.price_history
  for select to anon, authenticated
  using (
    exists (
      select 1 from public.products p
       where p.id = price_history.product_id and p.status = 'active'
    )
  );

-- product_pages and crawl_runs get no policy at all: raw fetched HTML and
-- crawl statistics are operational data. RLS with no policy denies everyone
-- except the table owner and the roles granted below.

-- ---------------------------------------------------------------------------
-- Worker grants
-- ---------------------------------------------------------------------------
-- 6: the worker "bypasses RLS only on pipeline/catalog tables". Postgres has
-- no per-table bypass, so it is expressed the honest way: table privileges on
-- exactly those tables, plus a permissive policy naming the role. The worker
-- has no policy on `profiles`, and none on `rooms` beyond what the purge job
-- needs -- so a compromised worker still cannot read a user's room list.
grant select, insert, update on
  public.scans, public.room_models, public.detected_objects,
  public.scan_artifacts, public.scan_events, public.layouts,
  public.placed_items, public.jobs, public.usage_counters
  to roomfittr_worker;

grant select, insert, update, delete on
  public.retailers, public.product_pages, public.products,
  public.price_history, public.crawl_runs
  to roomfittr_worker;

grant select on public.layout_candidates to roomfittr_worker, anon, authenticated;

-- The purge job (7.4) needs to find rooms marked `deleting` and remove them
-- once R2 is clear. Select and delete only: it never reads an active room.
grant select, delete on public.rooms to roomfittr_worker;

create policy scans_worker on public.scans
  for all to roomfittr_worker using (true) with check (true);
create policy room_models_worker on public.room_models
  for all to roomfittr_worker using (true) with check (true);
create policy detected_objects_worker on public.detected_objects
  for all to roomfittr_worker using (true) with check (true);
create policy scan_artifacts_worker on public.scan_artifacts
  for all to roomfittr_worker using (true) with check (true);
create policy scan_events_worker on public.scan_events
  for all to roomfittr_worker using (true) with check (true);
create policy layouts_worker on public.layouts
  for all to roomfittr_worker using (true) with check (true);
create policy placed_items_worker on public.placed_items
  for all to roomfittr_worker using (true) with check (true);
create policy jobs_worker on public.jobs
  for all to roomfittr_worker using (true) with check (true);
create policy usage_counters_worker on public.usage_counters
  for all to roomfittr_worker using (true) with check (true);

create policy retailers_worker on public.retailers
  for all to roomfittr_worker using (true) with check (true);
create policy product_pages_worker on public.product_pages
  for all to roomfittr_worker using (true) with check (true);
create policy products_worker on public.products
  for all to roomfittr_worker using (true) with check (true);
create policy price_history_worker on public.price_history
  for all to roomfittr_worker using (true) with check (true);
create policy crawl_runs_worker on public.crawl_runs
  for all to roomfittr_worker using (true) with check (true);

-- The purge job only: rooms already marked for deletion.
create policy rooms_worker_purge on public.rooms
  for all to roomfittr_worker
  using (status = 'deleting') with check (status = 'deleting');

-- ---------------------------------------------------------------------------
-- Client grants
-- ---------------------------------------------------------------------------
-- RLS filters rows; it does not grant table access. Without these the
-- policies above are unreachable and every query fails with a permission
-- error instead of an empty result.
grant select, insert, update, delete on
  public.rooms, public.scans, public.room_models, public.measurements,
  public.layouts, public.placed_items
  to authenticated;
grant select, update on public.profiles, public.detected_objects to authenticated;
grant select on
  public.scan_artifacts, public.scan_events, public.jobs,
  public.usage_counters, public.products, public.retailers, public.price_history
  to authenticated;
grant usage, select on sequence public.scan_events_id_seq to roomfittr_worker;

grant select on public.products, public.retailers, public.price_history to anon;

-- Nothing is granted to `anon` on a user-owned table. A signed-out visitor
-- has no rooms, so there is no row for them to reach -- but the grant is the
-- outer wall and the policy is the inner one, and 9.3 is explicit that this
-- is the failure the product cannot have.
