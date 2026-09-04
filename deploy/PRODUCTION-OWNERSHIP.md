# Production ownership contract

`jagalchi-infra` is the canonical owner of the personal-server VM stack that
runs the reviewed Nest API image, workflow worker, Django AI image, AI
PostgreSQL, MinIO, and the outbound Cloudflare Tunnel connector.

## Repository boundaries

| Concern | Owner | Notes |
| --- | --- | --- |
| VM compose + deploy automation | `jagalchi-infra` | `compose.production.yml`, `deploy/*.sh`, systemd CD |
| API application source + image build | `jagalchi-api` | Publish reviewed tags to GHCR |
| AI application source + image build | `jagalchi-ai` | Publish reviewed tags to GHCR |
| Web production deploy | `jagalchi-platform` (`apps/web`) | Vercel only; not started by VM compose |
| Local acceptance stack | `jagalchi-infra` | `compose.local.yml`, `local-*.sh`, gates |
| No-MSW browser harness | `jagalchi-platform` | `e2e-v1-local/`, Playwright config |

## Image contract

Production never builds application source on the VM.

- `API_IMAGE` and `AI_IMAGE` are required in `/etc/jagalchi/jagalchi-production.env`.
- Tags must match `ghcr.io/stacking-money-forever/jagalchi-api:<tag>` and
  `ghcr.io/stacking-money-forever/jagalchi-ai:<tag>`.
- `deploy/preflight.sh` rejects placeholder or non-GHCR values.
- `deploy/deploy.sh` pulls `api`, `api-migrate`, and `ai`, then starts the
  `cloudflare-tunnel` profile without local builds.

Service repositories own image publication and their own CI. Infra owns the
reviewed combination on the VM through the server env file and deploy scripts.

## Operator surfaces

| Task | Command |
| --- | --- |
| Static contract verification | `bash -n deploy/*.sh` and `python3 -m unittest discover -s deploy/tests -p 'test_*.py'` |
| Compose render check | `docker compose --env-file deploy/personal-server.env.example -f compose.production.yml --profile cloudflare-tunnel config --quiet` |
| Preflight | `./deploy/preflight.sh /etc/jagalchi/jagalchi-production.env` |
| Deploy | `./deploy/deploy.sh /etc/jagalchi/jagalchi-production.env` |
| Smoke | `./deploy/smoke.sh /etc/jagalchi/jagalchi-production.env` |

## Backend CD

The outbound CD controller deploys `stacking-money-forever/jagalchi-infra`
after a successful push-triggered `CI` run on the exact `main` SHA. Image tag
updates remain an explicit operator action in the production env file; CD does
not publish API or AI images.

## Web release boundary

`deploy/personal-server.env.example` documents Vercel-only `NEXT_PUBLIC_*`
and `API_ORIGIN` values. The VM stack does not consume those keys. After VM
smoke passes, copy the web section into Vercel Production and create a new
deployment.
