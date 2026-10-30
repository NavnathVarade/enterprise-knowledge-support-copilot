-- Read-only role for the SQL analytics executor (Spec §39-40).
-- Run ONCE per environment by a DBA / Terraform / init job as a privileged user, AFTER
-- the migrations. The password is supplied out-of-band, e.g.:
--   psql -v ro_password="$ANALYTICS_RO_PASSWORD" -f analytics_readonly_role.sql
-- Never commit a real password. DATABASE_READ_ONLY_ROLE_URL must log in as this role.
--
-- What this role can do: SELECT on the six analytics tables ONLY, and only rows whose
-- tenant_id equals app.current_tenant_id (row-level security, migration 0004).
-- What it cannot do: write anything, read documents/users/embeddings or any other table,
-- create objects, or run a statement longer than the timeout below.

-- psql does not substitute variables inside dollar-quoted blocks, so the role is created
-- with format(%L) + \gexec instead (the password is quoted as a literal by Postgres).
SELECT 'CREATE ROLE ekc_analytics_ro LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ekc_analytics_ro')
\gexec

-- Always (re)apply the password so re-running this script rotates it.
SELECT format('ALTER ROLE ekc_analytics_ro PASSWORD %L', :'ro_password')
\gexec

REVOKE ALL ON SCHEMA public FROM ekc_analytics_ro;
GRANT USAGE ON SCHEMA public TO ekc_analytics_ro;

GRANT SELECT ON
  customers, support_tickets, ticket_events, incidents, incident_events, deployments
TO ekc_analytics_ro;

ALTER ROLE ekc_analytics_ro SET default_transaction_read_only = on;
ALTER ROLE ekc_analytics_ro SET statement_timeout = '10s';
ALTER ROLE ekc_analytics_ro SET idle_in_transaction_session_timeout = '15s';
ALTER ROLE ekc_analytics_ro CONNECTION LIMIT 20;
