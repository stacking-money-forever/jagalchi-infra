#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${1:-}"
RELEASE="${RELEASE:-$(git -C "$ROOT_DIR" rev-parse --short=12 HEAD 2>/dev/null || date -u +%Y%m%d%H%M%S)}"
COMPOSE=(docker compose --env-file "$ENV_FILE" -f "$ROOT_DIR/compose.production.yml" --profile cloudflare-tunnel)
STATE_DIR="${DEPLOY_STATE_DIR:-$ROOT_DIR/.deploy-state}"
BACKUP_DIR=""
DEPLOY_MUTATED=false

rollback_on_error() {
  local exit_code=$?
  if [[ "$DEPLOY_MUTATED" == "true" && -n "$BACKUP_DIR" && -f "$BACKUP_DIR/rollback.env" ]]; then
    local rollback_api rollback_ai
    rollback_api="$(awk -F= '$1 == "API_IMAGE" {print $2}' "$BACKUP_DIR/rollback.env")"
    rollback_ai="$(awk -F= '$1 == "AI_IMAGE" {print $2}' "$BACKUP_DIR/rollback.env")"
    if [[ -n "$rollback_api" && -n "$rollback_ai" ]]; then
      echo "deployment failed; restoring previous application images" >&2
      API_IMAGE="$rollback_api" AI_IMAGE="$rollback_ai" \
        "${COMPOSE[@]}" up -d --no-deps --no-build api ai || true
    fi
  fi
  exit "$exit_code"
}
trap rollback_on_error ERR

"$ROOT_DIR/deploy/preflight.sh" "$ENV_FILE"

if git -C "$ROOT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 &&
  [[ -n "$(git -C "$ROOT_DIR" status --short)" ]] &&
  [[ "${ALLOW_DIRTY:-false}" != "true" ]]; then
  echo "refusing to tag a dirty working tree as $RELEASE (commit changes or set ALLOW_DIRTY=true)" >&2
  exit 1
fi

echo "pulling reviewed service images for infra release $RELEASE"
"${COMPOSE[@]}" pull api api-migrate ai

echo "creating pre-migration backups"
BACKUP_DIR="$("$ROOT_DIR/deploy/backup-before-deploy.sh" "$ENV_FILE")"
echo "backup created at $BACKUP_DIR"

echo "checking or importing legacy local API data"
"$ROOT_DIR/deploy/migrate-local-api-db-to-supabase.sh" "$ENV_FILE" "$BACKUP_DIR"

echo "checking Supabase migration state"
"${COMPOSE[@]}" run --rm --no-deps api-migrate node dist/database/check-migrations.js

echo "starting release $RELEASE"
DEPLOY_MUTATED=true
"${COMPOSE[@]}" up -d --remove-orphans

echo "verifying Supabase migrations"
"${COMPOSE[@]}" run --rm --no-deps api-migrate node dist/database/check-migrations.js | grep -Fx "no pending migrations"

"$ROOT_DIR/deploy/smoke.sh" "$ENV_FILE"

mkdir -p "$STATE_DIR"
if [[ -f "$STATE_DIR/current-release" ]]; then
  cp "$STATE_DIR/current-release" "$STATE_DIR/previous-release"
fi
printf '%s\n' "$RELEASE" > "$STATE_DIR/current-release"
printf '%s\n' "$BACKUP_DIR" > "$STATE_DIR/last-backup"
DEPLOY_MUTATED=false
trap - ERR
echo "release $RELEASE is healthy"
