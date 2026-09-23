-- Jobs and quotas (implementation-plan.md 6.4).

create table public.jobs (
  id uuid primary key default public.uuidv7(),
  type public.job_type not null,
  subject_type text not null,
  subject_id uuid not null,
  -- Nullable for system jobs (6.4): a catalog crawl belongs to nobody.
  owner_id uuid references public.profiles (id) on delete cascade,

  status public.job_status not null default 'queued',
  attempt integer not null default 0,
  max_attempts integer not null default 3,

  payload jsonb not null default '{}'::jsonb,
  result jsonb,
  error_code text,
  error_detail text,

  dispatch_id text,
  locked_at timestamptz,
  -- 7.4's sweeper finds work whose heartbeat has stopped and resumes it from
  -- the last stage checkpoint.
  heartbeat_at timestamptz,
  run_after timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  constraint jobs_attempts_sane check (attempt >= 0 and max_attempts >= 1)
);

create index jobs_queue_idx on public.jobs (status, run_after);
create index jobs_subject_idx on public.jobs (subject_type, subject_id);
create index jobs_type_created_idx on public.jobs (type, created_at);

-- 6.4's idempotency rule: "partial unique (type, subject_id) where status in
-- ('queued','running'). The same scan can't be processed twice
-- concurrently." This is the constraint behind 9.1's "double upload/complete
-- -> one job" test, and it belongs in the database because the two requests
-- may be served by two different instances that cannot see each other.
create unique index jobs_one_active_per_subject
  on public.jobs (type, subject_id)
  where status in ('queued', 'running');

-- ---------------------------------------------------------------------------
-- usage_counters
-- ---------------------------------------------------------------------------
-- 6.4: "owner_id, day, ... PK (owner_id, day). Also a global row for the
-- daily GPU cap."
--
-- The global row needs an owner_id that is not a user, and a nullable column
-- cannot be part of a primary key. The all-zero UUID is used as that sentinel
-- and is given a name here so no one has to guess what it means; it can never
-- collide because uuidv7() always sets a version nibble.
create table public.usage_counters (
  owner_id uuid not null,
  day date not null,
  scans_started integer not null default 0,
  layouts_generated integer not null default 0,
  gpu_seconds bigint not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (owner_id, day),

  constraint usage_counters_not_negative check (
    scans_started >= 0 and layouts_generated >= 0 and gpu_seconds >= 0
  )
);

comment on column public.usage_counters.owner_id is
  'A profile id, or 00000000-0000-0000-0000-000000000000 for the global row '
  'that carries the daily GPU cap (implementation-plan.md 6.4, 1.3).';

-- Deliberately not a foreign key to profiles: the global row has no profile,
-- and a per-user row must survive long enough for the cap to be correct on
-- the day a user deletes their account (1.3 enforces the cap "before
-- dispatch, not after the bill").

create trigger jobs_touch before update on public.jobs
  for each row execute function public.touch_updated_at();
create trigger usage_counters_touch before update on public.usage_counters
  for each row execute function public.touch_updated_at();
