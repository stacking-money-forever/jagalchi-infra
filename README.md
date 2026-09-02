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

The outbound CD controller remains disabled by default. Enabling it requires reviewed service image publication and an explicit image-version update policy.
