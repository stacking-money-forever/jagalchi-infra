#!/usr/bin/env bash
set -Eeuo pipefail

ENV_FILE="${1:-}"
BACKUP_DIR="${2:-}"

if [[ ! -f "$ENV_FILE" || ! -d "$BACKUP_DIR" ]]; then
  echo "usage: $0 /absolute/path/to/jagalchi-production.env /absolute/path/to/backup" >&2
  exit 2
fi
if [[ ! -f "$BACKUP_DIR/local-api-db.dump" || ! -f "$BACKUP_DIR/local-api-db-counts.tsv" ]]; then
  echo "legacy local API database was not running; no automatic data import needed"
  exit 0
fi

env_value() {
  awk -F= -v wanted="$1" '$1 == wanted {sub(/^[^=]*=/, ""); gsub(/^['"'"']|['"'"']$/, ""); print; exit}' "$ENV_FILE"
}
database_url="$(env_value DATABASE_URL)"
existing_data_policy="$(env_value SUPABASE_EXISTING_DATA_POLICY)"
[[ -n "$database_url" ]]
export DATABASE_URL="$database_url"

query_target() {
  docker run --rm -e DATABASE_URL -e PGSSLMODE=verify-full -e PGSSLROOTCERT=/backup/supabase-ca.crt \
    -v "$BACKUP_DIR:/backup:ro" postgres:17-alpine \
    sh -ec 'exec psql "$DATABASE_URL" --quiet --tuples-only --no-align "$@"' sh "$@"
}

target_table_count="$(query_target --command="SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE';")"

collect_target_counts() {
  local count_sql
  count_sql="$(cat <<'SQL'
CREATE TEMP TABLE jagalchi_row_counts(table_name text, row_count bigint);
DO $block$
DECLARE record_row record;
BEGIN
  FOR record_row IN
    SELECT table_schema, table_name
    FROM information_schema.tables
    WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
  LOOP
    EXECUTE format(
      'INSERT INTO jagalchi_row_counts SELECT %L, count(*)::bigint FROM %I.%I',
      record_row.table_name,
      record_row.table_schema,
      record_row.table_name
    );
  END LOOP;
END
$block$;
SELECT table_name, row_count FROM jagalchi_row_counts ORDER BY table_name;
SQL
)"
  query_target --command="$count_sql" > "$BACKUP_DIR/supabase-counts.tsv"
}

if ((target_table_count > 0)); then
  collect_target_counts
  if cmp -s "$BACKUP_DIR/local-api-db-counts.tsv" "$BACKUP_DIR/supabase-counts.tsv"; then
    echo "Supabase already matches the legacy local API database; import skipped"
    exit 0
  fi
  if [[ "$existing_data_policy" == "authoritative" ]]; then
    cut -d '|' -f 1 "$BACKUP_DIR/local-api-db-counts.tsv" > "$BACKUP_DIR/local-api-tables.txt"
    cut -d '|' -f 1 "$BACKUP_DIR/supabase-counts.tsv" > "$BACKUP_DIR/supabase-tables.txt"
    if ! cmp -s "$BACKUP_DIR/local-api-tables.txt" "$BACKUP_DIR/supabase-tables.txt"; then
      echo "Supabase table set differs from the local API database; refusing authoritative cutover" >&2
      exit 1
    fi
    if [[ ! -f "$BACKUP_DIR/local-api-migrations.tsv" || ! -f "$BACKUP_DIR/supabase-migrations.tsv" ]] \
      || ! cmp -s "$BACKUP_DIR/local-api-migrations.tsv" "$BACKUP_DIR/supabase-migrations.tsv"; then
      echo "Supabase migration history differs from the local API database; refusing authoritative cutover" >&2
      exit 1
    fi
    echo "Supabase is explicitly authoritative with matching tables and migrations; local import skipped"
    exit 0
  fi
  echo "Supabase public schema is not empty and differs from the local API database; refusing automatic overwrite or merge" >&2
  exit 1
fi

# Supabase already owns the public schema. Exclude only that schema-creation TOC
# entry; restore every application object and row without clean/drop operations.
grep -v ' SCHEMA - public ' "$BACKUP_DIR/local-api-db.contents.txt" > "$BACKUP_DIR/local-api-db.restore.list"
docker run --rm \
  -e DATABASE_URL \
  -e PGSSLROOTCERT=/backup/supabase-ca.crt \
  -v "$BACKUP_DIR:/backup:ro" \
  postgres:17-alpine \
  sh -ec 'exec pg_restore --dbname="$DATABASE_URL" --single-transaction --exit-on-error \
    --no-owner --no-privileges --use-list=/backup/local-api-db.restore.list \
    /backup/local-api-db.dump'

collect_target_counts
if ! cmp -s "$BACKUP_DIR/local-api-db-counts.tsv" "$BACKUP_DIR/supabase-counts.tsv"; then
  echo "Supabase row-count verification failed after import" >&2
  exit 1
fi
echo "legacy local API database imported into Supabase with matching table row counts"
