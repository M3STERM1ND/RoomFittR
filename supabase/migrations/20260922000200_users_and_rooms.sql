-- Users, rooms and everything a scan produces (implementation-plan.md 6.1).
--
-- `owner_id` appears on every table here, including ones reachable only
-- through a parent. 6.1 calls it "denormalized for RLS" on `scans` and the
-- same reasoning carries down the tree: an RLS policy that has to join back
-- to `rooms` to decide whether you may read a `scan_event` runs that join on
-- every row of every query, and it is one missing index away from a table
-- scan on the hottest path in the product. The column is redundant; the
-- check it enables is not.

-- ---------------------------------------------------------------------------
-- profiles
-- ---------------------------------------------------------------------------
create table public.profiles (
  id uuid primary key references auth.users (id) on delete cascade,
  display_name text,
  -- Mirrors auth.users.is_anonymous. Duplicated because 7.4's retention
  -- sweeper needs it together with last_active_at, and the auth schema is
  -- not ours to index.
  is_anonymous boolean not null default false,
  default_currency text not null default 'USD',
  unit_preference public.unit_preference not null default 'imperial',
  scan_quota_override integer,
  last_active_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  constraint profiles_currency_is_iso4217 check (default_currency ~ '^[A-Z]{3}$'),
  constraint profiles_quota_override_sane check (
    scan_quota_override is null or scan_quota_override between 0 and 1000
  )
);

-- 6.1: "Index: last_active_at (anonymous-retention sweeper)". Partial,
-- because the sweeper only ever asks about anonymous profiles and there is no
-- reason to carry the signed-in majority in that index.
create index profiles_anonymous_last_active_idx
  on public.profiles (last_active_at)
  where is_anonymous;

-- A profile per auth user, created by the database rather than the API.
-- 1.1 gives anonymous users "a real user ID, so RLS works identically" --
-- which is only true if the profile exists, and an anonymous user never goes
-- through a sign-up flow where the API might have created one.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.profiles (id, is_anonymous)
  values (new.id, coalesce(new.is_anonymous, false))
  on conflict (id) do nothing;
  return new;
end;
$$;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- Identity linking (1.1: "linking Google later keeps all their rooms") flips
-- is_anonymous on auth.users. Mirroring it keeps the sweeper from deleting
-- the rooms of someone who has just signed up.
create or replace function public.sync_profile_anonymity()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  update public.profiles
     set is_anonymous = coalesce(new.is_anonymous, false),
         updated_at = now()
   where id = new.id
     and is_anonymous is distinct from coalesce(new.is_anonymous, false);
  return new;
end;
$$;

create trigger on_auth_user_anonymity_changed
  after update of is_anonymous on auth.users
  for each row execute function public.sync_profile_anonymity();

-- ---------------------------------------------------------------------------
-- rooms
-- ---------------------------------------------------------------------------
create table public.rooms (
  id uuid primary key default public.uuidv7(),
  owner_id uuid not null references public.profiles (id) on delete cascade,
  name text not null default 'My room',
  -- Nullable until inferred (6.1): the room type comes from the LLM or the
  -- user, and neither has spoken when the row is created.
  room_type public.room_type,
  -- Set after the rows exist, so the FKs are added at the end of this file.
  current_scan_id uuid,
  current_layout_id uuid,
  thumbnail_key text,
  status public.room_status not null default 'active',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  constraint rooms_name_not_blank check (length(btrim(name)) > 0),
  constraint rooms_name_bounded check (length(name) <= 120)
);

create index rooms_owner_created_idx on public.rooms (owner_id, created_at desc);

-- 6.1: "status partial where deleting". The purge worker's work queue, and
-- it is empty almost always -- exactly what a partial index is for.
create index rooms_deleting_idx on public.rooms (status) where status = 'deleting';

