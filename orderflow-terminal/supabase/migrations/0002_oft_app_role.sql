-- Least-privilege role used by the Render server (password is set out-of-band, never committed):
--   create role oft_app login password '<from Render secret OFT_DB_PASSWORD>';
-- Then run the grants below. The role owns nothing, cannot read oft.secrets and has no access to
-- other schemas used by other projects.
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'oft_app') then
    raise notice 'create role oft_app first';
  end if;
end $$;

grant usage on schema oft to oft_app;
grant select, insert, update, delete on
  oft.record_list, oft.instruments, oft.candles_1m, oft.heat_tiles, oft.events, oft.event_status,
  oft.coverage, oft.gaps, oft.archives, oft.settings, oft.paper, oft.alert_log
  to oft_app;
grant usage, select on all sequences in schema oft to oft_app;
-- the app role bypasses nothing: give it explicit RLS policies on its own tables only
do $$
declare t text;
begin
  foreach t in array array['record_list','instruments','candles_1m','heat_tiles','events','event_status','coverage','gaps','archives','settings','paper','alert_log'] loop
    execute format('drop policy if exists oft_app_all on oft.%I', t);
    execute format('create policy oft_app_all on oft.%I for all to oft_app using (true) with check (true)', t);
  end loop;
end $$;
-- storage size accounting helper (read-only)
create or replace function oft.storage_usage() returns table(bucket text, objects bigint, bytes bigint)
language sql security definer set search_path = '' as $$
  select bucket_id::text, count(*), coalesce(sum((metadata->>'size')::bigint), 0)
  from storage.objects where bucket_id = 'oft-archive' group by bucket_id
$$;
revoke all on function oft.storage_usage() from public;
grant execute on function oft.storage_usage() to oft_app;
