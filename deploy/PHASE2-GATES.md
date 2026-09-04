# Phase 2 local-acceptance gate audit and design

Scope: `deploy/local-acceptance.sh` + `deploy/local_acceptance.py` coverage against the SSOT
`docs/product/NEXT_CYCLE.md` (Local acceptance checklist, "2.6 No-MSW browser gate").
Branch: `codex/phase2-infra`. This document is an audit + design record; no gate code changed here.

## 1. Current state: the 9 gates

`local_acceptance.py` `run()` executes four phases and records these `passedGates`
in the `.evidence/` receipt (`local_acceptance.py:280-290`):

| # | Gate | Covers (NEXT_CYCLE checklist) |
| --- | --- | --- |
| 1 | `environment` | Provider matrix is a locked Phase 1 mode; seed receipt schema; contract hashes (`validate_environment`, `contract_hashes`) |
| 2 | `seeded-resources` | Seed login returns the entitled seeded user through the normal auth boundary (`login_and_verify_seed`) |
| 3 | `target-import` | Fixture URL import completes via `WorkflowOperation`, resource href resolves |
| 4 | `profile-confirm` | GitHub fixture capture + profile confirmation |
| 5 | `diff-confirm` | Career Diff confirmation |
| 6 | `three-proposals` | Exactly three proposals |
| 7 | `valid-project-plan` | Schema-valid Project Run + Roadmap (tasks/map parity, plan schemaVersion 1) |
| 8 | `upload-lifecycle` | Presigned upload create, PUT, complete, read, delete against local MinIO |
| 9 | `worker-expired-lease-recovery` | Worker killed during `RUNNING`, lease-expiry reclaim, no duplicate result |

Compose-level preconditions (not receipt gates): `local-up.sh` waits on Postgres/AI/MinIO
health and migration completion; `local-smoke.sh` asserts `/api/health`, worker health-check,
Django `/ai/health/`.

## 2. Coverage matrix vs the Local acceptance checklist

NEXT_CYCLE.md lines 383-396, 12 items:

| Checklist item | Covered by | Verdict |
| --- | --- | --- |
| `/api/health/ready` reports DB, Django AI, MinIO, worker ready | compose `--wait` + smoke `/health` only | **Partial** — `/health/ready` (worker heartbeat, DB) is never asserted |
| Seed login returns authenticated, entitled user | gate 2 (+ entitlement enforced transitively by every mutation) | Covered |
| Fixture URL import through WorkflowOperation | gate 3 | Covered |
| GitHub capture, profile confirm, diff confirm, 3 proposals | gates 4-6 | Covered |
| Live DeepSeek compiles schema-valid plan | gate 7 (`local`/`local-real-source` modes) | Covered |
| **Map and Focus load through real Next → Nest with MSW disabled** | — | **MISSING** — acceptance is API-only (urllib); no web build, no browser, no UI assertion |
| **Fixture PR/CI facts drive task verification and a machine-verified Proof** | — | **MISSING** — no `start`/`verify`/`publish` call exists anywhere in `local_acceptance.py` |
| Presigned upload lifecycle | gate 8 | Covered |
| Restarting API during polling does not lose the operation | — | **MISSING** (gate 9 restarts the worker only) |
| Restarting the worker during `RUNNING` allows reclaim | gate 9 | Covered |
| Restarting the full backend retains Project Run, task state, Proof | — | **MISSING** |
| Logs contain no token/key/private source/model prompt | — | **MISSING** — no redaction scan exists in `deploy/` |

### 2.6 checklist specifically (NEXT_CYCLE.md 575-580)

- Loading/empty/blocked/error/stale-version/invalidated-Proof/narrow-web states: **not runnable
  today**; the platform `e2e-v1-local/` suite has only the Phase-1 entry spec.
- Project Run-linked graph is read-only: unenforced (platform spec concern).
- Built web against real Nest/worker/Postgres/Django/MinIO with fixture adapters + live DeepSeek:
  the stack exists (`compose.local.yml`), but no infra entrypoint builds the web and runs the
  browser suite.
- No MSW service worker + backend-generated resource IDs in UI: implemented *inside* the
  platform harness (`playwright.v1-local.config.ts` `serviceWorkers: 'block'`,
  `phase-one-entry.spec.ts` `expectNoServiceWorker`, seed-ID assertions), but infra never
  invokes it as an acceptance gate.

### 2.2 Audit findings (facts the gate design must respect)

