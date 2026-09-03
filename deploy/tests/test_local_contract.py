from __future__ import annotations

import os
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class LocalStackContractTest(unittest.TestCase):
    def test_local_compose_uses_only_standalone_source_variables(self) -> None:
        compose = (ROOT / "compose.local.yml").read_text(encoding="utf-8")

        self.assertIn("context: ${API_SOURCE_DIR}", compose)
        self.assertIn("context: ${AI_SOURCE_DIR}", compose)
        self.assertNotIn("jagalchi-platform/services", compose)
        for service in ("api:", "workflow-worker:", "api-seed:", "ai:", "api-db:", "ai-db:", "minio:"):
            self.assertIn(service, compose)

    def test_local_scripts_are_executable_and_parse(self) -> None:
        scripts = sorted((ROOT / "deploy").glob("local-*.sh"))
        self.assertGreaterEqual(len(scripts), 6)
        for script in scripts:
            self.assertTrue(os.access(script, os.X_OK), script.name)
            subprocess.run(["bash", "-n", str(script)], check=True)

    def test_reset_requires_exact_confirmation(self) -> None:
        result = subprocess.run(
            [str(ROOT / "deploy/local-reset.sh"), str(ROOT / "deploy/local.env.example")],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("--confirm=jagalchi-v1-local", result.stderr)

    def test_every_local_compose_command_pins_project_name(self) -> None:
        for name in ("local-doctor.sh", "local-up.sh", "local-down.sh", "local-smoke.sh", "local-reset.sh"):
            script = (ROOT / "deploy" / name).read_text(encoding="utf-8")
            self.assertIn('load_local_stack_lock "$repo_root"', script, name)
            self.assertIn('docker compose -p "$project_name"', script, name)

        common = (ROOT / "deploy/local-common.sh").read_text(encoding="utf-8")
        self.assertIn('[[ "$project_name" == "jagalchi-v1-local" ]]', common)

    def test_bootstrap_uses_the_pinned_pnpm_without_corepack(self) -> None:
        bootstrap = (ROOT / "deploy/local-bootstrap.sh").read_text(encoding="utf-8")
        doctor = (ROOT / "deploy/local-doctor.sh").read_text(encoding="utf-8")

        self.assertIn('pnpm --dir "$API_SOURCE_DIR" install --frozen-lockfile', bootstrap)
        self.assertIn('pnpm --dir "$PLATFORM_SOURCE_DIR" install --frozen-lockfile', bootstrap)
        self.assertIn('"$AI_SOURCE_DIR/.venv/bin/python" -m pip install --requirement', bootstrap)
        self.assertNotIn("corepack", bootstrap)
        self.assertLess(
            bootstrap.index('[[ "$node_major" == "$expected_node_major" ]]'),
            bootstrap.index('pnpm --dir "$API_SOURCE_DIR" install --frozen-lockfile'),
        )
        self.assertLess(
            bootstrap.index('"$repo_root/deploy/local-doctor.sh" "$env_file"'),
            bootstrap.index('pnpm --dir "$API_SOURCE_DIR" install --frozen-lockfile'),
        )
        self.assertIn('local-stack.lock.json', bootstrap)
        self.assertIn('local-stack.lock.json', doctor)

        lock = json.loads((ROOT / "deploy/local-stack.lock.json").read_text(encoding="utf-8"))
        self.assertEqual(lock["nodeMajor"], 24)
        self.assertEqual(lock["pnpm"], "10.33.2")
        self.assertRegex(lock["apiContractSha256"], r"^[0-9a-f]{64}$")

    def test_lock_validator_enforces_every_declared_runtime_boundary(self) -> None:
        lock = json.loads((ROOT / "deploy/local-stack.lock.json").read_text(encoding="utf-8"))
        validator = ROOT / "deploy/validate-local-lock.py"
        compose_name = lock["composeFile"]

        with tempfile.TemporaryDirectory() as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            repo_root = temp_dir / "infra"
            platform_source = temp_dir / "platform"
            api_source = temp_dir / "api"
            ai_source = temp_dir / "ai"
            repo_root.mkdir()
            shutil.copy2(ROOT / compose_name, repo_root / compose_name)

            for source, remote, required in (
                (platform_source, "stacking-money-forever/jagalchi-platform", lock["canonicalSources"]["platform"]["requiredFiles"]),
                (api_source, "stacking-money-forever/jagalchi-api", lock["canonicalSources"]["api"]["requiredFiles"]),
                (ai_source, "stacking-money-forever/jagalchi-ai", lock["canonicalSources"]["ai"]["requiredFiles"]),
            ):
                source.mkdir()
                subprocess.run(["git", "init", "-q", str(source)], check=True)
                subprocess.run(
                    ["git", "-C", str(source), "remote", "add", "origin", f"https://github.com/{remote}.git"],
                    check=True,
                )
                for relative in required:
                    path = source / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("fixture\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(source), "add", "-A"], check=True)
                subprocess.run(
                    ["git", "-C", str(source), "-c", "user.email=test@example.com", "-c", "user.name=test", "commit", "-qm", "fixture"],
                    check=True, capture_output=True, text=True,
                )

            openapi_path = api_source / "contracts/openapi.json"
            openapi_path.parent.mkdir(parents=True, exist_ok=True)
            openapi_path.write_text("{}\n", encoding="utf-8")
            lock["python"] = f"{os.sys.version_info.major}.{os.sys.version_info.minor}"
            lock["apiContractSha256"] = __import__("hashlib").sha256(openapi_path.read_bytes()).hexdigest()
            # fixture 저장소 HEAD를 lock revisions에 기록해 revision 대조가 통과하도록
            lock["revisions"] = {
                "platform": subprocess.run(["git", "-C", str(platform_source), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip(),
                "api": subprocess.run(["git", "-C", str(api_source), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip(),
                "ai": subprocess.run(["git", "-C", str(ai_source), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip(),
            }
            lock_path = temp_dir / "lock.json"
            lock_path.write_text(json.dumps(lock), encoding="utf-8")

            command = [
                "python3", str(validator), "--lock", str(lock_path), "--repo-root", str(repo_root),
                "--platform-source", str(platform_source),
                "--api-source", str(api_source), "--ai-source", str(ai_source),
            ]
            provider_env = os.environ | {
                "JAGALCHI_LOCAL_MODE": "ci",
                "JOB_SOURCE_PROVIDER": "fixture",
                "GITHUB_PROVIDER": "fixture",
                "AI_PROVIDER": "fixture",
                "AI_V1_PROVIDER": "fake",
                "AI_DISABLE_EXTERNAL": "true",
                "AI_DISABLE_LLM": "true",
                "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
                "DEEPSEEK_EXTRACTION_MODEL": lock["providers"]["extractionModel"],
                "DEEPSEEK_PLANNING_MODEL": lock["providers"]["planningModel"],
            }
            subprocess.run(command, check=True, capture_output=True, text=True, env=provider_env)

            for field, bad_value in (
                ("python", "0.0"),
                ("apiContractSha256", "0" * 64),
                ("composeFile", "missing.yml"),
                ("loopbackPorts", [1]),
                ("forbiddenSourceFragments", [str(api_source)]),
                ("revisions", {**lock["revisions"], "api": "0" * 40}),
            ):
                changed = dict(lock)
                changed[field] = bad_value
                lock_path.write_text(json.dumps(changed), encoding="utf-8")
                result = subprocess.run(command, check=False, capture_output=True, text=True, env=provider_env)
                self.assertNotEqual(result.returncode, 0, field)

            changed = json.loads(json.dumps(lock))
            changed["canonicalSources"]["api"]["requiredFiles"].append("missing.file")
            lock_path.write_text(json.dumps(changed), encoding="utf-8")
            result = subprocess.run(command, check=False, capture_output=True, text=True, env=provider_env)
            self.assertNotEqual(result.returncode, 0, "canonicalSources")

            lock_path.write_text(json.dumps(lock), encoding="utf-8")
            broken_env = provider_env | {"AI_V1_PROVIDER": "deepseek"}
            result = subprocess.run(command, check=False, capture_output=True, text=True, env=broken_env)
            self.assertNotEqual(result.returncode, 0, "providers")

    def test_minio_configures_browser_upload_cors(self) -> None:
        compose = (ROOT / "compose.local.yml").read_text(encoding="utf-8")
        self.assertNotIn("mc cors set", compose)
        self.assertIn("MINIO_API_CORS_ALLOW_ORIGIN:", compose)
        self.assertIn("http://127.0.0.1:3000", compose)
        self.assertIn("http://127.0.0.1:3100", compose)
        self.assertIn('MINIO_API_CORS_ALLOW_CREDENTIALS_WITH_WILDCARD: "off"', compose)

    def test_seed_is_explicit_idempotent_tooling_contract(self) -> None:
        compose = (ROOT / "compose.local.yml").read_text(encoding="utf-8")
        seed = (ROOT / "deploy/local-seed.sh").read_text(encoding="utf-8")
        self.assertIn('profiles: ["tools"]', compose)
        self.assertIn('dist/database/dev-seed.js', compose)
        self.assertIn('--profile tools run --rm --no-deps --build -T api-seed', seed)
        self.assertIn('(mode_value & 077) != 0', (ROOT / "deploy/local-doctor.sh").read_text(encoding="utf-8"))
        self.assertIn('"schemaVersion", "userId", "projectRunId", "roadmapId"', seed)
        self.assertNotIn("LOCAL_SEED_PASSWORD", seed)

    def test_production_has_separate_migration_and_worker_services(self) -> None:
        compose = (ROOT / "compose.production.yml").read_text(encoding="utf-8")

        self.assertIn("workflow-worker:", compose)
        self.assertIn('command: ["node", "dist/worker.js"]', compose)
        self.assertIn("ai-migrate:", compose)
        self.assertIn('command: ["gunicorn", "--config", "gunicorn.conf.py"', compose)
        self.assertGreaterEqual(compose.count('DEPLOYMENT_ENV: "production"'), 2)
        self.assertIn('PROJECT_RUNS_ENABLED: "${PROJECT_RUNS_ENABLED:-false}"', compose)
        self.assertEqual(compose.count('AI_V1_PROMPT_VERSION: "2026-09-03.3"'), 2)
        self.assertGreaterEqual(compose.count("DEEPSEEK_EXTRACTION_MODEL"), 2)
        self.assertGreaterEqual(compose.count("DEEPSEEK_PLANNING_MODEL"), 2)
        self.assertGreaterEqual(compose.count("OBJECT_STORAGE_PRESIGN_ENDPOINT"), 2)
        self.assertNotIn("uvicorn.workers.UvicornWorker", compose)


if __name__ == "__main__":
    unittest.main()
