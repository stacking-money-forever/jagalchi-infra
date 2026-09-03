#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
FIXTURE_JOB_URL = "https://fixture.invalid/jobs/software-engineer"
API_BASE_URL = "http://127.0.0.1:8080/api"


class AcceptanceError(RuntimeError):
    pass


@dataclass
class HttpResponse:
    status: int
    body: Any = None
    headers: dict[str, str] | None = None
    raw: bytes = b""


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        target: str,
        *,
        body: Any = None,
        headers: dict[str, str] | None = None,
        expected: tuple[int, ...] = (200,),
        follow_redirects: bool = True,
    ) -> HttpResponse: ...


class CommandTransport(Protocol):
    def run(self, command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]: ...


class UrllibTransport:
    def __init__(self, base_url: str = API_BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self.access_token: str | None = None

    def request(
        self,
        method: str,
        target: str,
        *,
        body: Any = None,
        headers: dict[str, str] | None = None,
        expected: tuple[int, ...] = (200,),
        follow_redirects: bool = True,
    ) -> HttpResponse:
        url = target if target.startswith(("http://", "https://")) else f"{self.base_url}/{target.lstrip('/')}"
        request_headers = dict(headers or {})
        if self.access_token and url.startswith(self.base_url):
            request_headers["Authorization"] = f"Bearer {self.access_token}"
        data: bytes | None
        if body is None:
            data = None
        elif isinstance(body, bytes):
            data = body
        else:
            data = json.dumps(body, separators=(",", ":")).encode()
            request_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        opener = urllib.request.build_opener() if follow_redirects else urllib.request.build_opener(NoRedirectHandler())
        try:
            response = opener.open(request, timeout=15)
            status = response.status
            raw = response.read()
            response_headers = {key.lower(): value for key, value in response.headers.items()}
        except urllib.error.HTTPError as error:
            status = error.code
            raw = error.read()
            response_headers = {key.lower(): value for key, value in error.headers.items()}
        except OSError as error:
            raise AcceptanceError(f"{method} {safe_target(target)} did not reach the local service") from error
        if status not in expected:
            code = ""
            try:
                value = json.loads(raw)
                code = f" code={value.get('code', '')}" if isinstance(value, dict) else ""
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
            raise AcceptanceError(f"{method} {safe_target(target)} returned {status}{code}")
        parsed: Any = None
        if raw and response_headers.get("content-type", "").split(";", 1)[0] == "application/json":
            parsed = json.loads(raw)
        return HttpResponse(status=status, body=parsed, headers=response_headers, raw=raw)


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class SubprocessTransport:
    def run(self, command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(command, check=check, capture_output=True, text=True, timeout=30)


def safe_target(target: str) -> str:
    if target.startswith(API_BASE_URL):
        return target.removeprefix(API_BASE_URL) or "/"
    if target.startswith(("http://", "https://")):
        return "external-storage"
    return target


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def require_uuid(value: Any, label: str) -> str:
    if not isinstance(value, str) or not UUID_RE.fullmatch(value):
        raise AcceptanceError(f"{label} is not a backend UUID")
    return value


def is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise AcceptanceError("required contract artifact is missing") from error


class LocalAcceptance:
    def __init__(
        self,
        http: HttpTransport,
        commands: CommandTransport,
        env: dict[str, str],
        seed_receipt: dict[str, Any],
        compose: list[str],
        api_source: Path,
        *,
        repo_root: Path | None = None,
        reset_performed: bool = False,
        monotonic=time.monotonic,
        sleep=time.sleep,
    ) -> None:
        self.http = http
        self.commands = commands
        self.env = env
        self.seed = seed_receipt
        self.compose = compose
        self.api_source = api_source
        self.repo_root = repo_root
        self.reset_performed = reset_performed
        self.monotonic = monotonic
        self.sleep = sleep
        self.namespace = uuid.uuid4().hex[:12]
        self.started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.client_ids: set[str] = set()
        self.resource_ids: set[str] = set()

    def run(self) -> None:
        self.validate_environment()
        self.login_and_verify_seed()
        self.run_fixture_path()
        self.run_upload_lifecycle()
        self.run_worker_recovery()
        self.write_receipt()
        print(f"local acceptance: OK mode={self.env['JAGALCHI_LOCAL_MODE']} namespace={self.namespace}")

    def validate_environment(self) -> None:
        required = [
            "LOCAL_SEED_EMAIL", "LOCAL_SEED_PASSWORD", "PLATFORM_SOURCE_DIR",
            "API_SOURCE_DIR", "AI_SOURCE_DIR",
        ]
        if any(not self.env.get(key) for key in required):
            raise AcceptanceError("local acceptance environment is incomplete")
        mode = self.env.get("JAGALCHI_LOCAL_MODE")
        allowed = {
            "ci": ("fixture", "fixture", "fixture", "fake", "true", "true"),
            "ci-real-source": ("live", "fixture", "fixture", "fake", "true", "true"),
            "local": ("fixture", "fixture", "deepseek", "deepseek", "false", "false"),
            "local-real-source": ("live", "fixture", "deepseek", "deepseek", "false", "false"),
        }
        expected = allowed.get(mode or "")
        actual = tuple(self.env.get(key) for key in (
            "JOB_SOURCE_PROVIDER", "GITHUB_PROVIDER", "AI_PROVIDER", "AI_V1_PROVIDER",
            "AI_DISABLE_EXTERNAL", "AI_DISABLE_LLM",
        ))
        if expected is None or actual != expected:
            raise AcceptanceError("local acceptance requires a locked Phase 1 provider mode")
        if mode in {"ci-real-source", "local-real-source"} and not self.env.get("REAL_JOB_SOURCE_URL"):
            raise AcceptanceError("REAL_JOB_SOURCE_URL is required for real-source acceptance")
        for key in ("userId", "projectRunId", "roadmapId"):
            require_uuid(self.seed.get(key), f"seed {key}")
        if self.seed.get("schemaVersion") != 1:
            raise AcceptanceError("seed schemaVersion must be 1")
        if self.repo_root is not None:
            self.contract_hashes()

    def login_and_verify_seed(self) -> None:
        response = self.http.request(
            "POST",
            "/users/auth/login",
            body={"email": self.env["LOCAL_SEED_EMAIL"], "password": self.env["LOCAL_SEED_PASSWORD"]},
            expected=(200,),
        )
        token = object_path(response.body, "accessToken")
        user_id = require_uuid(object_path(response.body, "user", "id"), "login userId")
        if user_id != self.seed["userId"] or not isinstance(token, str) or not token:
            raise AcceptanceError("login identity does not match the seed receipt")
        if isinstance(self.http, UrllibTransport):
            self.http.access_token = token
        self.assert_resource("PROJECT_RUN", self.seed["projectRunId"], "/project-runs")
        self.assert_resource("ROADMAP", self.seed["roadmapId"], "/roadmaps")

    def job_source_url(self) -> str:
        if self.env["JAGALCHI_LOCAL_MODE"] in {"ci-real-source", "local-real-source"}:
            return self.env["REAL_JOB_SOURCE_URL"]
        return FIXTURE_JOB_URL

    def write_receipt(self) -> Path:
        if self.repo_root is None:
            raise AcceptanceError("acceptance receipt requires the infra repository root")
        completed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        mode = self.env["JAGALCHI_LOCAL_MODE"]
        matrix = {
            "jobSource": self.env["JOB_SOURCE_PROVIDER"],
            "github": self.env["GITHUB_PROVIDER"],
            "apiAi": self.env["AI_PROVIDER"],
            "aiRuntime": self.env["AI_V1_PROVIDER"],
            "externalDisabled": self.env["AI_DISABLE_EXTERNAL"] == "true",
            "llmDisabled": self.env["AI_DISABLE_LLM"] == "true",
        }
        live_deepseek = (
            matrix["apiAi"] == "deepseek"
            and matrix["aiRuntime"] == "deepseek"
            and not matrix["externalDisabled"]
            and not matrix["llmDisabled"]
        )
        receipt = {
            "receiptVersion": 1,
            "mode": mode,
            "startedAt": self.started_at,
            "completedAt": completed_at,
            "resetPerformed": self.reset_performed,
            "providerEvidence": matrix,
            "claims": {
                "realJobSource": matrix["jobSource"] == "live",
                "fixtureGithub": matrix["github"] == "fixture",
                "liveDeepSeek": live_deepseek,
                "fakeAi": matrix["apiAi"] == "fixture" and matrix["aiRuntime"] == "fake",
            },
            "contractHashes": self.contract_hashes(),
            "passedGates": [
                "environment",
                "seeded-resources",
                "target-import",
                "profile-confirm",
                "diff-confirm",
                "three-proposals",
                "valid-project-plan",
                "upload-lifecycle",
                "worker-expired-lease-recovery",
            ],
        }
        evidence_dir = self.repo_root / ".evidence"
        evidence_dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(evidence_dir, 0o700)
        suffix = time.time_ns() % 1_000_000_000
        filename = f"local-acceptance-{mode}-{completed_at.replace(':', '').replace('-', '')}-{suffix:09d}.json"
        destination = evidence_dir / filename
        descriptor, temporary_name = tempfile.mkstemp(prefix=".receipt-", dir=evidence_dir)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(receipt, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, destination)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
        return destination

    def contract_hashes(self) -> dict[str, str]:
        if self.repo_root is None:
            raise AcceptanceError("contract hash verification requires the infra repository root")
        platform_source = Path(self.env.get("PLATFORM_SOURCE_DIR", ""))
        ai_source = Path(self.env.get("AI_SOURCE_DIR", ""))
        api_openapi = self.api_source / "contracts/openapi.json"
        platform_openapi = platform_source / "packages/api-client/contract/openapi.json"
        lock = json.loads((self.repo_root / "deploy/local-stack.lock.json").read_text())
        actual_openapi = file_sha256(api_openapi)
        if actual_openapi != lock.get("apiContractSha256") or file_sha256(platform_openapi) != actual_openapi:
            raise AcceptanceError("OpenAPI contract hashes are not synchronized")

        api_manifest_path = self.api_source / "contracts/ai/v1/manifest.json"
        ai_manifest_path = ai_source / "contracts/ai/v1-generated/manifest.json"
        api_manifest = json.loads(api_manifest_path.read_text())
        ai_manifest = json.loads(ai_manifest_path.read_text())
        producer_files = api_manifest.get("files")
        consumer_files = ai_manifest.get("files")
        if not isinstance(producer_files, dict) or producer_files != consumer_files:
            raise AcceptanceError("API and AI contract manifests are not synchronized")
        for filename, expected_hash in producer_files.items():
            if (
                not isinstance(filename, str)
                or not is_sha256(expected_hash)
                or file_sha256(self.api_source / "contracts/ai/v1" / filename) != expected_hash
                or file_sha256(ai_source / "contracts/ai/v1-generated" / filename) != expected_hash
            ):
                raise AcceptanceError("API and AI contract files are not synchronized")
        api_bundle = api_manifest.get("bundleSha256")
        ai_aggregate = ai_manifest.get("aggregateSha256")
        if not is_sha256(api_bundle) or not is_sha256(ai_aggregate):
            raise AcceptanceError("contract aggregate hashes are malformed")
        return {
            "apiOpenApiSha256": actual_openapi,
            "apiAiBundleSha256": api_bundle,
            "aiSnapshotAggregateSha256": ai_aggregate,
        }

    def run_fixture_path(self) -> None:
        target_version_id = self.create_and_poll(
            "/career/target-imports",
            {"input": {"kind": "FETCHED_URL", "url": self.job_source_url()}},
            "CAREER_TARGET_VERSION",
        )
        target = self.http.request("GET", f"/career/target-versions/{target_version_id}").body
        target_id = require_uuid(object_path(target, "careerTargetId"), "career targetId")

        profile_draft_id = self.create_and_poll(
            "/career/profile-snapshot-operations/github",
            {"repositoryIds": []},
            "CANDIDATE_PROFILE_SNAPSHOT",
        )
        profile_draft = self.http.request("GET", f"/career/profile-snapshots/{profile_draft_id}").body
        repositories = object_path(profile_draft, "payload", "repositories")
        if not isinstance(repositories, list) or not repositories:
            raise AcceptanceError("fixture profile contains no backend repository facts")
        github_repository_id = object_path(repositories[0], "githubRepositoryId")
        if not isinstance(github_repository_id, str) or not github_repository_id:
            raise AcceptanceError("fixture repository id is missing")

        profile_id = self.post_resource(
            f"/career/profile-snapshots/{profile_draft_id}/confirm",
            {"acceptedRepositoryIds": []},
            "confirmed profile",
        )
        diff_draft = self.http.request(
            "POST",
            f"/career/targets/{target_id}/diff-snapshots",
            body={"careerTargetVersionId": target_version_id, "candidateProfileSnapshotId": profile_id},
            headers=self.idempotency_headers(),
            expected=(201,),
        ).body
        diff_draft_id = self.record_resource(object_path(diff_draft, "id"), "diff draft")
        diff_id = self.post_resource(
            f"/career/diff-snapshots/{diff_draft_id}/confirm",
            {"acceptedCompetencyIds": []},
            "confirmed diff",
        )

        proposal_set_id = self.create_and_poll(
            f"/career/targets/{target_id}/project-proposal-operations",
            {"careerDiffSnapshotId": diff_id, "constraints": {"availableHours": 20, "preferredStack": ["typescript"], "allowedRepositoryModes": ["EXISTING_OWNED"]}},
            "PROJECT_PROPOSAL_SET",
        )
        proposal_set = self.http.request("GET", f"/career/project-proposal-sets/{proposal_set_id}").body
        proposals = object_path(proposal_set, "proposals")
        if not isinstance(proposals, list) or len(proposals) != 3:
            raise AcceptanceError("proposal set must contain exactly three proposals")
        proposal_ids = [self.record_resource(object_path(item, "id"), "proposal") for item in proposals]

        project_run_id = self.create_and_poll(
            "/project-run-operations",
            {
                "projectProposalId": proposal_ids[0],
                "candidateProfileSnapshotId": profile_id,
                "careerDiffSnapshotId": diff_id,
                "repository": {"mode": "EXISTING_OWNED", "githubRepositoryId": github_repository_id},
                "constraints": {"availableHours": 20},
            },
            "PROJECT_RUN",
        )
        project_run = self.http.request("GET", f"/project-runs/{project_run_id}").body
        if object_path(project_run, "plan", "schemaVersion") != 1:
            raise AcceptanceError("Project Run plan schemaVersion is invalid")
        tasks = object_path(project_run, "tasks")
        nodes = object_path(project_run, "map", "nodes")
        if not isinstance(tasks, list) or not tasks or not isinstance(nodes, list):
            raise AcceptanceError("Project Run plan is empty")
        task_ids = {object_path(task, "id") for task in tasks}
        if task_ids != {object_path(node, "id") for node in nodes}:
            raise AcceptanceError("Project Run task and map projections differ")

    def run_upload_lifecycle(self) -> None:
        content = f"jagalchi-local-acceptance:{self.namespace}".encode()
        approval = self.http.request(
            "POST",
            "/uploads",
            body={
                "purpose": "ROADMAP_ATTACHMENT",
                "roadmapId": self.seed["roadmapId"],
                "fileName": f"acceptance-{self.namespace}.txt",
                "contentType": "text/plain",
                "size": len(content),
            },
            expected=(201,),
        ).body
        upload_id = self.record_resource(object_path(approval, "id"), "upload")
        upload_url = object_path(approval, "uploadUrl")
        upload_headers = object_path(approval, "headers")
        if not isinstance(upload_url, str) or not upload_url.startswith("http://127.0.0.1:9000/"):
            raise AcceptanceError("presigned upload URL is not the locked browser origin")
        if not isinstance(upload_headers, dict):
            raise AcceptanceError("presigned upload headers are missing")
        self.http.request("PUT", upload_url, body=content, headers=upload_headers, expected=(200,))
        completed = self.http.request("POST", f"/uploads/{upload_id}/complete", body={}, expected=(201,)).body
        if object_path(completed, "id") != upload_id or object_path(completed, "status") != "READY":
            raise AcceptanceError("upload completion did not return the approved ready resource")
        content_response = self.http.request(
            "GET", f"/uploads/{upload_id}/content", expected=(302,), follow_redirects=False
        )
        location = (content_response.headers or {}).get("location")
        if not location or not location.startswith("http://127.0.0.1:9000/"):
            raise AcceptanceError("content route did not return a locked storage redirect")
        downloaded = self.http.request("GET", location, expected=(200,))
        if downloaded.raw != content:
            raise AcceptanceError("downloaded upload differs from the accepted bytes")
        self.http.request("DELETE", f"/uploads/{upload_id}", expected=(204,))

    def run_worker_recovery(self) -> None:
        worker_source = self.api_source / "src/workflow-operations/workflow-operation.worker.ts"
        environment_source = self.api_source / "src/shared/config/environment.ts"
        if not worker_source.is_file() or "WORKFLOW_HOLD_AFTER_CLAIM_MS" not in worker_source.read_text():
            raise AcceptanceError("worker recovery requires WORKFLOW_HOLD_AFTER_CLAIM_MS")
        if not environment_source.is_file() or "not allowed in production" not in environment_source.read_text():
            raise AcceptanceError("worker recovery hold hook lacks a production guard")

        name = f"jagalchi-v1-acceptance-held-{self.namespace}"
        operation_id: str | None = None
        self.commands.run([*self.compose, "stop", "workflow-worker"])
        try:
            self.commands.run(
                [
                    *self.compose,
                    "run",
                    "-d",
                    "--no-deps",
                    "--name",
                    name,
                    "-e",
                    "AI_TIMEOUT_MS=1000",
                    "-e",
                    "WORKFLOW_LEASE_MS=10000",
                    "-e",
                    "WORKFLOW_HEARTBEAT_MS=1000",
                    "-e",
                    "WORKFLOW_HEALTH_MAX_AGE_MS=3000",
                    "-e",
                    "WORKFLOW_POLL_MS=200",
                    "-e",
                    "WORKFLOW_HOLD_AFTER_CLAIM_MS=12000",
                    "workflow-worker",
                ]
            )
            operation = self.http.request(
                "POST",
                "/career/target-imports",
                body={"input": {"kind": "FETCHED_URL", "url": self.job_source_url()}},
                headers=self.idempotency_headers(),
                expected=(202,),
            ).body
            operation_id = require_uuid(object_path(operation, "id"), "recovery operation")
            self.wait_for_state(operation_id, "RUNNING", 10)
            self.commands.run(["docker", "kill", "--signal=KILL", name])
            self.sleep(11)
            self.commands.run([*self.compose, "up", "-d", "--no-deps", "workflow-worker"])
            self.poll_operation(operation_id, timeout_seconds=60)
        finally:
            self.commands.run([*self.compose, "up", "-d", "--no-deps", "workflow-worker"], check=False)
            self.commands.run(["docker", "rm", "-f", name], check=False)
        if operation_id is None:
            raise AcceptanceError("worker recovery did not create an operation")

    def create_and_poll(self, path: str, body: dict[str, Any], resource_type: str) -> str:
        response = self.http.request(
            "POST", path, body=body, headers=self.idempotency_headers(), expected=(202,)
        ).body
        operation_id = require_uuid(object_path(response, "id"), "operation")
        operation = self.poll_operation(operation_id)
        if object_path(operation, "result", "resourceType") != resource_type:
            raise AcceptanceError(f"operation did not produce {resource_type}")
        return self.record_resource(object_path(operation, "result", "resourceId"), resource_type)

    def poll_operation(self, operation_id: str, timeout_seconds: int = 90) -> dict[str, Any]:
        deadline = self.monotonic() + timeout_seconds
        while self.monotonic() < deadline:
            try:
                operation = self.http.request("GET", f"/workflow-operations/{operation_id}").body
            except AcceptanceError as error:
                if "429" not in str(error):
                    raise
                self.sleep(1.0)
                continue
            state = object_path(operation, "state")
            if state == "SUCCEEDED":
                return operation
            if state in {"FAILED", "CANCELLED"}:
                code = object_path(operation, "error", "code") if state == "FAILED" else "CANCELLED"
                raise AcceptanceError(f"operation ended in {state} code={code}")
            self.sleep(0.5)
        raise AcceptanceError("operation polling deadline exceeded")

    def wait_for_state(self, operation_id: str, expected_state: str, timeout_seconds: int) -> None:
        deadline = self.monotonic() + timeout_seconds
        while self.monotonic() < deadline:
            operation = self.http.request("GET", f"/workflow-operations/{operation_id}").body
            if object_path(operation, "state") == expected_state:
                return
            self.sleep(0.2)
        raise AcceptanceError(f"operation did not reach {expected_state}")

    def post_resource(self, path: str, body: dict[str, Any], label: str) -> str:
        response = self.http.request(
            "POST", path, body=body, headers=self.idempotency_headers(), expected=(201,)
        ).body
        return self.record_resource(object_path(response, "id"), label)

    def idempotency_headers(self) -> dict[str, str]:
        value = str(uuid.uuid4())
        self.client_ids.add(value)
        return {"Idempotency-Key": value}

    def record_resource(self, value: Any, label: str) -> str:
        resource_id = require_uuid(value, label)
        if resource_id in self.client_ids:
            raise AcceptanceError(f"{label} reused a client-generated idempotency key")
        self.resource_ids.add(resource_id)
        return resource_id

    def assert_resource(self, label: str, resource_id: str, collection: str) -> None:
        response = self.http.request("GET", f"{collection}/{resource_id}").body
        if object_path(response, "id") != resource_id:
            raise AcceptanceError(f"seeded {label} does not match its backend resource")


def object_path(value: Any, *keys: str) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            raise AcceptanceError(f"response field is missing: {'.'.join(keys)}")
        current = current[key]
    return current


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--seed-receipt", required=True)
    parser.add_argument("--reset-performed", action="store_true")
    args = parser.parse_args()
    env = read_env(args.env)
    seed = json.loads(args.seed_receipt)
    lock = json.loads((args.repo_root / "deploy/local-stack.lock.json").read_text())
    project = lock.get("project")
    compose_file = lock.get("composeFile")
    if project != "jagalchi-v1-local" or not isinstance(compose_file, str):
        raise AcceptanceError("local acceptance requires the locked Compose project")
    compose = [
        "docker",
        "compose",
        "-p",
        project,
        "--env-file",
        str(args.env),
        "-f",
        str(args.repo_root / compose_file),
    ]
    acceptance = LocalAcceptance(
        UrllibTransport(),
        SubprocessTransport(),
        env,
        seed,
        compose,
        Path(env.get("API_SOURCE_DIR", "")),
        repo_root=args.repo_root,
        reset_performed=args.reset_performed,
    )
    acceptance.run()


if __name__ == "__main__":
    try:
        main()
    except (AcceptanceError, json.JSONDecodeError, KeyError) as error:
        print(f"local acceptance: FAILED: {error}", file=os.sys.stderr)
        raise SystemExit(1)