1. **Seed run is a valid machine-verification success path (api `b3d32a3+`).**
   Pre-`b3d32a3`, `dev-seed` used human prose in `evidenceRequirements`, which parsed to zero
   rules and always failed with `VERIFICATION_RULE_UNSUPPORTED`. As of
   `b3d32a3ab1fec527f0ab8058838b3fbb2bba0365` (`fix(project-runs): extend Phase 2 query
   projection…`), the seeded task carries machine-parseable rules aligned with the fixture
   provider's default `success` facts:
   - seed rules: `['PR', 'CHANGED_PATH:src/core.ts', 'NAMED_CHECK:ci/test']`
     (`dev-seed.service.ts`, api `b3d32a3`);
   - fixture facts (`fixture-verification-provider.ts`): MERGED PR `17`, head `a…a`,
     `changedPaths: ['src/core.ts', 'src/core.spec.ts']`, `ci/test` → `SUCCESS`.
   **Consequence for G10:** the primary success leg should exercise
   `seed.projectRunId` (already created in gate 2) — no extra fixture-path run required
   for proof. The **fixture-path run** from `run_fixture_path()` remains valid as a
   secondary leg: it still proves plan-compiled evidence rules and the full
   target→proposal→plan→run vertical slice; keep it when G10 must certify both paths or
   when seed and plan-compiled rule shapes diverge in future.
2. The fixture verification chain is deterministic and aligned with the seed binding:
   fixture repository `9000001`, PR `17`, head `a…a` (`FIXTURE_VERIFICATION_IDS`) exactly
   match what `dev-seed` and the `PROJECT_RUN_CREATE` handler bind, and the fixture provider
   returns a MERGED PR with `ci/test` SUCCESS unless `FIXTURE_VERIFICATION_SCENARIO` is set
   (default `success`; local compose never sets it).
3. `local-smoke.sh` asserts `/api/health`, not `/api/health/ready`; the worker heartbeat /
   DB readiness contract (checklist item 1) is therefore only implicitly exercised.
4. `local_acceptance.py` records contract hashes in every receipt, so pin/contract history is
   auditable per run — the new gates should extend the same receipt, not introduce a second
   evidence format.

## 3. New gate design

All new gates append to the same receipt (`passedGates` + new fields); no second evidence
store. Numbering continues from 9.

### G10 `task-verification-proof` (API-level, in `local_acceptance.py`)

Goal: checklist item "Fixture PR/CI facts drive task verification and a machine-verified Proof",
plus the API half of the Major-2 exit gate.

**Pin prerequisite:** `revisions.api` must be at least `b3d32a3` (seed evidence rules fix).

**Primary run:** `seed.projectRunId` from gate 2 (fixture binding `9000001` / PR `17` / head
`a…a` already on the seeded run). **Optional secondary run:** the fixture-path run from
`run_fixture_path()` when the gate must also certify plan-compiled evidence emission.

Flow (all via the existing `HttpTransport`, each mutation with `If-Match` + fresh
`Idempotency-Key`):

1. `GET /project-runs/{seedProjectRunId}` → take `version`, task `seed-task-1` in `READY`.
2. `POST …/tasks/{taskKey}/start` → expect task `IN_PROGRESS`, run `ACTIVE`.
3. `POST …/tasks/{taskKey}/verify` → `202` with `operationId` (`project-runs.service.ts`
   enqueues `TASK_VERIFICATION`).
4. Poll `/workflow-operations/{operationId}` to `SUCCEEDED`
   (reuse `poll_operation`, `Retry-After` handling).
5. Assert on the fresh projection: task `DONE`, run `COMPLETED`,
   `proof.facts.snapshotId` is a backend UUID, `proof.facts.verificationLevel ==
   'MACHINE_VERIFIED'`, `proof.facts.headSha == run binding expectedHeadSha`,
   `publication.state == 'UNPUBLISHED'`.
6. `POST /project-runs/{id}/publish` → `publication.state == 'ACTIVE'`,
   `publication.publicId` non-null, `verification.state == 'PASS'`.
7. `POST /project-runs/{id}/reverify` → poll → new `proof.facts.snapshotId` differs from 5
   (immutability: supersede, never overwrite).
8. **Failure leg (replaces pre-`b3d32a3` seed `VERIFICATION_RULE_UNSUPPORTED` trick):**
   the seeded run now passes under default `FIXTURE_VERIFICATION_SCENARIO=success`, so a
   negative verification verdict is **not** available on the same stack without either
   (a) recycling the API/worker with `FIXTURE_VERIFICATION_SCENARIO=failure` for a
   dedicated sub-leg, or (b) asserting failure in api unit tests
   (`task-verification.handler.spec.ts`). G10 acceptance may omit the negative leg and
   defer the 2.6 "failed verification" UI state to platform E2E once exemplar lands.

Receipt additions: `proofSnapshotId`, `publicationState`, `reverifiedSnapshotId`,
`proofRunId` (seed vs fixture-path). No `failureLegCode` unless a negative sub-leg is
added later.

