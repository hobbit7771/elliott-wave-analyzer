-- OrderFlow Terminal persistent history. Everything lives in the dedicated schema "oft",
-- which is NOT exposed through the Supabase Data API (PostgREST). The server connects with its own
-- least-privilege role (created outside this file, see README "Supabase"), never with the service key.

create schema if not exists oft;

-- instruments recorded continuously (independent of what the browser shows)
create table if not exists oft.record_list (
  source text not null,
  symbol text not null,
  enabled boolean not null default true,
  added_at timestamptz not null default now(),
  primary key (source, symbol)
);

create table if not exists oft.instruments (
  source text not null,
  symbol text not null,
  meta jsonb not null,
  updated_at timestamptz not null default now(),
  primary key (source, symbol)
);

-- closed 1m candles (built from live trades or taken from the exchange REST)
create table if not exists oft.candles_1m (
  source text not null,
  symbol text not null,
  t bigint not null,                 -- open time, epoch ms UTC
  o double precision not null,
  h double precision not null,
  l double precision not null,
  c double precision not null,
  v double precision not null,
  bv double precision not null,      -- aggressive buy volume
  n integer,
  origin text not null,              -- 'live' | 'rest'
  stored_at timestamptz not null default now(),
  primary key (source, symbol, t)
);

-- heatmap tiles: numeric aggregates of real local-book snapshots (never derived from OHLC)
create table if not exists oft.heat_tiles (
  source text not null,
  symbol text not null,
  res_s integer not null,            -- 10 or 60 seconds
  t bigint not null,                 -- end time of the tile, epoch ms
  observed_ms integer not null,      -- how long the book was actually observed inside the tile
  step double precision not null,
  data bytea not null,               -- deflated JSON HeatColumn
  stored_at timestamptz not null default now(),
  primary key (source, symbol, res_s, t)
);

-- detector events: latest state + separate status history (no look-ahead: detected_at is stored)
create table if not exists oft.events (
  source text not null,
  symbol text not null,
  id text not null,
  kind text not null,
  t bigint not null,                 -- market time of the event
  detected_at bigint not null,       -- when the detector could first know it
  status_at bigint not null,         -- last status change
  score integer not null,
  status text,
  algo_version text not null,
  body jsonb not null,
  stored_at timestamptz not null default now(),
  primary key (source, symbol, id)
);
create index if not exists events_sym_t on oft.events (source, symbol, t);

create table if not exists oft.event_status (
  source text not null,
  symbol text not null,
  id text not null,
  status_at bigint not null,
  status text,
  score integer not null,
  body jsonb not null,
  primary key (source, symbol, id, status_at)
);

-- recording coverage and gaps (unknown periods are never filled with zeros)
create table if not exists oft.coverage (
  source text not null,
  symbol text not null,
  stream text not null,              -- 'l2' | 'trades'
  t0 bigint not null,
  t1 bigint not null,
  primary key (source, symbol, stream, t0)
);
create table if not exists oft.gaps (
  source text not null,
  symbol text not null,
  stream text not null,
  t0 bigint not null,
  t1 bigint,
  reason text not null,
  primary key (source, symbol, stream, t0)
);

-- manifests of compressed raw blocks stored in the private Storage bucket "oft-archive"
create table if not exists oft.archives (
  path text primary key,
  source text not null,
  symbol text not null,
  kind text not null,                -- 'l2' (snapshot + depth diffs + trades)
  schema_version integer not null,
  t0 bigint not null,
  t1 bigint not null,
  first_update_id bigint,
  last_update_id bigint,
  first_trade_id bigint,
  last_trade_id bigint,
  diffs integer not null,
  trades integer not null,
  depth_band double precision not null,
  bytes integer not null,
  sha256 text not null,
  status text not null,              -- 'pending' -> 'final'
  created_at timestamptz not null default now(),
  finalized_at timestamptz
);
create index if not exists archives_sym_t on oft.archives (source, symbol, t0);

-- key/value settings, user objects (drawings, alert rules), paper state, alert log
create table if not exists oft.settings (
  k text primary key,
  v jsonb not null,
  updated_at timestamptz not null default now()
);
create table if not exists oft.paper (
  account text primary key,
  state jsonb not null,
  updated_at timestamptz not null default now()
);
create table if not exists oft.alert_log (
  id bigserial primary key,
  t bigint not null,
  rule_id integer not null,
  source text not null,
  symbol text not null,
  event_id text not null,
  body jsonb not null,
  unique (rule_id, event_id)
);

-- only the archive edge function reads this (service role); the app role cannot
create table if not exists oft.secrets (
  k text primary key,
  v text not null
);

-- defence in depth: RLS on, no policies => nothing reachable through anon/authenticated API roles
do $$
declare r record;
begin
  for r in select tablename from pg_tables where schemaname = 'oft' loop
    execute format('alter table oft.%I enable row level security', r.tablename);
  end loop;
end $$;

revoke all on schema oft from anon, authenticated;

-- private bucket for raw archive blocks
insert into storage.buckets (id, name, public, file_size_limit)
values ('oft-archive', 'oft-archive', false, 52428800)
on conflict (id) do nothing;
