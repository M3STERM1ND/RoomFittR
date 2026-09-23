-- Extensions, shared types and the helpers every later migration leans on
-- (implementation-plan.md 6).
--
-- Kept separate from the tables because these are the things that must exist
-- before anything else can be created, and because a reviewer reading the
-- schema should not have to wade through trigger boilerplate to find it.

-- 1.1's stack: "Supabase Postgres (+ pgvector)". `vector` is needed by
-- products.image_embedding (6.3); pgcrypto supplies the randomness uuidv7()
-- is built from.
create extension if not exists "pgcrypto" with schema extensions;
create extension if not exists "vector" with schema extensions;

-- ---------------------------------------------------------------------------
-- UUIDv7
-- ---------------------------------------------------------------------------
-- 6: "IDs are UUIDv7 (time-ordered) unless noted."
--
-- Time-ordered matters here for a specific reason: every hot listing in this
-- schema is `(owner_id, created_at desc)` or `(room_id, created_at desc)`,
-- and random v4 keys scatter inserts across the whole index. v7 keys are
-- generated in ascending order, so a room's scans land next to each other on
-- disk and the primary key itself is a usable time ordering.
--
-- Written out rather than taken from an extension because `uuidv7()` is
-- built in from Postgres 18 and Supabase is not there yet; when it is, this
-- function can be dropped and the defaults keep working unchanged.
create or replace function public.uuidv7()
returns uuid
language plpgsql
parallel safe
as $$
declare
  unix_ms bigint;
  bytes bytea;
begin
  unix_ms := (extract(epoch from clock_timestamp()) * 1000)::bigint;

  -- 16 random bytes, then overwrite the first six with the timestamp and set
  -- the version and variant bits in place. Taking randomness first and
  -- stamping over it is what keeps the remaining 74 bits uniform.
  bytes := extensions.gen_random_bytes(16);
  bytes := set_byte(bytes, 0, ((unix_ms >> 40) & 255)::int);
  bytes := set_byte(bytes, 1, ((unix_ms >> 32) & 255)::int);
  bytes := set_byte(bytes, 2, ((unix_ms >> 24) & 255)::int);
  bytes := set_byte(bytes, 3, ((unix_ms >> 16) & 255)::int);
  bytes := set_byte(bytes, 4, ((unix_ms >> 8) & 255)::int);
  bytes := set_byte(bytes, 5, (unix_ms & 255)::int);

  -- Version 7 in the high nibble of byte 6, RFC 9562 variant in byte 8.
  bytes := set_byte(bytes, 6, ((get_byte(bytes, 6) & 15) | 112));
  bytes := set_byte(bytes, 8, ((get_byte(bytes, 8) & 63) | 128));

  return encode(bytes, 'hex')::uuid;
end;
$$;

comment on function public.uuidv7() is
  'RFC 9562 UUIDv7. Time-ordered primary keys (implementation-plan.md 6).';

-- ---------------------------------------------------------------------------
-- updated_at
-- ---------------------------------------------------------------------------
-- 6: "All tables have created_at, updated_at." A trigger rather than an
-- application convention, because the pipeline writes these rows from Python,
-- the API writes them from TypeScript, and an `updated_at` that only some
-- writers maintain is worse than none -- the sweeper in 7.4 uses it to decide
-- what is stale.
create or replace function public.touch_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

-- ---------------------------------------------------------------------------
-- Enumerated domains
-- ---------------------------------------------------------------------------
-- Every closed vocabulary in 6 and 7.3, as a real type. The alternative is a
-- text column with a check constraint; the type is better here because these
-- values cross a language boundary -- Python writes `status`, TypeScript
-- reads it -- and a typo becomes an error at write time instead of a row
-- nobody's code matches.

create type public.room_type as enum ('living', 'bedroom', 'office', 'dining', 'other');
create type public.room_status as enum ('active', 'deleting');

-- 7.3's scan lifecycle.
create type public.scan_status as enum (
  'awaiting_upload', 'uploaded', 'queued', 'processing',
  'needs_calibration', 'ready', 'failed', 'canceled'
);

-- 7.4's stage taxonomy, matching workers/pipeline's `Stage`.
create type public.scan_stage as enum (
  'ingest', 'frames', 'reconstruct', 'segment', 'align',
  'geometry', 'scale', 'shell', 'roommodel'
);

create type public.scale_confidence as enum ('high', 'medium', 'low');
create type public.unit_preference as enum ('imperial', 'metric');
create type public.measurement_kind as enum (
  'wall_length', 'ceiling_height', 'door_width', 'door_height'
);
create type public.label_group as enum ('removable', 'fixed');
create type public.object_disposition as enum ('removed', 'kept');
create type public.artifact_kind as enum (
  'keyframes', 'recon', 'masks', 'points', 'shell',
  'textures', 'objects', 'room_model', 'thumb'
);

create type public.layout_source as enum ('ai', 'ai_edited', 'manual');
create type public.layout_status as enum ('generating', 'ready', 'failed');

create type public.retailer_status as enum ('active', 'paused', 'disabled');
create type public.availability as enum (
  'in_stock', 'out_of_stock', 'preorder', 'unknown'
);
-- 6.3 writes this as `rejected_*`: the reason a product was rejected is part
-- of the state, because 9.3's R7 mitigation watches the reject rate per
-- reason and a single `rejected` value would make that dashboard useless.
create type public.product_status as enum (
  'active', 'pending_review', 'unavailable', 'archived',
  'rejected_dimensions', 'rejected_category', 'rejected_duplicate',
  'rejected_policy'
);
create type public.dimension_source as enum ('jsonld', 'parser', 'llm_grounded');

create type public.crawl_kind as enum ('discover', 'refresh', 'linkcheck');
create type public.crawl_status as enum ('running', 'succeeded', 'failed');

create type public.job_type as enum (
  'process_scan', 'rescale_scan', 'generate_layout', 'purge_room', 'purge_user',
  'catalog_discover', 'catalog_refresh', 'catalog_tag', 'product_assets'
);
create type public.job_status as enum (
  'queued', 'running', 'succeeded', 'failed', 'canceled'
);
