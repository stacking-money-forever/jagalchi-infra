# Personal server deployment

The current release target is the Vercel-hosted web application backed by a
single personal-server VM. The VM runs the Nest API, Django AI service, AI
PostgreSQL, MinIO, and an outbound-only Cloudflare Tunnel connector. The Nest
API uses Supabase PostgreSQL with verified TLS. Apple,
mobile, and in-app purchase credentials are not part of this web release.

This runbook assumes the checkout is `/srv/jagalchi-platform` on the Ubuntu VM
and that the `deploy` operator can use Docker Compose. Run root-only backup and
environment-file operations with `sudo`; do not run
`deploy/bootstrap-ubuntu.sh` on an existing host. Before deployment, record the
intended source revision and confirm at least 10 GiB is free.

## Ingress contract

The production profile uses a remotely-managed Cloudflare Tunnel because the
home server cannot accept forwarded inbound ports. The connector needs outbound
TCP or UDP 7844 and no inbound 80/443 rule.

Create the tunnel and DNS routes in Cloudflare only after operator approval.
Configure exactly these published application routes in the Cloudflare dashboard:

| Public hostname | Tunnel service URL |
| --- | --- |
| value of `API_DOMAIN` | `http://api:8080` |
| value of `UPLOADS_DOMAIN` | `http://minio:9000` |

Do not place Cloudflare Access authentication in front of either public route;
the browser and OAuth/provider callbacks must reach the application endpoints.
Keep MinIO console port 9001 unpublished. The pinned `cloudflared` container
receives the remotely-managed tunnel token through a Compose secret file rather
than a container environment variable. The operator stores
`CLOUDFLARE_TUNNEL_TOKEN` in `/etc/jagalchi/jagalchi-production.env`; Compose
materializes it only inside the connector as
`/run/secrets/cloudflare_tunnel_token`.

The uploads hostname exposes the MinIO API, not its console. Only
`<OBJECT_STORAGE_BUCKET>/public/profiles/` is anonymous; uploads remain gated by
the Nest API's presigned-upload flow. Verify both DNS records resolve through
the intended tunnel before deployment.

The existing Caddy service is retained behind the opt-in `direct-ingress`
Compose profile for a future network with working 80/443 forwarding. It is not
started by the current `cloudflare-tunnel` profile.

## Production environment

The canonical schema is `deploy/personal-server.env.example`. Copy it outside
the repository, fill each active placeholder, and restrict it to its owner:

```bash
install -m 600 deploy/personal-server.env.example /etc/jagalchi/jagalchi-production.env
```

The web-release feature boundary must remain:

```dotenv
OAUTH_ENABLED=true
OAUTH_APPLE_ENABLED=false
IAP_ENABLED=false
EMAIL_ENABLED=true
AI_DISABLE_EXTERNAL=false
AI_DISABLE_LLM=false
```

No Apple, IAP, or mobile credential belongs in this environment. Google/GitHub
OAuth, GitHub App evidence execution, Resend, DeepSeek, Tavily, Exa, MinIO, and
Cloudflare Tunnel are release requirements. Multiline PEM values use literal
`\n`. The Nest API database URL must target external PostgreSQL and
`DATABASE_SSL=true`, include `sslmode=verify-full`, and provide the project CA
as `DATABASE_SSL_CA`. On IPv4-only hosts use the Supabase Session pooler on
port 5432; use the direct connection only when the host has working IPv6 or the
project has the IPv4 add-on. `DATABASE_SYNCHRONIZE` must remain false.

Copy `DATABASE_URL` and the CA from the intended Supabase project's Connect and
Database Settings panels. Confirm the project reference in the hostname before
preflight. The scripts can prove TLS and database access, but cannot infer that
the operator selected the correct Supabase project.

DeepSeek uses the official OpenAI-compatible origin
`https://api.deepseek.com`. `deepseek-v4-flash` with thinking disabled is the
default for the application's frequent JSON generation calls. Set
`DEEPSEEK_MODEL=deepseek-v4-pro` or enable thinking only after measuring the
latency and cost impact on a target-shaped request.

## Preflight, deploy, and verify

Run from the repository checkout on the VM:

```bash
cd /srv/jagalchi-infra
./deploy/preflight.sh /etc/jagalchi/jagalchi-production.env
./deploy/deploy.sh /etc/jagalchi/jagalchi-production.env
```

The deployment scripts select the `cloudflare-tunnel` Compose profile directly;
the operator does not need to manage `COMPOSE_PROFILES`. Preflight prints only
the names of unresolved variables, never their values. The deploy command pulls
the reviewed `API_IMAGE` and `AI_IMAGE` tags pinned in the server-owned environment
file; application images are built and verified by their service repositories.
Before mutation it dumps the target
Supabase database, any running legacy local API database, the AI database, and
the MinIO volume under `/var/backups/jagalchi`. It then performs a read-only
migration check, starts the stack with a transaction-protected one-shot
migration, verifies that no migrations remain, and runs public API, MinIO,
OAuth, webhook-boundary, email-credential, and external-AI smoke tests. It
rejects a dirty working tree unless the
operator explicitly sets `ALLOW_DIRTY=true`.

A failed deployment restores the previously running API and AI image tags when
they were available, and does not record the release as current. On a first
deployment with no running images, automatic rollback is unavailable. Database
migrations are not automatically reversed; a migration that succeeds before a
later smoke failure remains applied.

