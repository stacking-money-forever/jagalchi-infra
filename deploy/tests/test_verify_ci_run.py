from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "verify-ci-run.py"
SPEC = importlib.util.spec_from_file_location("verify_ci_run", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def run(status: str, conclusion: str | None, *, name: str = "CI", sha: str = "a" * 40):
    return {
        "name": name,
        "head_sha": sha,
        "event": "push",
        "path": ".github/workflows/ci.yml@main",
        "status": status,
        "conclusion": conclusion,
        "run_number": 7,
        "run_attempt": 1,
    }


class VerifyCiRunTests(unittest.TestCase):
    def test_success_requires_exact_workflow_and_sha(self) -> None:
        sha = "a" * 40
        payload = {"workflow_runs": [run("completed", "success", sha=sha)]}
        self.assertEqual(MODULE.classify(payload, "CI", ".github/workflows/ci.yml", sha), "success")
        self.assertEqual(MODULE.classify(payload, "Other", ".github/workflows/ci.yml", sha), "pending")
        self.assertEqual(MODULE.classify(payload, "CI", ".github/workflows/other.yml", sha), "pending")
        self.assertEqual(MODULE.classify(payload, "CI", ".github/workflows/ci.yml", "b" * 40), "pending")

    def test_incomplete_is_pending(self) -> None:
        sha = "a" * 40
        self.assertEqual(MODULE.classify({"workflow_runs": [run("in_progress", None)]}, "CI", ".github/workflows/ci.yml", sha), "pending")

    def test_latest_attempt_controls_failure(self) -> None:
        sha = "a" * 40
        first = run("completed", "success")
        latest = {**run("completed", "failure"), "run_attempt": 2}
        self.assertEqual(MODULE.classify({"workflow_runs": [first, latest]}, "CI", ".github/workflows/ci.yml", sha), "failure")


if __name__ == "__main__":
    unittest.main()