-- ---------------------------------------------------------------------------
-- scans
-- ---------------------------------------------------------------------------
create table public.scans (
  id uuid primary key default public.uuidv7(),
  room_id uuid not null references public.rooms (id) on delete cascade,
  owner_id uuid not null references public.profiles (id) on delete cascade,
  video_key text,
  video_meta jsonb not null default '{}'::jsonb,
  motion_key text,

  status public.scan_status not null default 'awaiting_upload',
  stage public.scan_stage,
  progress_pct smallint not null default 0,
  error_code text,
  -- 6.1 calls this the "user-safe message". 7.4 requires the envelope to
  -- leak no internals, so whatever lands here is shown to the user as-is.
  error_detail text,
  retryable boolean not null default false,

  pipeline_version text,
  upload_id text,
  upload_expires_at timestamptz,
  metrics jsonb not null default '{}'::jsonb,

  started_at timestamptz,
  completed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  constraint scans_progress_is_a_percentage check (progress_pct between 0 and 100),
  -- A terminal scan has finished; a running one has not. Cheap to state and
  -- it catches a worker that forgot to stamp completed_at, which would
  -- otherwise show as a scan that runs forever in the UI.
  constraint scans_terminal_has_completed_at check (
    (status in ('ready', 'failed', 'canceled')) = (completed_at is not null)
  )
);

create index scans_room_created_idx on public.scans (room_id, created_at desc);

-- 6.1: "(status) partial where status in active states". The sweeper (7.4)
-- scans this to find work that has stopped heartbeating.
create index scans_active_idx on public.scans (status)
  where status in ('uploaded', 'queued', 'processing');

alter table public.rooms
  add constraint rooms_current_scan_fk
  foreign key (current_scan_id) references public.scans (id) on delete set null;

-- ---------------------------------------------------------------------------
-- room_models
-- ---------------------------------------------------------------------------
create table public.room_models (
  id uuid primary key default public.uuidv7(),
  scan_id uuid not null references public.scans (id) on delete cascade,
  owner_id uuid not null references public.profiles (id) on delete cascade,
  version integer not null default 1,
  -- The RoomModel document itself (2.4). Stored whole rather than shredded
  -- into columns: the schema is the contract, it is validated on the way in
  -- by both languages, and a normalised copy would be a second definition
  -- free to disagree with it.
  geometry jsonb not null,
  schema_version integer not null,

  scale_factor double precision,
  scale_confidence public.scale_confidence,
  scale_sources jsonb not null default '[]'::jsonb,
  user_calibrated boolean not null default false,

  -- Denormalized for listing (6.1), so the rooms list does not parse a
  -- geometry document per row to show an area.
  floor_area_mm2 bigint,
  ceiling_height_mm integer,

  shell_key text,
  objects_key text,
  is_current boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  constraint room_models_version_positive check (version >= 1),
  constraint room_models_ceiling_plausible check (
    ceiling_height_mm is null or ceiling_height_mm between 1500 and 6000
  ),
  constraint room_models_scale_positive check (
    scale_factor is null or scale_factor > 0
  )
);

create unique index room_models_scan_version_key
  on public.room_models (scan_id, version);

-- 6.1: "Partial unique: (scan_id) where is_current". Recalibration writes a
-- new version and clears the old flag; this is what makes "the current
-- geometry" a fact about the database rather than a convention the writers
-- are trusted to keep.
create unique index room_models_one_current_per_scan
  on public.room_models (scan_id)
  where is_current;

