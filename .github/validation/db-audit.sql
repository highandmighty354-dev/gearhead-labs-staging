-- Gearhead Labs staging validation: protection audit (read-only).
-- Run twice per validation: against the staging database, and against a throwaway replica built on the runner from the
-- migration files the staging database records as applied. db_check.py compares the two outputs section by section.
-- Every list is sorted with the C collation so the hosted database and the replica sort identically.
-- Output: one JSON object on one line.
select json_build_object(
  'tables', (select coalesce(json_agg(c.relname::text order by c.relname::text collate "C"), '[]')
               from pg_class c join pg_namespace n on n.oid = c.relnamespace
              where n.nspname = 'public' and c.relkind in ('r','p','v','m','f')),
  'rls', (select coalesce(json_agg(x order by x collate "C"), '[]') from (
            select c.relname::text || ':' || c.relkind::text || ':rls=' || c.relrowsecurity::text || ':force=' || c.relforcerowsecurity::text x
              from pg_class c join pg_namespace n on n.oid = c.relnamespace
             where n.nspname = 'public' and c.relkind in ('r','p','v','m','f')) s),
  'views', (select coalesce(json_agg(x order by x collate "C"), '[]') from (
            select c.relname::text || ':' || coalesce(array_to_string(c.reloptions, ','), '-') x
              from pg_class c join pg_namespace n on n.oid = c.relnamespace
             where n.nspname = 'public' and c.relkind in ('v','m')) s),
  'policies', (select coalesce(json_agg(x order by x collate "C"), '[]') from (
            select tablename::text || '|' || policyname::text || '|' || cmd || '|' || array_to_string(roles, ',') || '|' || permissive
                   || '|' || coalesce(qual, '-') || '|' || coalesce(with_check, '-') x
              from pg_policies where schemaname = 'public') s),
  'table_grants', (select coalesce(json_agg(x order by x collate "C"), '[]') from (
            select table_name::text || '|' || grantee::text || '|' || privilege_type::text x
              from information_schema.role_table_grants
             where table_schema = 'public' and grantee in ('anon', 'authenticated', 'PUBLIC')) s),
  'column_grants', (select coalesce(json_agg(x order by x collate "C"), '[]') from (
            select table_name::text || '|' || column_name::text || '|' || grantee::text || '|' || privilege_type::text x
              from information_schema.column_privileges
             where table_schema = 'public' and grantee in ('anon', 'authenticated', 'PUBLIC')) s),
  'functions', (select coalesce(json_agg(x order by x collate "C"), '[]') from (
            select p.proname::text || '(' || pg_get_function_identity_arguments(p.oid) || ')|secdef=' || p.prosecdef::text
                   || '|cfg=' || coalesce(array_to_string(p.proconfig, ';'), '-')
                   || '|anon=' || has_function_privilege('anon', p.oid, 'EXECUTE')::text
                   || '|authenticated=' || has_function_privilege('authenticated', p.oid, 'EXECUTE')::text x
              from pg_proc p join pg_namespace n on n.oid = p.pronamespace where n.nspname = 'public') s),
  -- Every function in public that the anonymous role can EXECUTE (directly or through PUBLIC), with its return type.
  -- Judged against the explicit policy in expected.json ("anon_function_policy"), not only against the replica.
  'anon_executable_functions', (select coalesce(json_agg(x order by x collate "C"), '[]') from (
            select p.proname::text || '(' || pg_get_function_identity_arguments(p.oid) || ')|returns=' || pg_get_function_result(p.oid)
                   || '|secdef=' || p.prosecdef::text x
              from pg_proc p join pg_namespace n on n.oid = p.pronamespace
             where n.nspname = 'public' and has_function_privilege('anon', p.oid, 'EXECUTE')) s),
  'triggers', (select coalesce(json_agg(x order by x collate "C"), '[]') from (
            select c.relname::text || '|' || t.tgname::text || '|' || pg_get_triggerdef(t.oid) x
              from pg_trigger t join pg_class c on c.oid = t.tgrelid join pg_namespace n on n.oid = c.relnamespace
             where n.nspname = 'public' and not t.tgisinternal) s),
  'columns', (select coalesce(json_agg(x order by x collate "C"), '[]') from (
            select table_name::text || '.' || column_name::text || ':' || data_type::text || ':' || is_nullable::text
                   || ':' || coalesce(column_default::text, '') x
              from information_schema.columns where table_schema = 'public') s),
  'constraints', (select coalesce(json_agg(x order by x collate "C"), '[]') from (
            select conrelid::regclass::text || ':' || conname::text || ':' || pg_get_constraintdef(oid) x
              from pg_constraint where connamespace = 'public'::regnamespace) s),
  'enums', (select coalesce(json_agg(x order by x collate "C"), '[]') from (
            select t.typname::text || ':' || string_agg(e.enumlabel::text, ',' order by e.enumsortorder) x
              from pg_type t join pg_enum e on e.enumtypid = t.oid
             where t.typnamespace = 'public'::regnamespace group by t.typname) s),
  -- Reference data: one hash per table over the md5 of every row, the row hashes sorted with the C collation.
  'reference_data', (select json_object_agg(t, h) from (
            select c.relname::text t,
                   (xpath('/row/h/text()', query_to_xml(format(
                      'select md5(coalesce(string_agg(md5(x::text), %L order by md5(x::text) collate "C"), %L)) h from public.%I x',
                      '', '', c.relname), false, true, '')))[1]::text h
              from pg_class c join pg_namespace n on n.oid = c.relnamespace
             where n.nspname = 'public' and c.relkind = 'r'
               and c.relname in ('calculators','canonical_fields','formula_versions','plans','plan_offers','plan_prices','engineering_analyzers')) s)
) as audit;
