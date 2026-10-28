#!/bin/sh
# Runs once, when the postgres volume is first initialised (docker-entrypoint-initdb.d).
# Creates the three application roles (build prompt section 7):
#   alloyiq_migrator  owns the schema and tables; used only by Alembic
#   alloyiq_api       API role: not an owner, no BYPASSRLS, no superuser
#   alloyiq_worker    worker role: separately scoped
# Table-level grants and RLS policies are created by the phase 1 migrations.
set -eu

: "${ALLOYIQ_API_DB_PASSWORD:?}" "${ALLOYIQ_WORKER_DB_PASSWORD:?}" "${ALLOYIQ_MIGRATOR_DB_PASSWORD:?}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  -v api_pw="$ALLOYIQ_API_DB_PASSWORD" \
  -v worker_pw="$ALLOYIQ_WORKER_DB_PASSWORD" \
  -v migrator_pw="$ALLOYIQ_MIGRATOR_DB_PASSWORD" <<'SQL'
CREATE ROLE alloyiq_migrator LOGIN PASSWORD :'migrator_pw' NOSUPERUSER NOCREATEROLE NOBYPASSRLS;
CREATE ROLE alloyiq_api      LOGIN PASSWORD :'api_pw'      NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE alloyiq_worker   LOGIN PASSWORD :'worker_pw'   NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;

CREATE DATABASE alloyiq      OWNER alloyiq_migrator;
CREATE DATABASE alloyiq_test OWNER alloyiq_migrator;
CREATE DATABASE alloyiq_e2e  OWNER alloyiq_migrator;

-- Only the application roles may connect.
REVOKE ALL ON DATABASE alloyiq, alloyiq_test, alloyiq_e2e FROM PUBLIC;
GRANT CONNECT ON DATABASE alloyiq, alloyiq_test, alloyiq_e2e TO alloyiq_api, alloyiq_worker, alloyiq_migrator;
SQL
