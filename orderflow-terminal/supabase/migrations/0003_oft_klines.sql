-- Closed exchange klines used by the level / setup engine (history is fetched once, then only the tail).
create table if not exists oft.klines (
  source text not null,
  symbol text not null,
  tf text not null,
  t bigint not null,
  o double precision not null,
  h double precision not null,
  l double precision not null,
  c double precision not null,
  v double precision not null,
  primary key (source, symbol, tf, t)
);
alter table oft.klines enable row level security;
revoke all on oft.klines from anon, authenticated;
grant select, insert, update, delete on oft.klines to oft_app;
drop policy if exists oft_app_all on oft.klines;
create policy oft_app_all on oft.klines for all to oft_app using (true) with check (true);
