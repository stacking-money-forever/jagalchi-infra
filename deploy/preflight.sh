#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${1:-}"

if [[ -z "$ENV_FILE" || ! -f "$ENV_FILE" ]]; then
  echo "usage: $0 /absolute/path/to/jagalchi-production.env" >&2
  exit 2
fi

for command in docker awk stat; do
  command -v "$command" >/dev/null 2>&1 || { echo "$command is not installed" >&2; exit 1; }
done
docker compose version >/dev/null 2>&1 || { echo "docker compose plugin is not installed" >&2; exit 1; }

env_value() {
  awk -F= -v wanted="$1" '$1 == wanted {sub(/^[^=]*=/, ""); gsub(/^['"'"']|['"'"']$/, ""); print; exit}' "$ENV_FILE"
}

required_keys=(
  API_DOMAIN UPLOADS_DOMAIN API_IMAGE AI_IMAGE CLOUDFLARE_TUNNEL_TOKEN PUBLIC_API_URL WEB_APP_URL
  CORS_ORIGINS TRUST_PROXY_HOPS DATABASE_URL DATABASE_SSL DATABASE_SSL_CA DATABASE_SYNCHRONIZE
  POSTGRES_PASSWORD JWT_ACCESS_SECRET AI_AUTH_JWT_SECRET VERIFICATION_CODE_SECRET
  RATE_LIMIT_HASH_SECRET DJANGO_SECRET_KEY GITHUB_APP_ID GITHUB_APP_PRIVATE_KEY
  GITHUB_APP_WEBHOOK_SECRET GITHUB_APP_SLUG
  OAUTH_GOOGLE_CLIENT_ID OAUTH_GOOGLE_CLIENT_SECRET OAUTH_GITHUB_CLIENT_ID
  OAUTH_GITHUB_CLIENT_SECRET RESEND_API_KEY EMAIL_FROM OBJECT_STORAGE_BUCKET
  OBJECT_STORAGE_REGION OBJECT_STORAGE_ACCESS_KEY_ID OBJECT_STORAGE_SECRET_ACCESS_KEY
  DEEPSEEK_API_KEY TAVILY_API_KEY EXA_API_KEY
)

invalid=()
for key in "${required_keys[@]}"; do
  value="$(env_value "$key")"
  if [[ -z "$value" || "$value" == *replace-with-* || "$value" == *replace-me* || "$value" == *example.com* ]]; then
    invalid+=("$key")
  fi
done

for key in API_IMAGE AI_IMAGE; do
  image_value="$(env_value "$key")"
  [[ "$image_value" =~ ^ghcr\.io/stacking-money-forever/jagalchi-(api|ai):[A-Za-z0-9._-]+$ ]] || {
    echo "$key must pin a stacking-money-forever GHCR image tag" >&2
    exit 1
  }
done
if ((${#invalid[@]})); then
  echo "missing or placeholder production values:" >&2
  printf ' - %s\n' "${invalid[@]}" >&2
  exit 1
fi

expect_value() {
  local key="$1" expected="$2" actual
  actual="$(env_value "$key")"
  [[ "$actual" == "$expected" ]] || { echo "$key must be $expected" >&2; exit 1; }
}

expect_value DATABASE_SSL true
expect_value DATABASE_SYNCHRONIZE false
expect_value OAUTH_ENABLED true
expect_value OAUTH_APPLE_ENABLED false
expect_value IAP_ENABLED false
expect_value EMAIL_ENABLED true
expect_value AI_DISABLE_EXTERNAL false
expect_value AI_DISABLE_LLM false
deepseek_base_url="$(env_value DEEPSEEK_BASE_URL)"
[[ -z "$deepseek_base_url" || "$deepseek_base_url" == "https://api.deepseek.com" ]] || {
  echo "DEEPSEEK_BASE_URL must be https://api.deepseek.com" >&2
  exit 1
}

case "$(env_value DEEPSEEK_MODEL)" in
  ""|deepseek-v4-flash|deepseek-v4-pro) ;;
  *) echo "DEEPSEEK_MODEL must be deepseek-v4-flash or deepseek-v4-pro" >&2; exit 1 ;;
esac
case "$(env_value DEEPSEEK_THINKING_ENABLED)" in ""|true|false) ;; *) echo "DEEPSEEK_THINKING_ENABLED must be true or false" >&2; exit 1 ;; esac

database_url="$(env_value DATABASE_URL)"
if [[ ! "$database_url" =~ ^postgres(ql)?:// ]] || [[ "$database_url" =~ @(api-db|localhost|127\.0\.0\.1)(:|/) ]]; then
  echo "DATABASE_URL must target an external PostgreSQL service" >&2
  exit 1
fi
[[ "$database_url" =~ @[^/?]*\.supabase\.(co|com)(:|/) ]] || {
  echo "DATABASE_URL must target Supabase PostgreSQL" >&2
  exit 1
}
[[ "$database_url" != *"sslmode="* ]] || {
  echo "DATABASE_URL must not contain sslmode; DATABASE_SSL_CA supplies Node TLS trust and psql forces verify-full separately" >&2
  exit 1
}
database_ssl_ca="$(env_value DATABASE_SSL_CA)"
normalized_ca="${database_ssl_ca//\\n/$'\n'}"
[[ "$normalized_ca" == '-----BEGIN CERTIFICATE-----'$'\n'*$'\n''-----END CERTIFICATE-----' ]] || {
  echo "DATABASE_SSL_CA must be a PEM certificate" >&2
  exit 1
}

for key in JWT_ACCESS_SECRET AI_AUTH_JWT_SECRET VERIFICATION_CODE_SECRET RATE_LIMIT_HASH_SECRET DJANGO_SECRET_KEY GITHUB_APP_WEBHOOK_SECRET; do
  secret_value="$(env_value "$key")"
  ((${#secret_value} >= 32)) || { echo "$key must contain at least 32 characters" >&2; exit 1; }
done
[[ "$(env_value GITHUB_APP_ID)" =~ ^[0-9]+$ ]] || { echo "GITHUB_APP_ID must be numeric" >&2; exit 1; }
[[ "$(env_value RESEND_API_KEY)" =~ ^re_[A-Za-z0-9_-]{16,}$ ]] || { echo "RESEND_API_KEY is invalid" >&2; exit 1; }
mode="$(stat -c '%a' "$ENV_FILE" 2>/dev/null || stat -f '%Lp' "$ENV_FILE")"
if ((10#$mode % 100 > 0)); then
  echo "environment file must have mode 600" >&2
  exit 1
fi

docker compose --env-file "$ENV_FILE" -f "$ROOT_DIR/compose.production.yml" --profile cloudflare-tunnel config --quiet

available_kb="$(df -Pk "$ROOT_DIR" | awk 'NR==2 {print $4}')"
((available_kb >= 10 * 1024 * 1024)) || { echo "at least 10 GiB of free disk space is required" >&2; exit 1; }

echo "preflight passed"
