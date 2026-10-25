-- 02_readonly_role.sql
-- Creates a dedicated read-only role for AskData query execution.
-- This role can only SELECT from tables in the public schema.

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'askdata_reader') THEN
    CREATE ROLE askdata_reader LOGIN PASSWORD 'askdata_readonly_pw';
  END IF;
END
$$;

-- Grant connect to the database
GRANT CONNECT ON DATABASE northwind TO askdata_reader;

-- Grant usage on the public schema
GRANT USAGE ON SCHEMA public TO askdata_reader;

-- Grant SELECT on all current tables
GRANT SELECT ON ALL TABLES IN SCHEMA public TO askdata_reader;

-- Ensure future tables are also readable
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO askdata_reader;