Each backup directory contains custom-format database dumps, TOC listings,
exact legacy/target row-count manifests, an object-storage archive, image tags,
and checksums. Copy it off-host after deployment. Inspect database dumps with
`pg_restore --list`; restore only during a reviewed outage using the same
Supabase CA and a newly verified target. Useful read-only operations are:

```bash
docker compose --env-file /etc/jagalchi/jagalchi-production.env -f compose.production.yml --profile cloudflare-tunnel ps
docker compose --env-file /etc/jagalchi/jagalchi-production.env -f compose.production.yml --profile cloudflare-tunnel logs --tail=200 api ai cloudflared
./deploy/smoke.sh /etc/jagalchi/jagalchi-production.env
```

After server smoke passes, set the Vercel Production values from the web section
of `deploy/personal-server.env.example`, then create a new production deployment;
environment changes do not affect existing deployments. Use `/api` for
`NEXT_PUBLIC_API_URL`, the API HTTPS origin for `API_ORIGIN` and
`NEXT_PUBLIC_REALTIME_URL`, and the web HTTPS origin for `NEXT_PUBLIC_SITE_URL`.
Sign in once with Google and GitHub, verify refresh survives a page reload,
start a GitHub App installation claim, and upload/read one profile asset before
retiring the previous API origin.

## Rollback boundary

Each backup records the exact application image references used for rollback.
Confirm the referenced images remain available, then select them without rebuilding:

```bash
export API_IMAGE=ghcr.io/stacking-money-forever/jagalchi-api:<previous-reviewed-tag>
export AI_IMAGE=ghcr.io/stacking-money-forever/jagalchi-ai:<previous-reviewed-tag>
docker compose --env-file /etc/jagalchi/jagalchi-production.env -f compose.production.yml --profile cloudflare-tunnel up -d --no-build
```

If `.deploy-state/previous-release` or either image is missing, do not perform a
partial rollback. Use `rollback.env` from the latest verified backup or rebuild
the last known source revision. Keep Cloudflare and persistent services running
while only the API and AI images are rolled back.

The legacy `jagalchi-personal-api-postgres` volume is preserved and the
`api-db` service is isolated behind the `local-api-db` profile; the production
stack never starts it. Do not roll application code back across an incompatible
database migration. Docker volumes are not backups; restore from the verified
dump when a schema or persistent-data rollback is required.

## Backend continuous deployment

The backend uses an outbound pull controller instead of a GitHub self-hosted
runner. This repository is public, so PR-authored workflow changes must never
execute on the production VM. Every five minutes the VM resolves `main`, checks
the GitHub Actions API for a successful push-triggered `CI` run on that exact
SHA, creates an immutable release worktree, rechecks that `main` did not advance,
and invokes the same backup, migration, rollback, and smoke gates documented
above. Service image publication is separate: the production environment must be
updated to reviewed API and AI tags before an infra deployment. No GitHub, SSH,
or Tailscale secret is stored on the VM.

Install or refresh the controller from the synchronized operator checkout:

```bash
cd /srv/jagalchi-infra
sudo ./deploy/install-backend-cd.sh
sudo systemctl status jagalchi-backend-cd.timer
sudo journalctl -u jagalchi-backend-cd.service -n 100 --no-pager
```

The host needs `git`, `curl`, `python3`, `flock`, systemd, Docker Compose, a
`deploy` user in the Docker group, and outbound HTTPS. The installer verifies
the user and creates `/etc/jagalchi/backend-cd.env` with `CD_ENABLED=false`.
Keep it disabled until the production env passes preflight and the intended
source changes are committed and pushed. Exercise the full GitHub decision and
release-staging path without deployment:

```bash
sudo -u deploy /usr/local/libexec/jagalchi-backend-cd/cd-poll.sh --dry-run
```

Dry-run may fetch the public mirror and create an immutable release worktree;
it does not run preflight, build images, mutate databases or containers, or
write deployed/failed state. Confirm production readiness separately:

```bash
cd /srv/jagalchi-platform
./deploy/preflight.sh /etc/jagalchi/jagalchi-production.env
```

Edit `/etc/jagalchi/backend-cd.env` as root and set `CD_ENABLED=true`; the next
oneshot reads the file directly, so daemon reload is unnecessary. CI approval
matches both the workflow name `CI` and `.github/workflows/ci.yml` path for the
exact push SHA. A failed deployment SHA is not
retried automatically; after correcting the external condition, run exactly one
operator retry:

```bash
sudo -u deploy /usr/local/libexec/jagalchi-backend-cd/cd-poll.sh --retry-failed
```

Retry only bypasses the failed-state guard when that same SHA is still `main`;
if `main` advanced, the newer commit must pass its own CI. GitHub/API/network
errors fail the service without recording a deployment failure and are retried
by the next timer tick.

State lives under `/srv/jagalchi-cd/state`, immutable sources under
`/srv/jagalchi-cd/releases`, and the last successful source is linked from
`/srv/jagalchi-cd/current`. The timer serializes deployments with `flock`; a new
commit is never deployed until its own CI succeeds.
The controller retains the five newest release worktrees plus the currently
deployed worktree; change `CD_RELEASE_RETENTION` in the controller config if the
host's disk budget requires a different bound.
