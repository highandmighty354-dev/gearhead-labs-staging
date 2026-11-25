-- Gearhead Labs staging validation: counts and migration history (read-only). Taken before and after the access matrix;
-- the two outputs must be identical and no validation test user may exist. Output: one JSON object on one line.
select json_build_object(
  'auth_users', (select count(*) from auth.users),
  'test_users', (select count(*) from auth.users
                  where email like '%@example.test'
                     or id in ('a0000000-0000-4000-8000-00000000000a', 'b0000000-0000-4000-8000-00000000000b', 'c0000000-0000-4000-8000-00000000000c')),
  'tables', (select json_object_agg(t, n) from (
               select c.relname::text t,
                      (xpath('/row/n/text()', query_to_xml(format('select count(*) n from public.%I', c.relname), false, true, '')))[1]::text::bigint n
                 from pg_class c join pg_namespace ns on ns.oid = c.relnamespace
                where ns.nspname = 'public' and c.relkind in ('r','p')) s),
  'migrations', (select coalesce(json_agg(version::text order by version::text collate "C"), '[]')
                   from supabase_migrations.schema_migrations)
) as counts;
