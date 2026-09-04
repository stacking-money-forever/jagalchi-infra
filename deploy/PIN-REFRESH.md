# Platform pin (revisions) refresh procedure

Audience: infra owner updating `deploy/local-stack.lock.json` `revisions.*` when a consumer
branch (e.g. `codex/phase2-platform`) advances the reviewed combination the local stack runs.

## Where the pins live and who reads them

`revisions` (40-hex SHAs for `platform`, `api`, `ai`, `infra`):

- `deploy/validate-local-lock.py` (`main()` 99-113): every `local-doctor.sh` run compares each
  checkout's `git rev-parse HEAD` to the pin. Mismatch fails with the escape hatch
  `JAGALCHI_DEV_HEAD=true` (env opt-in only; never set it in `local.env`).
- `deploy/local-bootstrap.sh` (`clone_if_missing`, line 40): clones missing checkouts and
  `git checkout --detach` the pinned revision.
- The pin is the only "which commit is reviewed for local runs" record; `apiContractSha256`
  is pinned separately by content hash, not by commit.

`infra` self-pin: HEAD of the infra checkout itself. Every commit that changes
`local-stack.lock.json` or gate code makes the previous self-pin stale, so pin refreshes come
in a fixed order (below).

## Procedure: advancing one pin (e.g. platform → new codex/phase2-platform HEAD)

Principle: the pin names the exact commit a human reviewed and accepted as the
new reviewed combination. Advance the pin to a SHA you have verified locally, never to a
remote branch name — `HEAD` values move, pins must not.

1. **Freeze the candidate SHA.**
   `git -C <platform-checkout> rev-parse HEAD`. The checkout must be exactly the commit you
   intend to pin: `git status --porcelain` empty (or the dirty files are known, accepted
   platform-side work that is NOT part of what you pin). A pin with a dirty tree is a lie:
   doctor only reads HEAD, it cannot see uncommitted UI changes the browser gate will load.

2. **Verify contracts before pinning.** Producer-first rule still applies:
   - `platform/packages/api-client/contract/openapi.json` sha256 must equal
     `apiContractSha256` (which itself must equal
     `api/contracts/openapi.json`) — `validate-local-lock.py` fails otherwise, and
     `local_acceptance.py` `contract_hashes()` re-checks it in every acceptance receipt.
   - The platform commit must not require a newer API than the currently pinned
     `revisions.api`. If it does: advance the API pin first (its own reviewed commit), then
     platform. Never advance a consumer pin onto a contract its producer pin cannot serve.

3. **Run the stack against the candidate before committing the pin.**
   ```sh
   # in an env file pointing at the candidate checkouts
   ./deploy/local-doctor.sh deploy/local.env        # will FAIL on the old pin — expected
   JAGALCHI_DEV_HEAD=true ./deploy/local-doctor.sh deploy/local.env   # bypass, diagnose
   JAGALCHI_DEV_HEAD=true ./deploy/local-up.sh deploy/local.env
   JAGALCHI_DEV_HEAD=true ./deploy/local-seed.sh deploy/local.env
   # run whatever gate the platform change claims to satisfy (e.g. no-MSW browser gate)
   JAGALCHI_DEV_HEAD=true ./deploy/local-acceptance.sh deploy/local.env
   ```
   `JAGALCHI_DEV_HEAD=true` is the explicit "I am verifying a not-yet-pinned head" mode.
   Acceptance must pass in exactly the mode(s) the pin will be consumed in (`ci` mode for
   contract-only changes; `local` for anything touching the DeepSeek path).

4. **Commit the pin.** One commit per semantic pin advance, message
   `chore(local): advance <repo> pin to <short-reason>` (precedent: 697c805, a47c991):
   edit only the one `revisions` field in `deploy/local-stack.lock.json`.
   If `apiContractSha256` changed, that update rides the API pin commit, never a platform-only
   commit (the lock must stay internally consistent at every commit).

5. **Advance the infra self-pin in the same push-order (second commit if needed).**
   The commit that edits `local-stack.lock.json` changes infra's own HEAD, so the `infra`
   pin goes stale by one commit by construction. Following the established Phase-1 pattern
   (24e2d99 → b7e2d61, 43c1d76 → a47c991): land the content commit(s), then a follow-up
   `chore(local): advance infra self-pin` commit updating `revisions.infra` to the new HEAD.
   This is why the self-pin always trails: pinning your own HEAD inside the commit that IS
   that HEAD is impossible. Unpinned-in-between commits are covered by
   `JAGALCHI_DEV_HEAD=true` during local verification; CI (`.github/workflows/ci.yml`) does
   not run the doctor, so nothing blocks on the one-commit lag.

6. **Post-commit sanity.** `python3 deploy/validate-local-lock.py --lock deploy/local-stack.lock.json
   --repo-root . --platform-source <p> --api-source <a> --ai-source <i>` against checkouts at
   the new pins must print `local stack lock: OK` with no `JAGALCHI_DEV_HEAD` — this is the
   whole point of the pin (the next operator's clean bootstrap needs no env override).
   Then `python3 -m unittest discover -s deploy/tests -p 'test_*.py'`.

## Relationship to validate-local-lock.py

- The validator is the *enforcement* half; this procedure is the *authorization* half.
  It has no notion of branches or "latest" — only exact equality between `revisions.<key>`
  and the checkout's `git rev-parse HEAD`, plus `JAGALCHI_DEV_HEAD=true` as a single,
  loud, per-invocation bypass.
- Every gate entrypoint (`local-doctor.sh` → `local-up.sh` → `local-seed.sh` →
  `local-acceptance.sh`, and the platform `test-v1-local-e2e.sh` via `local-doctor.sh`)
  runs the doctor first, so an unpinned consumer checkout fails fast with a message that
  names the expected and found SHAs.
- The escape hatch is intentionally per-process (`os.environ`), not a file flag: a stale
  `local.env` cannot silently normalize running off-head.

## Anti-patterns

- Pinning a branch name, a tag you might move, or `main`'s head "while you're there".
- Advancing a pin from a dirty checkout (doctor can't see the dirt; the browser gate will).
- Mixing pin advance + gate code change in one commit: gate changes alter what "verified"
  means; each pin commit must certify behavior under the gates as they were at that commit.
- Bumping `apiContractSha256` without regenerating the platform consumer copy
  (`packages/api-client/contract/openapi.json`) — the acceptance harness cross-checks both
  hashes and will fail, correctly.
