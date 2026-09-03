# Jagalchi Infrastructure

Cross-service production deployment for Jagalchi.

- NestJS product API: [`stacking-money-forever/jagalchi-api`](https://github.com/stacking-money-forever/jagalchi-api)
- Django AI runtime: [`stacking-money-forever/jagalchi-ai`](https://github.com/stacking-money-forever/jagalchi-ai)
- Detailed operator runbook: [`deploy/README.md`](./deploy/README.md)

This repository owns the reviewed combination of service images, ingress, PostgreSQL connectivity, object storage, backup, restore, smoke, rollback, and deployment automation. It does not build application source. `API_IMAGE` and `AI_IMAGE` must pin reviewed GHCR tags in the server-owned production environment file.

No credential or production environment file belongs in Git. Copy `deploy/personal-server.env.example` outside the repository and replace every placeholder through the server's secret-management path.

## Static verification

```sh
bash -n deploy/*.sh
python3 -m unittest discover -s deploy/tests -p 'test_*.py'
docker compose --env-file deploy/personal-server.env.example -f compose.production.yml --profile cloudflare-tunnel config --quiet
```

## Local cross-service stack

The local stack builds only the standalone API and AI checkouts and runs the
platform web app on the host. Copied sources under `jagalchi-platform/services`
are rejected.

```sh
install -m 600 deploy/local.env.example deploy/local.env
# Set absolute PLATFORM_SOURCE_DIR, API_SOURCE_DIR, and AI_SOURCE_DIR paths.
# The default local mode also requires a local DeepSeek key.
./deploy/local-bootstrap.sh deploy/local.env
./deploy/local-up.sh deploy/local.env
./deploy/local-seed.sh deploy/local.env
./deploy/local-smoke.sh deploy/local.env
./deploy/local-acceptance.sh deploy/local.env
./deploy/local-down.sh deploy/local.env
```

Local ports bind to loopback. Django AI remains reachable only inside the Compose network. Resetting data is intentionally separate and requires the exact guard:

`JAGALCHI_LOCAL_MODE` is fail-closed. `local` uses deterministic job
and GitHub facts with live DeepSeek; `ci` requires fixture/fake providers
and disables external/LLM access; `ci-real-source` proves a real allowlisted
job URL with fake AI before credentials are introduced; `local-real-source` uses a real job source
with fixture GitHub and live DeepSeek for the Phase 1 gate; `local-real` adds
real GitHub for Phase 3. The doctor rejects mixed matrices, non-official DeepSeek origins,
or model drift from `deepseek-v4-flash` extraction and `deepseek-v4-pro`
planning.

```sh
./deploy/local-reset.sh deploy/local.env --confirm=jagalchi-v1-local
```

Run `./deploy/local-acceptance.sh deploy/local.env --reset` only when an empty-volume
acceptance run is explicitly intended. It delegates the same exact reset guard,
then exercises the seeded deterministic source path, upload lifecycle, and worker
lease recovery without printing credentials, tokens, or response payloads.
On full success it atomically writes a mode-600 JSON receipt under ignored
`.evidence/`. The receipt contains only timestamps, the exact provider mode,
contract hashes, reset status, and passed gate names. `ci-real-source` is recorded
as real-source plus fake AI; only the exact `local-real-source` DeepSeek matrix can
claim live AI. Neither receipt contains resource IDs, credentials, or payloads.

The outbound CD controller remains disabled by default. Enabling it requires reviewed service image publication and an explicit image-version update policy.
