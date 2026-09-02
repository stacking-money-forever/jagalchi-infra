#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${1:-}"
BACKUP_ROOT="${2:-/var/backups/jagalchi}"

if [[ -z "$ENV_FILE" || ! -f "$ENV_FILE" ]]; then
  echo "usage: $0 /absolute/path/to/jagalchi-production.env [backup-root]" >&2
  exit 2
fi

umask 077
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_dir="$BACKUP_ROOT/$timestamp"
mkdir -p "$backup_dir"

env_value() {
  awk -F= -v wanted="$1" '$1 == wanted {sub(/^[^=]*=/, ""); gsub(/^['"'"']|['"'"']$/, ""); print; exit}' "$ENV_FILE"
}

database_url="$(env_value DATABASE_URL)"
database_ssl_ca="$(env_value DATABASE_SSL_CA)"
if [[ -z "$database_url" || -z "$database_ssl_ca" ]]; then
  echo "DATABASE_URL and DATABASE_SSL_CA are required for the pre-migration backup" >&2
  exit 1
fi
normalized_ca="${database_ssl_ca//\\n/$'\n'}"
printf '%s\n' "$normalized_ca" > "$backup_dir/supabase-ca.crt"
chmod 600 "$backup_dir/supabase-ca.crt"
export DATABASE_URL="$database_url"

compose=(docker compose --env-file "$ENV_FILE" -f "$ROOT_DIR/compose.production.yml")

printf 'created_at=%s\nsource_commit=%s\nenv_sha256=%s\n' \
  "$timestamp" \
  "$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || printf unknown)" \
  "$(sha256sum "$ENV_FILE" 2>/dev/null | awk '{print $1}' || shasum -a 256 "$ENV_FILE" | awk '{print $1}')" \
  > "$backup_dir/manifest.txt"

# The remote Supabase snapshot is mandatory before running TypeORM migrations.
docker run --rm \
  -e DATABASE_URL \
  -e PGSSLMODE=verify-full \
  -e PGSSLROOTCERT=/backup/supabase-ca.crt \
  -v "$backup_dir:/backup" \
  postgres:17-alpine \
  sh -ec 'pg_dump --format=custom --file=/backup/supabase-before-migration.dump "$DATABASE_URL"'
docker run --rm \
  -v "$backup_dir:/backup:ro" \
  postgres:17-alpine \
  pg_restore --list /backup/supabase-before-migration.dump \
  > "$backup_dir/supabase-before-migration.contents.txt"
docker run --rm \
  -e DATABASE_URL \
  -e PGSSLMODE=verify-full \
  -e PGSSLROOTCERT=/backup/supabase-ca.crt \
  -v "$backup_dir:/backup:ro" \
  postgres:17-alpine \
  psql "$DATABASE_URL" --quiet --tuples-only --no-align --field-separator='|' \
  --command='SELECT "timestamp", name FROM jagalchi_migrations ORDER BY "timestamp";' \
  > "$backup_dir/supabase-migrations.tsv"

# Preserve the previous local API database if its container still exists.
local_api_id="$("${compose[@]}" --profile local-api-db ps -q api-db 2>/dev/null || true)"
if [[ -n "$local_api_id" ]]; then
  docker exec "$local_api_id" pg_dump --format=custom --username=jagalchi_api --dbname=jagalchi_api \
    > "$backup_dir/local-api-db.dump"
  docker exec -i "$local_api_id" psql --username=jagalchi_api --dbname=jagalchi_api --tuples-only --no-align \
    > "$backup_dir/local-api-db-counts.tsv" <<'SQL'
SELECT format('SELECT %L, count(*)::bigint FROM %I.%I;', table_name, table_schema, table_name)
FROM information_schema.tables
WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
ORDER BY table_name
\gexec
SQL
  docker exec "$local_api_id" psql --username=jagalchi_api --dbname=jagalchi_api \
    --quiet --tuples-only --no-align --field-separator='|' \
    --command='SELECT "timestamp", name FROM jagalchi_migrations ORDER BY "timestamp";' \
    > "$backup_dir/local-api-migrations.tsv"
  docker run --rm -v "$backup_dir:/backup:ro" postgres:17-alpine \
    pg_restore --list /backup/local-api-db.dump > "$backup_dir/local-api-db.contents.txt"
  echo "local_api_db=backed_up" >> "$backup_dir/manifest.txt"
else
  echo "local_api_db=container_not_running_volume_preserved" >> "$backup_dir/manifest.txt"
fi

ai_db_id="$("${compose[@]}" ps -q ai-db 2>/dev/null || true)"
if [[ -n "$ai_db_id" ]]; then
  docker exec "$ai_db_id" pg_dump --format=custom --username=jagalchi_ai --dbname=jagalchi_ai \
    > "$backup_dir/ai-db.dump"
  echo "ai_db=backed_up" >> "$backup_dir/manifest.txt"
else
  echo "ai_db=container_not_running_volume_preserved" >> "$backup_dir/manifest.txt"
fi

if docker volume inspect jagalchi-personal-object-storage >/dev/null 2>&1; then
  docker run --rm --entrypoint /bin/sh \
    -v jagalchi-personal-object-storage:/data:ro \
    -v "$backup_dir:/backup" \
    postgres:17-alpine \
    -ec 'tar -czf /backup/object-storage.tar.gz -C /data .'
  echo "object_storage=backed_up" >> "$backup_dir/manifest.txt"
else
  echo "object_storage=volume_not_found" >> "$backup_dir/manifest.txt"
fi

docker image inspect jagalchi-personal-api:production jagalchi-personal-ai:production \
  --format '{{.RepoTags}} {{.Id}}' > "$backup_dir/previous-images.txt" 2>/dev/null || true

api_id="$("${compose[@]}" ps -q api 2>/dev/null || true)"
ai_id="$("${compose[@]}" ps -q ai 2>/dev/null || true)"
if [[ -n "$api_id" && -n "$ai_id" ]]; then
  rollback_api="jagalchi-personal-api:rollback-$timestamp"
  rollback_ai="jagalchi-personal-ai:rollback-$timestamp"
  docker tag "$(docker inspect --format '{{.Image}}' "$api_id")" "$rollback_api"
  docker tag "$(docker inspect --format '{{.Image}}' "$ai_id")" "$rollback_ai"
  printf 'API_IMAGE=%s\nAI_IMAGE=%s\n' "$rollback_api" "$rollback_ai" > "$backup_dir/rollback.env"
fi

sudo chown -R "$(id -u):$(id -g)" "$backup_dir"
chmod -R go-rwx "$backup_dir"
printf '%s\n' "$backup_dir"