### G11 `no-msw-browser` (runner gate, new `deploy/local-browser-gate.sh`)

Goal: the 2.6 browser checklist, executed by infra **as a runner**, keeping spec ownership in
platform (canonical ownership: platform owns browser E2E). infra never forks its own Playwright
suite.

Contract with the platform harness (already implemented, Phase-1 entry):

- `scripts/test-v1-local-e2e.sh` builds the production web (MSW flags hard-false), starts it on
  `:3100`, runs the `chromium-no-msw` project with `serviceWorkers: 'block'`, and the specs
  assert no service-worker registration plus backend-generated resource IDs in UI **and** via
  the same-origin proxy (seed receipt ↔ login response ↔ `/project-runs/{id}` ↔ heading).
- infra runner obligations: run `local-doctor.sh`, `local-up.sh`, `local-seed.sh`, then invoke
  the platform script with the two-argument interface it already defines
  (`$JAGALCHI_INFRA_DIR`, env file); verify `PLATFORM_SOURCE_DIR` in the env file resolves to
  the same platform checkout (the harness already dies otherwise); assert the E2E process
  exits 0; append `no-msw-browser` to the receipt with the platform HEAD the specs ran
  against (must equal `revisions.platform` when `JAGALCHI_DEV_HEAD` is unset).

Phase-2 extensions (owned by FE, consumed by this gate):

- Spec inventory contract: the `e2e-v1-local/` directory must contain specs covering the seven
  2.6 states + read-only graph + Map + Focus + Proof. The runner enforces a minimal inventory
  check (fail-closed if the count of spec files drops below the agreed manifest exported by
  platform, e.g. `apps/web/e2e-v1-local/manifest.json` listing state → spec file). This keeps
  "2.6 checklist passes" machine-checkable instead of trust-based.
- The browser gate runs **before G10** in the same stack lifetime so the seeded run is still
  `READY` (Map/Focus on a pristine seed run; proof mutations happen after browser specs).
  Export `JAGALCHI_E2E_SEED_RUN_ID` for entry/Map/Focus specs. For the "failed
  verification" 2.6 state, export a separate id once a failure fixture exists (not the seed
  run post-`b3d32a3`); options are `FIXTURE_VERIFICATION_SCENARIO=failure` on a recycled
  stack or a platform-owned setup step — do not assume `seed.projectRunId` is a failure run.

### G12 `health-ready` (cheap, fold into smoke)

`local-smoke.sh` gains one curl: `GET /api/health/ready` expecting `200 {"status":"ready"}`;
together with the existing Django + worker checks this discharges checklist item 1 exactly
as written (DB, Django AI, MinIO via compose wait conditions, worker heartbeat via ready).

### G13 `restart-retention` (API-level, after G10)

Checklist items "Restarting API during polling" and "Restarting the full backend retains…":

1. After G10 publishes: `docker compose stop api workflow-worker` → `up -d` → poll
   `/api/health/ready` → assert run still `COMPLETED`, task states intact, publication
   `ACTIVE` with the same `proofSnapshotId`.
2. Mid-poll restart leg: enqueue a target-import, `stop api` during the poll loop
   (reuse the gate-9 pattern: bounded sleep, then `up -d`), assert the same operation id
   still reaches `SUCCEEDED` and no duplicate resource id appears in `resource_ids`.

### Explicitly out of scope for this slice

- **Log redaction scan** (checklist item 12): needs cross-service redaction rule ownership
  (API/AI/web). infra can host the scanner entrypoint (`deploy/local-log-audit.sh`, runs after
  a run marker, reports rule names only) but the rule set must come from a contract with
  api/ai/platform. Design when those land; do not half-implement now.
- Real GitHub / real job source / staging evidence: Major 3/4 gates (`local-real` matrix exists
  in the lock already).

## 4. Sequencing

1. Platform lands Map/Focus/Proof exemplar + the 2.6 spec inventory on `codex/phase2-platform`
   (producer first; **exemplar user-approval gate blocks G11 implementation**).
2. Advance `revisions.api` to `b3d32a3+` before G10 implementation (seed evidence fix).
3. infra implements G10 + G13 (pure `local_acceptance.py`, testable against `ci` mode without
   DeepSeek) and the G11 runner + manifest check in the same change set; unit tests in
   `deploy/tests/test_local_acceptance.py` follow the existing fake-transport pattern.
4. Receipt version bumps to 2 (`receiptVersion: 2`, adds the fields above) — consumers are
   only humans/CI, no compat shim needed.
5. Gate code commits land after step 1; this document is the Phase 2 runner design SSOT.
   Push remains forbidden until orchestrator authorizes.