-- ---------------------------------------------------------------------------
-- measurements
-- ---------------------------------------------------------------------------
create table public.measurements (
  id uuid primary key default public.uuidv7(),
  room_model_id uuid not null references public.room_models (id) on delete cascade,
  owner_id uuid not null references public.profiles (id) on delete cascade,
  kind public.measurement_kind not null,
  -- A wall or opening id from the RoomModel: W2, O1 (2.4's id grammar).
  target_ref text,
  value_mm integer not null,
  applied boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  constraint measurements_value_positive check (value_mm > 0),
  constraint measurements_target_ref_shape check (
    target_ref is null or target_ref ~ '^[WOFB][0-9]+$'
  )
);

create index measurements_room_model_idx on public.measurements (room_model_id);

-- ---------------------------------------------------------------------------
-- detected_objects
-- ---------------------------------------------------------------------------
create table public.detected_objects (
  id uuid primary key default public.uuidv7(),
  scan_id uuid not null references public.scans (id) on delete cascade,
  owner_id uuid not null references public.profiles (id) on delete cascade,
  label text not null,
  label_group public.label_group not null,
  score real,
  center_x_mm integer not null,
  center_z_mm integer not null,
  width_mm integer not null,
  depth_mm integer not null,
  height_mm integer not null,
  yaw_deg double precision not null default 0,
  disposition public.object_disposition not null default 'removed',
  crop_key text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  constraint detected_objects_score_is_a_probability check (
    score is null or score between 0 and 1
  ),
  constraint detected_objects_extent_positive check (
    width_mm > 0 and depth_mm > 0 and height_mm > 0
  )
);

create index detected_objects_scan_idx on public.detected_objects (scan_id);

-- ---------------------------------------------------------------------------
-- scan_artifacts
-- ---------------------------------------------------------------------------
create table public.scan_artifacts (
  id uuid primary key default public.uuidv7(),
  scan_id uuid not null references public.scans (id) on delete cascade,
  owner_id uuid not null references public.profiles (id) on delete cascade,
  stage public.scan_stage,
  kind public.artifact_kind not null,
  object_key text not null,
  bytes bigint,
  sha256 text,
  pipeline_version text,
  -- Nullable, for debug artifacts (6.1). The R2 lifecycle rules in Phase 5
  -- and this column have to agree, so the expiry is recorded rather than
  -- left implicit in a bucket rule nobody can see from the database.
  expires_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  constraint scan_artifacts_sha256_shape check (
    sha256 is null or sha256 ~ '^[0-9a-f]{64}$'
  )
);

create index scan_artifacts_scan_kind_idx on public.scan_artifacts (scan_id, kind);

-- The purge job (7.4) deletes R2 objects before the DB rows, so it needs
-- every key that is due to expire regardless of which scan it belongs to.
create index scan_artifacts_expiring_idx on public.scan_artifacts (expires_at)
  where expires_at is not null;

-- ---------------------------------------------------------------------------
-- scan_events
-- ---------------------------------------------------------------------------
-- 6.1: "append-only audit/debug trail". bigserial rather than uuidv7 because
-- 6.1 says so, and because this is the one table where the id is only ever a
-- sequence number read in order.
create table public.scan_events (
  id bigserial primary key,
  scan_id uuid not null references public.scans (id) on delete cascade,
  owner_id uuid not null references public.profiles (id) on delete cascade,
  at timestamptz not null default now(),
  from_status public.scan_status,
  to_status public.scan_status,
  stage public.scan_stage,
  message text,
  data jsonb not null default '{}'::jsonb
);

create index scan_events_scan_at_idx on public.scan_events (scan_id, at);

-- ---------------------------------------------------------------------------
-- updated_at triggers
-- ---------------------------------------------------------------------------
create trigger profiles_touch before update on public.profiles
  for each row execute function public.touch_updated_at();
create trigger rooms_touch before update on public.rooms
  for each row execute function public.touch_updated_at();
create trigger scans_touch before update on public.scans
  for each row execute function public.touch_updated_at();
create trigger room_models_touch before update on public.room_models
  for each row execute function public.touch_updated_at();
create trigger measurements_touch before update on public.measurements
  for each row execute function public.touch_updated_at();
create trigger detected_objects_touch before update on public.detected_objects
  for each row execute function public.touch_updated_at();
create trigger scan_artifacts_touch before update on public.scan_artifacts
  for each row execute function public.touch_updated_at();
