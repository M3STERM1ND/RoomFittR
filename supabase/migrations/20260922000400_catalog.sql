-- The furniture catalog (implementation-plan.md 6.3).
--
-- Nothing here is user-owned. These rows are the same for everybody, which
-- is why the RLS migration gives them a different treatment entirely: public
-- read of what is active, writes only from the catalog worker.

create table public.retailers (
  id smallint generated always as identity primary key,
  name text not null,
  domain text not null unique,
  allowed_hosts text[] not null default '{}',
  -- 9.4 makes the crawl policy a legal question as much as a technical one:
  -- robots notes and the ToS decision live here so the answer is recorded
  -- next to the retailer it applies to rather than in someone's memory.
  crawl_policy jsonb not null default '{}'::jsonb,
  extraction_config jsonb not null default '{}'::jsonb,
  affiliate_config jsonb not null default '{}'::jsonb,
  status public.retailer_status not null default 'paused',
  last_crawl_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- product_pages -- the raw fetch ledger (6.3)
-- ---------------------------------------------------------------------------
create table public.product_pages (
  id uuid primary key default public.uuidv7(),
  retailer_id smallint not null references public.retailers (id) on delete cascade,
  url text not null,
  -- Unique on the hash rather than the url: 6.3 asks for a unique constraint
  -- and product urls run past the ~2700-byte limit of a btree entry, so an
  -- index on the url itself can fail on exactly the long tracking-parameter
  -- urls a crawler meets.
  url_hash text not null unique,
  http_status integer,
  content_hash text,
  raw_key text,
  fetched_at timestamptz,
  extract_status text,
  extract_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  constraint product_pages_url_hash_shape check (url_hash ~ '^[0-9a-f]{64}$')
);

create index product_pages_retailer_fetched_idx
  on public.product_pages (retailer_id, fetched_at);

-- ---------------------------------------------------------------------------
-- products
-- ---------------------------------------------------------------------------
create table public.products (
  id uuid primary key default public.uuidv7(),
  retailer_id smallint not null references public.retailers (id) on delete restrict,
  retailer_sku text not null,
  gtin text,
  canonical_url text not null unique,
  title text not null,
  brand text,

  category text not null,
  category_confidence real,
  proxy_family text,
  style_tags text[] not null default '{}',
  color_hex text,
  material text,

  width_mm integer,
  depth_mm integer,
  height_mm integer,
  -- Kept for audit (4.4: "Keep dimension_raw for audit"). When R7 bites and a
  -- sideboard turns out to be listed in inches, this is the only record of
  -- what the page actually said.
  dimension_raw text,
  dimension_source public.dimension_source,
  dimension_confidence real,

  price_cents bigint,
  list_price_cents bigint,
  currency text not null default 'USD',
  price_checked_at timestamptz,
  availability public.availability not null default 'unknown',

  image_url text,
  thumbnail_key text,
  -- 6.3 marks the dimension "[VERIFY dim for chosen model]". 1.2 chose SigLIP
  -- 2, whose base checkpoints embed at 768. If a different checkpoint is
  -- picked, this column changes and the HNSW index is rebuilt -- which is a
  -- migration, so the number is stated here rather than inferred anywhere.
  image_embedding extensions.vector(768),
  glb_key text,

  status public.product_status not null default 'pending_review',
  status_reason text,
  duplicate_group_id uuid,
  consecutive_failures integer not null default 0,
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  constraint products_retailer_sku_key unique (retailer_id, retailer_sku),
  constraint products_color_hex_shape check (color_hex is null or color_hex ~ '^#[0-9a-fA-F]{6}$'),
  constraint products_currency_is_iso4217 check (currency ~ '^[A-Z]{3}$'),
  constraint products_price_not_negative check (price_cents is null or price_cents >= 0),
  -- 9.3's R7 is "wrong dimensions (package vs. assembled, swapped axes)" at
  -- High likelihood and Critical impact. A dimension of zero or of eight
  -- metres is not a furniture measurement, and letting it into the catalog
  -- means the solver reasons about it as though it were.
  constraint products_dimensions_plausible check (
    (width_mm is null or width_mm between 10 and 5000)
    and (depth_mm is null or depth_mm between 10 and 5000)
    and (height_mm is null or height_mm between 10 and 3000)
  ),
  constraint products_confidence_is_a_probability check (
    (category_confidence is null or category_confidence between 0 and 1)
    and (dimension_confidence is null or dimension_confidence between 0 and 1)
  )
);

-- 6.3's index list, each with the query it exists for.
-- "primary shortlist filter" -- 5.4 L3 filters category then price.
create index products_shortlist_idx on public.products (status, category, price_cents);
-- "footprint filter" -- the same query's width/depth bound.
create index products_footprint_idx on public.products (status, category, width_mm, depth_mm);
create index products_style_tags_idx on public.products using gin (style_tags);
-- "refresh scheduler" -- which active products have the stalest prices.
create index products_price_checked_idx on public.products (price_checked_at)
  where status = 'active';
create index products_gtin_idx on public.products (gtin) where gtin is not null;

-- HNSW for "similar style" swaps (1.2). Cosine, because SigLIP embeddings
-- are compared by direction; an L2 index would rank by magnitude too and
-- return a different neighbour set.
create index products_embedding_idx on public.products
  using hnsw (image_embedding extensions.vector_cosine_ops);

-- ---------------------------------------------------------------------------
-- price_history
-- ---------------------------------------------------------------------------
create table public.price_history (
  product_id uuid not null references public.products (id) on delete cascade,
  at timestamptz not null default now(),
  price_cents bigint,
  availability public.availability not null default 'unknown',
  primary key (product_id, at)
);

-- ---------------------------------------------------------------------------
-- crawl_runs
-- ---------------------------------------------------------------------------
create table public.crawl_runs (
  id uuid primary key default public.uuidv7(),
  retailer_id smallint not null references public.retailers (id) on delete cascade,
  kind public.crawl_kind not null,
  status public.crawl_status not null default 'running',
  -- "fetched, new, updated, rejected by reason, errors" (6.3). The
  -- rejected-by-reason breakdown is what 9.3 watches to catch a retailer
  -- changing its page structure.
  stats jsonb not null default '{}'::jsonb,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index crawl_runs_retailer_started_idx
  on public.crawl_runs (retailer_id, started_at desc);

-- ---------------------------------------------------------------------------
-- layout_candidates
-- ---------------------------------------------------------------------------
-- 6.3: "active, non-duplicate-hidden products joined with retailer status.
-- Refreshed concurrently after catalog runs."
--
-- This is the view the layout engine reads, and `roomfittr_layout.Product` is
-- its shape (4.11). A materialized view rather than a plain one because L3
-- runs it once per slot per layout and the join to retailers never changes
-- between catalog runs.
create materialized view public.layout_candidates as
select
  p.id,
  p.retailer_id,
  p.category,
  p.proxy_family,
  p.title,
  p.brand,
  p.width_mm,
  p.depth_mm,
  p.height_mm,
  p.price_cents,
  p.currency,
  p.style_tags,
  p.color_hex,
  p.material,
  p.image_url,
  p.thumbnail_key,
  p.glb_key,
  p.canonical_url,
  p.availability,
  p.price_checked_at,
  p.image_embedding
from public.products p
join public.retailers r on r.id = p.retailer_id
where p.status = 'active'
  and r.status = 'active'
  and p.availability in ('in_stock', 'preorder')
  and p.width_mm is not null
  and p.depth_mm is not null
  and p.height_mm is not null
  and p.price_cents is not null
  -- Only one product of a duplicate group is offered. Showing the same sofa
  -- three times from three retailers is 4.9's deduplication failing in the
  -- one place the user sees it.
  --
  -- `order by id limit 1` rather than `min(id)`: Postgres has no min()
  -- aggregate for uuid before 18. It is also the better rule -- ids are
  -- UUIDv7, so the lowest is the one seen first, and the representative
  -- stays the same as the group grows instead of changing whenever a
  -- retailer with a smaller id turns up.
  and (
    p.duplicate_group_id is null
    or p.id = (
      select d.id
        from public.products d
       where d.duplicate_group_id = p.duplicate_group_id
         and d.status = 'active'
       order by d.id
       limit 1
    )
  );

-- REFRESH ... CONCURRENTLY requires a unique index on the view; without one
-- the refresh takes an exclusive lock and every layout generation blocks
-- behind the nightly catalog run.
create unique index layout_candidates_id_key on public.layout_candidates (id);
create index layout_candidates_shortlist_idx
  on public.layout_candidates (category, price_cents);
create index layout_candidates_footprint_idx
  on public.layout_candidates (category, width_mm, depth_mm);

create trigger retailers_touch before update on public.retailers
  for each row execute function public.touch_updated_at();
create trigger product_pages_touch before update on public.product_pages
  for each row execute function public.touch_updated_at();
create trigger products_touch before update on public.products
  for each row execute function public.touch_updated_at();
create trigger crawl_runs_touch before update on public.crawl_runs
  for each row execute function public.touch_updated_at();

-- placed_items.product_id could not be given its FK until products existed
-- (6.2: restrict, never cascade).
alter table public.placed_items
  add constraint placed_items_product_fk
  foreign key (product_id) references public.products (id) on delete restrict;
