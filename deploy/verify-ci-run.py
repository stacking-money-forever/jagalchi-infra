#!/usr/bin/env python3
"""Classify the required GitHub Actions run for one exact commit."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def classify(payload: dict[str, Any], workflow: str, workflow_path: str, sha: str) -> str:
    runs = [
        run
        for run in payload.get("workflow_runs", [])
        if run.get("name") == workflow
        and str(run.get("path", "")).split("@", 1)[0] == workflow_path
        and run.get("head_sha") == sha
        and run.get("event") == "push"
    ]
    if not runs:
        return "pending"
    latest = max(runs, key=lambda run: (int(run.get("run_number", 0)), int(run.get("run_attempt", 0))))
    if latest.get("status") != "completed":
        return "pending"
    return "success" if latest.get("conclusion") == "success" else "failure"


def main() -> int:
    if len(sys.argv) != 5:
        print("usage: verify-ci-run.py PAYLOAD WORKFLOW WORKFLOW_PATH SHA", file=sys.stderr)
        return 2
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    verdict = classify(payload, sys.argv[2], sys.argv[3], sys.argv[4])
    print(verdict)
    return {"success": 0, "failure": 1, "pending": 75}[verdict]


if __name__ == "__main__":
    raise SystemExit(main())
