#!/usr/bin/env python3
"""Summarize task records into conservative cost-per-verified-success profiles."""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROLES = {"planner", "implementer", "reviewer"}
FAILURES = {"none", "infrastructure", "config", "capability", "risk"}


def fail(message: str, code: int = 2) -> int:
    print(json.dumps({"ok": False, "error": message}, ensure_ascii=False, separators=(",", ":")))
    return code


def number(value, field: str, line: int) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"line {line}: {field} must be a finite non-negative number")
    return float(value)


def integer(value, field: str, line: int, low: int, high: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < low or (high is not None and value > high):
        raise ValueError(f"line {line}: {field} is outside {low}..{high if high is not None else 'inf'}")
    return value


def text(value, field: str, line: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"line {line}: {field} must be a non-empty string")
    return value.strip()


def wilson_lower(passes: int, count: int, z: float = 1.96) -> float:
    if count <= 0:
        return 0.0
    p = passes / count
    denom = 1 + z * z / count
    center = p + z * z / (2 * count)
    margin = z * math.sqrt(p * (1 - p) / count + z * z / (4 * count * count))
    return max(0.0, (center - margin) / denom)


def load_rows(path: Path) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except ValueError as e:
                raise ValueError(f"line {line_no}: invalid JSON") from e
            if not isinstance(row, dict):
                raise ValueError(f"line {line_no}: record must be an object")
            required = {"task_id", "model_id", "model_version", "role", "complexity", "blast_radius",
                        "verified_pass", "attempts", "input_tokens", "output_tokens", "attempt_usd",
                        "verification_usd", "rework_usd", "latency_s", "failure_kind", "reasoning_level",
                        "timestamp", "eval_config_sha256"}
            missing = required - set(row)
            if missing:
                raise ValueError(f"line {line_no}: missing fields: {', '.join(sorted(missing))}")
            row["task_id"] = text(row["task_id"], "task_id", line_no)
            row["model_id"] = text(row["model_id"], "model_id", line_no)
            row["model_version"] = text(row["model_version"], "model_version", line_no)
            row["role"] = text(row["role"], "role", line_no)
            if row["role"] not in ROLES:
                raise ValueError(f"line {line_no}: unsupported role")
            row["complexity"] = integer(row["complexity"], "complexity", line_no, 0, 3)
            row["blast_radius"] = integer(row["blast_radius"], "blast_radius", line_no, 0, 3)
            if not isinstance(row["verified_pass"], bool):
                raise ValueError(f"line {line_no}: verified_pass must be boolean")
            row["attempts"] = integer(row["attempts"], "attempts", line_no, 1)
            row["input_tokens"] = integer(row["input_tokens"], "input_tokens", line_no, 0)
            row["output_tokens"] = integer(row["output_tokens"], "output_tokens", line_no, 0)
            for field in ("attempt_usd", "verification_usd", "rework_usd", "latency_s"):
                row[field] = number(row[field], field, line_no)
            row["failure_kind"] = text(row["failure_kind"], "failure_kind", line_no)
            if row["failure_kind"] not in FAILURES:
                raise ValueError(f"line {line_no}: unsupported failure_kind")
            row["reasoning_level"] = text(row["reasoning_level"], "reasoning_level", line_no)
            if row["reasoning_level"] not in {"low", "medium", "high"}:
                raise ValueError(f"line {line_no}: unsupported reasoning_level")
            row["timestamp"] = text(row["timestamp"], "timestamp", line_no)
            row["eval_config_sha256"] = text(row["eval_config_sha256"], "eval_config_sha256", line_no)
            rows.append(row)
    return rows


def summarize(rows: list[dict], min_samples: int) -> list[dict]:
    groups = defaultdict(list)
    for row in rows:
        key = (row["model_id"], row["model_version"], row["role"], row["eval_config_sha256"])
        groups[key].append(row)
    profiles = []
    for (model_id, version, role, config_hash), items in groups.items():
        count = len(items)
        passes = sum(1 for item in items if item["verified_pass"])
        total_cost = sum(item["attempt_usd"] + item["verification_usd"] + item["rework_usd"] for item in items)
        avg_cost = total_cost / count if count else 0.0
        lower = wilson_lower(passes, count)
        profiles.append({
            "model_id": model_id,
            "model_version": version,
            "role": role,
            "eval_config_sha256": config_hash,
            "sample_count": count,
            "verified_passes": passes,
            "verified_pass_rate": round(passes / count, 6) if count else 0.0,
            "wilson_lower_bound": round(lower, 6),
            "average_task_cost": round(avg_cost, 6),
            "expected_cost_per_verified_success": round(avg_cost / lower, 6) if lower > 0 else None,
            "average_latency_s": round(sum(item["latency_s"] for item in items) / count, 3) if count else 0.0,
            "input_tokens": sum(item["input_tokens"] for item in items),
            "output_tokens": sum(item["output_tokens"] for item in items),
            "failure_kinds": dict(Counter(item["failure_kind"] for item in items)),
            "eligible": count >= min_samples and lower > 0,
        })
    return sorted(profiles, key=lambda p: (p["expected_cost_per_verified_success"] is None,
                                           p["expected_cost_per_verified_success"] or 0,
                                           p["model_id"], p["model_version"], p["role"]))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Score AI development task records without network access.")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--min-samples", type=int, default=5)
    parser.add_argument("--complexity", type=int, choices=range(4))
    parser.add_argument("--blast-radius", type=int, choices=range(4))
    args = parser.parse_args(argv)
    if args.min_samples < 1:
        return fail("--min-samples must be at least 1")
    try:
        rows = load_rows(args.input)
    except (OSError, ValueError) as e:
        return fail(str(e))
    if args.complexity is not None:
        rows = [row for row in rows if row["complexity"] == args.complexity]
    if args.blast_radius is not None:
        rows = [row for row in rows if row["blast_radius"] == args.blast_radius]
    profiles = summarize(rows, args.min_samples)
    recommended = next((profile for profile in profiles if profile["eligible"]), None)
    result = {
        "ok": True,
        "min_samples": args.min_samples,
        "filters": {"complexity": args.complexity, "blast_radius": args.blast_radius},
        "record_count": len(rows),
        "profiles": profiles,
        "recommended": recommended,
        "notes": [
            "Wilson lower bound is used as a conservative success probability.",
            "Infrastructure and config failures remain in the records; classify them before policy decisions.",
            "Do not mix model versions or eval configurations when updating a routing profile.",
        ],
    }
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
