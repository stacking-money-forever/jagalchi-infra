from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
COMPOSE = ROOT / "compose.production.yml"
ENV_EXAMPLE = DEPLOY / "personal-server.env.example"

GHCR_IMAGE_RE = re.compile(
    r"^ghcr\.io/stacking-money-forever/jagalchi-(api|ai):[A-Za-z0-9._-]+$"
)

PRODUCTION_SHELL_SCRIPTS = sorted(
    path
    for path in DEPLOY.glob("*.sh")
    if not path.name.startswith("local-")
)


class ProductionContractTest(unittest.TestCase):
    def test_compose_pins_external_images_only(self) -> None:
        compose = COMPOSE.read_text(encoding="utf-8")

        self.assertNotIn("build:", compose)
        self.assertNotIn("dockerfile:", compose)
        self.assertNotIn("context:", compose)
        self.assertNotIn("services/api", compose)
        self.assertNotIn("services/ai", compose)
        self.assertGreaterEqual(compose.count("${API_IMAGE:?API_IMAGE is required}"), 3)
        self.assertGreaterEqual(compose.count("${AI_IMAGE:?AI_IMAGE is required}"), 2)

    def test_compose_declares_required_runtime_services(self) -> None:
        compose = COMPOSE.read_text(encoding="utf-8")

        for service in (
            "api:",
            "api-migrate:",
            "workflow-worker:",
            "ai:",
            "ai-migrate:",
            "ai-db:",
            "minio:",
            "minio-init:",
            "cloudflared:",
        ):
            self.assertIn(service, compose)

    def test_personal_server_env_example_declares_image_pins(self) -> None:
        env = ENV_EXAMPLE.read_text(encoding="utf-8")

        for key in ("API_IMAGE=", "AI_IMAGE="):
            self.assertIn(key, env)
        for line in env.splitlines():
            if line.startswith(("API_IMAGE=", "AI_IMAGE=")):
                _, value = line.split("=", 1)
                self.assertTrue(
                    value.startswith("ghcr.io/stacking-money-forever/jagalchi-"),
                    line,
                )

    def test_personal_server_env_example_matches_preflight_required_keys(self) -> None:
        preflight = (DEPLOY / "preflight.sh").read_text(encoding="utf-8")
        env_keys = {
            line.split("=", 1)[0]
            for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#") and "=" in line
        }
        match = re.search(
            r"required_keys=\(\n((?:  [A-Z0-9_]+.*\n)+)\)",
            preflight,
        )
        self.assertIsNotNone(match, "required_keys block missing from preflight.sh")
        required = re.findall(r"  ([A-Z0-9_]+)", match.group(1))
        missing = [key for key in required if key not in env_keys]
        self.assertEqual(missing, [], f"missing from personal-server.env.example: {missing}")

    def test_deploy_script_pulls_reviewed_images(self) -> None:
        deploy = (DEPLOY / "deploy.sh").read_text(encoding="utf-8")

        self.assertIn("pull api api-migrate ai", deploy)
        self.assertNotIn(" build --pull api", deploy)
        self.assertNotIn("dockerfile:", deploy)

    def test_preflight_validates_ghcr_image_pins(self) -> None:
        preflight = (DEPLOY / "preflight.sh").read_text(encoding="utf-8")

        self.assertIn("API_IMAGE AI_IMAGE", preflight)
        self.assertIn("stacking-money-forever GHCR image tag", preflight)
        self.assertRegex(preflight, r"\^ghcr\\.io/stacking-money-forever/jagalchi-\(api\|ai\):")

    def test_backend_cd_targets_infra_repository(self) -> None:
        backend_cd = (DEPLOY / "backend-cd.env.example").read_text(encoding="utf-8")
        cd_poll = (DEPLOY / "cd-poll.sh").read_text(encoding="utf-8")

        self.assertIn("CD_REPOSITORY=stacking-money-forever/jagalchi-infra", backend_cd)
        self.assertIn(
            'CD_REPOSITORY:=stacking-money-forever/jagalchi-infra',
            cd_poll,
        )
        self.assertIn("$release_dir/deploy/deploy.sh", cd_poll)

    def test_production_scripts_parse(self) -> None:
        self.assertGreaterEqual(len(PRODUCTION_SHELL_SCRIPTS), 8)
        for script in PRODUCTION_SHELL_SCRIPTS:
            subprocess.run(["bash", "-n", str(script)], check=True)

    def test_production_compose_config_is_valid(self) -> None:
        subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                str(ENV_EXAMPLE),
                "-f",
                str(COMPOSE),
                "--profile",
                "cloudflare-tunnel",
                "config",
                "--quiet",
            ],
            check=True,
            cwd=ROOT,
        )

    def test_production_has_separate_migration_and_worker_services(self) -> None:
        compose = COMPOSE.read_text(encoding="utf-8")

        self.assertIn("workflow-worker:", compose)
        self.assertIn('command: ["node", "dist/worker.js"]', compose)
        self.assertIn("ai-migrate:", compose)
        self.assertIn('command: ["gunicorn", "--config", "gunicorn.conf.py"', compose)
        self.assertGreaterEqual(compose.count('DEPLOYMENT_ENV: "production"'), 2)
        self.assertIn('PROJECT_RUNS_ENABLED: "${PROJECT_RUNS_ENABLED:-false}"', compose)
        self.assertEqual(compose.count('AI_V1_PROMPT_VERSION: "2026-09-04.2"'), 2)
        self.assertGreaterEqual(compose.count("DEEPSEEK_EXTRACTION_MODEL"), 2)
        self.assertGreaterEqual(compose.count("DEEPSEEK_PLANNING_MODEL"), 2)
        self.assertGreaterEqual(compose.count("OBJECT_STORAGE_PRESIGN_ENDPOINT"), 2)
        self.assertNotIn("uvicorn.workers.UvicornWorker", compose)

    def test_readme_documents_infra_checkout_path(self) -> None:
        readme = (DEPLOY / "README.md").read_text(encoding="utf-8")

        self.assertIn("/srv/jagalchi-infra", readme)
        self.assertIn("API_IMAGE", readme)
        self.assertIn("AI_IMAGE", readme)
        self.assertNotIn("/srv/jagalchi-platform", readme)

    def test_backup_script_records_ghcr_image_pins(self) -> None:
        backup = (DEPLOY / "backup-before-deploy.sh").read_text(encoding="utf-8")
        self.assertIn('env_value API_IMAGE', backup)
        self.assertIn('env_value AI_IMAGE', backup)
        self.assertIn("jagalchi-rollback/api:", backup)
        self.assertNotIn("jagalchi-personal-api:production", backup)

    def test_ghcr_example_tags_match_preflight_regex(self) -> None:
        env = ENV_EXAMPLE.read_text(encoding="utf-8")
        for line in env.splitlines():
            if not line.startswith(("API_IMAGE=", "AI_IMAGE=")):
                continue
            _, value = line.split("=", 1)
            if "replace-with-reviewed-tag" in value:
                continue
            self.assertRegex(value, GHCR_IMAGE_RE, line)


if __name__ == "__main__":
    unittest.main()
