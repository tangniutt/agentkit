# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Packaged cost-efficient development skill structure and scoring script."""
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

from agentkit import release


def _skill() -> Path:
    return Path(release.__file__).parent / "skills" / "cost-efficient-ai-development"


def test_cost_skill_contract_is_valid():
    root = _skill()
    text = (root / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n") and "\n---\n" in text
    front = yaml.safe_load(text.split("---", 2)[1])
    assert front["name"] == "cost-efficient-ai-development"
    assert re.fullmatch(r"[a-z0-9-]+", front["name"])
    assert isinstance(front["description"], str) and len(front["description"]) <= 1024
    assert len(text.splitlines()) < 500 and "TODO:" not in text
    assert not (root / "README.md").exists()
    assert set(release._REQUIRED_SKILL_FILES) == {
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


def test_cost_skill_scoring_script(tmp_path):
    base = {"role": "implementer", "complexity": 1, "blast_radius": 1, "attempts": 1,
            "input_tokens": 1000, "output_tokens": 100, "verification_usd": 0.001, "rework_usd": 0.0,
            "latency_s": 10.0, "failure_kind": "none", "reasoning_level": "low",
            "timestamp": "2026-11-28T00:00:00Z", "eval_config_sha256": "a" * 64}
    rows = []
    for model, cost, passes in (("provider/model-a", 0.01, 5), ("provider/model-b", 0.02, 6)):
        for index in range(6):
            rows.append({**base, "task_id": f"{model}-{index}", "model_id": model, "model_version": "v1",
                         "verified_pass": index < passes, "attempt_usd": cost,
                         "failure_kind": "none" if index < passes else "capability"})
    data = tmp_path / "records.jsonl"
    data.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    script = _skill() / "scripts" / "score_records.py"
    result = subprocess.run([sys.executable, str(script), "--input", str(data), "--min-samples", "5"],
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] and payload["record_count"] == 12
    assert payload["recommended"]["model_id"] == "provider/model-a"
    data.write_text("{bad json}\n", encoding="utf-8")
    result = subprocess.run([sys.executable, str(script), "--input", str(data)],
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 2 and json.loads(result.stdout)["ok"] is False
