-- Layouts and the items in them (implementation-plan.md 6.2).
--
-- 6.2: "the Masterplan's Budget entity is folded in here: each layout carries
-- its own budget. A room may have several layouts, e.g. 'cheap' vs
-- 'splurge'." So the budget is a column on the layout rather than a table --
-- there is no budget that exists apart from an arrangement it paid for.

create table public.layouts (
  id uuid primary key default public.uuidv7(),
  room_id uuid not null references public.rooms (id) on delete cascade,
  owner_id uuid not null references public.profiles (id) on delete cascade,
  -- 6.2: "layout is bound to a geometry version". Not to the scan and not to
  -- the room: recalibration writes a new room_models row, and a layout
  -- solved against the old measurements is no longer known to fit. This FK
  -- is what lets the UI say so instead of showing a plan that is quietly
  -- wrong by a few centimetres.
  room_model_id uuid references public.room_models (id) on delete set null,

  name text not null default 'Layout',
  source public.layout_source not null default 'ai',
  status public.layout_status not null default 'generating',

  budget_cents bigint not null,
  currency text not null default 'USD',
  style text,
  room_type public.room_type,

  -- Cached, recomputed on every item change (6.2). A trigger below owns it,
  -- for the same reason updated_at has one: Python and TypeScript both write
  -- placed_items, and a cached total maintained by only some writers is a
  -- number the user is shown that nobody can justify.
  total_cents bigint not null default 0,
  fits boolean,
  validation_report jsonb,

  plan jsonb,
  generation_meta jsonb not null default '{}'::jsonb,

  version integer not null default 1,
  error_code text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  constraint layouts_budget_positive check (budget_cents > 0),
  constraint layouts_total_not_negative check (total_cents >= 0),
  constraint layouts_currency_is_iso4217 check (currency ~ '^[A-Z]{3}$'),
  constraint layouts_version_positive check (version >= 1),
  -- 5.5 H7 makes "within budget" a hard rule, so a layout that is ready and
  -- does not fit has to say which. `fits` is unknown only while generating.
  constraint layouts_ready_has_a_verdict check (
    status <> 'ready' or fits is not null
  )
);

create index layouts_room_created_idx on public.layouts (room_id, created_at desc);
create index layouts_owner_idx on public.layouts (owner_id);

alter table public.rooms
  add constraint rooms_current_layout_fk
  foreign key (current_layout_id) references public.layouts (id) on delete set null;

-- ---------------------------------------------------------------------------
-- placed_items
-- ---------------------------------------------------------------------------
create table public.placed_items (
  id uuid primary key default public.uuidv7(),
  layout_id uuid not null references public.layouts (id) on delete cascade,
  owner_id uuid not null references public.profiles (id) on delete cascade,
  -- 6.2: restrict, because "products are archived, never hard-deleted while
  -- referenced". A layout that loses the product it was built from cannot be
  -- priced, re-rendered or bought, so the delete is refused rather than the
  -- layout quietly hollowed out.
  product_id uuid not null,

  -- Nullable for manual adds (6.2): an item the user dragged in belongs to
  -- no slot in the plan.
  slot_id text,
  category text not null,

  x_mm integer not null,
  z_mm integer not null,
  elevation_mm integer not null default 0,
  rotation_deg double precision not null default 0,

  price_cents_at_placement bigint not null default 0,
  -- "user pinned: regenerate keeps it" (6.2).
  locked boolean not null default false,
  validation_flags text[] not null default '{}',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  constraint placed_items_slot_id_shape check (slot_id is null or slot_id ~ '^S[0-9]+$'),
  constraint placed_items_price_not_negative check (price_cents_at_placement >= 0),
  constraint placed_items_elevation_not_negative check (elevation_mm >= 0)
);

create index placed_items_layout_idx on public.placed_items (layout_id);
-- 6.2: "(product_id) (price-refresh prioritization and 'unavailable'
-- propagation)" -- the catalog needs to find every layout affected by a
-- product going out of stock.
create index placed_items_product_idx on public.placed_items (product_id);

-- ---------------------------------------------------------------------------
-- total_cents
-- ---------------------------------------------------------------------------
-- Recompute from the items rather than adjusting by a delta. A delta is
-- faster and drifts: one missed path and the cached total disagrees with the
-- sum of the rows forever, which is a number shown next to a budget the user
-- is making decisions against. A layout holds at most ten items (5.4).
create or replace function public.recompute_layout_total()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  target uuid := coalesce(new.layout_id, old.layout_id);
begin
  update public.layouts
     set total_cents = coalesce(
           (select sum(price_cents_at_placement)
              from public.placed_items
             where layout_id = target),
           0
         ),
         updated_at = now()
   where id = target;
  return null;
end;
$$;

create trigger placed_items_maintain_layout_total
  after insert or update or delete on public.placed_items
  for each row execute function public.recompute_layout_total();

create trigger layouts_touch before update on public.layouts
  for each row execute function public.touch_updated_at();
create trigger placed_items_touch before update on public.placed_items
  for each row execute function public.touch_updated_at();
