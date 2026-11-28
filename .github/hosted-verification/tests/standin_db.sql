-- LOCAL TEST STAND-IN ONLY (never run against staging). Run as supabase_admin on a local database that already has
-- premium-foundation/tests/sql/000_hosted_supabase_shim.sql and the 21 approved migrations applied.
-- Adds what hosted Supabase has and community PostgreSQL does not: the CLI's migration history table, and a minimal
-- pg_cron-shaped cron.job / cron.job_run_details (0501 skips scheduling when pg_cron is unavailable).
CREATE SCHEMA IF NOT EXISTS supabase_migrations;
CREATE TABLE IF NOT EXISTS supabase_migrations.schema_migrations (version text PRIMARY KEY, name text);
INSERT INTO supabase_migrations.schema_migrations (version)
SELECT v FROM unnest(ARRAY['0001','0002','0003','0004','0005','0101','0102','0103','0201','0301','0401','0402','0403','0404',
                           '0405','0406','0407','0408','0409','0410','0501']) v
ON CONFLICT DO NOTHING;
CREATE SCHEMA IF NOT EXISTS cron;
CREATE TABLE IF NOT EXISTS cron.job (jobid bigserial PRIMARY KEY, schedule text, command text, nodename text DEFAULT 'localhost',
  nodeport int DEFAULT 5432, database text DEFAULT current_database(), username text DEFAULT 'postgres', active boolean DEFAULT true,
  jobname text UNIQUE);
CREATE TABLE IF NOT EXISTS cron.job_run_details (jobid bigint, runid bigserial PRIMARY KEY, job_pid int, database text, username text,
  command text, status text, return_message text, start_time timestamptz, end_time timestamptz);
INSERT INTO cron.job (jobname, schedule, command) VALUES
  ('analytics-rollup-recent', '*/15 * * * *', 'SELECT public.analytics_rollup_pending();'),
  ('analytics-purge', '17 4 * * *', 'SELECT public.analytics_purge();')
ON CONFLICT (jobname) DO NOTHING;
GRANT USAGE ON SCHEMA supabase_migrations, cron TO postgres;
GRANT SELECT ON ALL TABLES IN SCHEMA supabase_migrations TO postgres;
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA cron TO postgres;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA cron TO postgres;
