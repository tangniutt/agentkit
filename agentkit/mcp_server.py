# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Minimal MCP stdio server exposing AgentKit's core tools and resources."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import sys
import threading
from pathlib import Path
from typing import Any

from . import __version__
from .core import AKError, dumps, find_git_root

PROTOCOL_VERSIONS = {"2025-06-18", "2025-11-25"}
SERVER_INFO = {"name": "agentkit", "version": __version__}
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "data": {},
        "error": {"type": ["object", "null"]},
        "truncated": {"type": "boolean"},
        "next": {"type": ["string", "null"]},
    },
    "required": ["ok", "data", "error", "truncated", "next"],
    "additionalProperties": True,
}


def _tool(name: str, description: str, properties: dict, required: list[str] | None = None,
          annotations: dict | None = None) -> dict:
    schema = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return {"name": name, "description": description, "inputSchema": schema,
            "outputSchema": OUTPUT_SCHEMA, "annotations": annotations or {}}


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return True


def _schema_issues(schema: dict, value: Any, path: str = "arguments") -> list[str]:
    issues = []
    expected = schema.get("type")
    types = expected if isinstance(expected, list) else [expected] if expected else []
    if types and not any(_type_matches(value, item) for item in types):
        return [f"{path}: expected {' or '.join(types)}"]
    if "enum" in schema and value not in schema["enum"]:
        issues.append(f"{path}: expected one of {schema['enum']}")
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in value:
                issues.append(f"{path}: missing required field '{key}'")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    issues.append(f"{path}: unknown field '{key}'")
        for key, item in value.items():
            child = properties.get(key)
            if child is None and isinstance(schema.get("additionalProperties"), dict):
                child = schema["additionalProperties"]
            if isinstance(child, dict):
                issues += _schema_issues(child, item, f"{path}.{key}")
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            issues.append(f"{path}: expected at least {schema['minItems']} item(s)")
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                issues += _schema_issues(items, item, f"{path}[{index}]")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            issues.append(f"{path}: must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            issues.append(f"{path}: must be <= {schema['maximum']}")
    return issues


def _normalize_tool_arguments(name: str, arguments: dict) -> tuple[dict, list[str]]:
    args = dict(arguments)
    issues = []
    if name == "agentkit_read":
        if "spec" in args and "path" in args:
            issues.append("arguments: use only one of 'spec' or 'path'")
        elif "path" in args:
            args["spec"] = args.pop("path")
        if "spec" not in args:
            issues.append("arguments: missing required field 'spec' (or alias 'path')")
    single = {"agentkit_task_report", "agentkit_task_diff", "agentkit_task_steer", "agentkit_task_answer",
              "agentkit_task_accept", "agentkit_task_reject", "agentkit_task_handoff"}
    if name in single:
        if "id" in args and "ids" in args:
            issues.append("arguments: use only one of 'id' or 'ids'")
        elif "ids" in args:
            ids = args.pop("ids")
            if not isinstance(ids, list) or len(ids) != 1 or not isinstance(ids[0], str):
                issues.append("arguments.ids: single-task tools require exactly one string id")
            else:
                args["id"] = ids[0]
        if "id" not in args:
            issues.append("arguments: missing required field 'id' (or alias 'ids')")
    if name in ("agentkit_task_status", "agentkit_task_wait"):
        if "id" in args and "ids" in args:
            issues.append("arguments: use only one of 'id' or 'ids'")
        elif "id" in args:
            if not isinstance(args["id"], str):
                issues.append("arguments.id: expected string")
            else:
                args["ids"] = [args.pop("id")]
        if name == "agentkit_task_wait" and not args.get("ids"):
            issues.append("arguments: missing required field 'ids' (or alias 'id')")
    return args, issues


TOOLS = [
    _tool("agentkit_doctor", "Check version, git, write access, disk, tree-sitter and model configuration.", {},
          annotations={"readOnlyHint": True}),
    _tool("agentkit_manual", "Return the generated operating manual or one command's details.",
          {"words": {"type": "array", "items": {"type": "string"}}, "all": {"type": "boolean"}},
          annotations={"readOnlyHint": True}),
    _tool("agentkit_repo_map", "Return a budgeted repository map with directories and top symbols.",
          {"path": {"type": "string"}}, annotations={"readOnlyHint": True}),
    _tool("agentkit_search", "Search repository text with regex or literal matching.",
          {"pattern": {"type": "string"}, "path": {"type": "string"}, "glob": {"type": "string"},
           "fixed": {"type": "boolean"}, "ignore_case": {"type": "boolean"}, "offset": {"type": "integer"}},
          ["pattern"], {"readOnlyHint": True}),
    _tool("agentkit_read", "Read a file, line range, or symbol with budgeted output; 'path' is an alias for 'spec'.",
          {"spec": {"type": "string"}, "path": {"type": "string"}, "allow_secret_file": {"type": "boolean"}},
          annotations={"readOnlyHint": True}),
    _tool("agentkit_edit", "Replace one exact unique snippet; syntax errors are rejected.",
          {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"},
           "all": {"type": "boolean"}, "dry_run": {"type": "boolean"}},
          ["path", "old", "new"], {"readOnlyHint": False, "destructiveHint": False}),
    _tool("agentkit_run", "Run a shell command with timeout, permission checks and filtered output.",
          {"command": {"type": "string"}, "timeout": {"type": "number"}, "cwd": {"type": "string"},
           "raw": {"type": "boolean"}}, ["command"],
          {"readOnlyHint": False, "destructiveHint": True}),
    _tool("agentkit_verify", "Run acceptance checks in a clean worktree or temporary copy.",
          {"checks": {"type": "array", "items": {"type": "string"}, "minItems": 1},
           "timeout": {"type": "number"}, "base": {"type": "string"}, "receipt": {"type": "boolean"}}, ["checks"],
          {"readOnlyHint": False, "destructiveHint": False}),
    _tool("agentkit_memory_search", "Search project memory by keyword.",
          {"query": {"type": "string"}}, ["query"], {"readOnlyHint": True}),
    _tool("agentkit_credential_show", "Show non-secret credential grant/binding metadata for a task or id.",
          {"target": {"type": "string"}}, ["target"], {"readOnlyHint": True}),
    _tool("agentkit_credential_revoke", "Revoke a credential grant or task binding without exposing secret values.",
          {"target": {"type": "string"}}, ["target"], {"readOnlyHint": False, "destructiveHint": True}),
    _tool("agentkit_route", "Route a task using measured model profiles, risk and expected verified cost.",
          {"task_id": {"type": "string"}, "complexity": {"type": "integer", "minimum": 0, "maximum": 3},
           "blast_radius": {"type": "integer", "minimum": 0, "maximum": 3},
           "context_pressure": {"type": "integer", "minimum": 0, "maximum": 3},
           "required_capabilities": {"type": "array", "items": {"type": "string"}},
           "verification_strength": {"type": "string", "enum": ["basic", "standard", "strong"]},
           "profiles": {"type": "array", "items": {"type": "object", "required": ["model_id", "model_version", "evaluated_at", "eval_config_sha256"],
                         "properties": {"model_id": {"type": "string"}, "model_version": {"type": "string"},
                                        "status": {"type": "string", "enum": ["provisional", "active", "deprecated", "revoked"]},
                                        "capabilities": {"type": "object", "additionalProperties": {"type": "number"}},
                                        "pricing": {"type": "object"}, "limits": {"type": "object"}, "evidence": {"type": "object"},
                                        "evaluated_at": {"type": "string"}, "eval_config_sha256": {"type": "string"}}}},
           "min_samples": {"type": "integer", "minimum": 1}, "capability_threshold": {"type": "number", "minimum": 0, "maximum": 1}},
          ["task_id", "complexity", "blast_radius", "profiles"], {"readOnlyHint": True}),
    _tool("agentkit_task_create", "Create an isolated worker task with frozen fail-to-pass acceptance.",
          {"goal": {"type": "string"}, "checks": {"type": "array", "items": {"type": "string"}, "minItems": 1},
           "allow": {"type": "array", "items": {"type": "string"}}, "attachments": {"type": "array", "items": {"type": "string"}},
           "model": {"type": "string"},
           "backend": {"type": "string", "enum": ["builtin", "codex", "opencode", "qoder", "cursor"]},
           "tier": {"type": "string"}, "budget_tokens": {"type": "integer"}, "budget_usd": {"type": "number"},
           "budget_minutes": {"type": "number"}, "allow_baseline_pass": {"type": "boolean"},
           "fresh": {"type": "boolean"}},
          ["goal", "checks"], {"readOnlyHint": False, "destructiveHint": False}),
    _tool("agentkit_task_status", "Return compact status for one or more tasks; 'id' is a single-task alias for 'ids'.",
          {"id": {"type": "string"}, "ids": {"type": "array", "items": {"type": "string"}}},
          annotations={"readOnlyHint": True}),
    _tool("agentkit_task_wait", "Wait for tasks to finish and return a compact report; 'id' is a single-task alias for 'ids'.",
          {"id": {"type": "string"}, "ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
           "timeout": {"type": "number"}, "no_report": {"type": "boolean"}},
          annotations={"readOnlyHint": True}),
    _tool("agentkit_task_report", "Return the independent verification report for a task; 'ids' accepts one id.",
          {"id": {"type": "string"}, "ids": {"type": "array", "items": {"type": "string"}},
           "details": {"type": "boolean"}, "include_images": {"type": "boolean"}}, annotations={"readOnlyHint": True}),
    _tool("agentkit_task_diff", "Return a budgeted tracked and untracked task diff; 'ids' accepts one id.",
          {"id": {"type": "string"}, "ids": {"type": "array", "items": {"type": "string"}},
           "offset": {"type": "integer"}}, annotations={"readOnlyHint": True}),
    _tool("agentkit_task_steer", "Append a constraint to a queued or paused task; 'ids' accepts one id.",
          {"id": {"type": "string"}, "ids": {"type": "array", "items": {"type": "string"}},
           "text": {"type": "string"}}, ["text"], {"readOnlyHint": False}),
    _tool("agentkit_task_answer", "Answer a worker question and resume the task; 'ids' accepts one id.",
          {"id": {"type": "string"}, "ids": {"type": "array", "items": {"type": "string"}},
           "text": {"type": "string"}}, ["text"], {"readOnlyHint": False}),
    _tool("agentkit_task_accept", "Record acceptance of a verified pass without merging; 'ids' accepts one id.",
          {"id": {"type": "string"}, "ids": {"type": "array", "items": {"type": "string"}}},
          {"readOnlyHint": False}),
    _tool("agentkit_task_reject", "Record task rejection and reason; 'ids' accepts one id.",
          {"id": {"type": "string"}, "ids": {"type": "array", "items": {"type": "string"}},
           "reason": {"type": "string"}}, {"readOnlyHint": False}),
    _tool("agentkit_task_handoff", "Resume a paused task with another model or backend while preserving worktree and budgets; 'ids' accepts one id.",
          {"id": {"type": "string"}, "ids": {"type": "array", "items": {"type": "string"}}, "model": {"type": "string"},
           "backend": {"type": "string", "enum": ["builtin", "codex", "opencode", "qoder", "cursor"]}, "budget_tokens": {"type": "integer"},
           "budget_usd": {"type": "number"}, "budget_minutes": {"type": "number"}},
          {"readOnlyHint": False, "destructiveHint": False}),
    _tool("agentkit_goal_create", "Create a bounded Goal that loops through execution and independent verification.",
          {"outcome": {"type": "string"}, "checks": {"type": "array", "items": {"type": "string"}, "minItems": 1},
           "allow": {"type": "array", "items": {"type": "string"}}, "attachments": {"type": "array", "items": {"type": "string"}},
           "model": {"type": "string"},
           "budget_tokens": {"type": "integer"}, "budget_usd": {"type": "number"}, "budget_minutes": {"type": "number"},
           "max_iterations": {"type": "integer"}, "failover": {"type": "array", "items": {"type": "string"}}},
          ["outcome", "checks"], {"readOnlyHint": False, "destructiveHint": False}),
    _tool("agentkit_goal_run", "Run a Goal until verified completion, a stop condition, or the timeout.",
          {"id": {"type": "string"}, "timeout": {"type": "number"}}, ["id"],
          {"readOnlyHint": False, "destructiveHint": False}),
    _tool("agentkit_goal_status", "Return compact Goal status.",
          {"id": {"type": "string"}}, ["id"], {"readOnlyHint": True}),
    _tool("agentkit_goal_wait", "Wait for a Goal terminal state.",
          {"id": {"type": "string"}, "timeout": {"type": "number"}}, ["id"], {"readOnlyHint": True}),
    _tool("agentkit_goal_report", "Return Goal status and task evidence.",
          {"id": {"type": "string"}}, ["id"], {"readOnlyHint": True}),
    _tool("agentkit_goal_steer", "Append a constraint without changing the Goal outcome.",
          {"id": {"type": "string"}, "text": {"type": "string"}}, ["id", "text"], {"readOnlyHint": False}),
    _tool("agentkit_goal_cancel", "Cancel a Goal and its underlying task.",
          {"id": {"type": "string"}}, ["id"], {"readOnlyHint": False, "destructiveHint": True}),
    _tool("agentkit_goal_resume", "Resume an unfinished Goal and its resumable task.",
          {"id": {"type": "string"}, "model": {"type": "string"}, "budget_tokens": {"type": "integer"},
           "budget_usd": {"type": "number"}, "budget_minutes": {"type": "number"}}, ["id"],
          {"readOnlyHint": False, "destructiveHint": False}),
    _tool("agentkit_goal_answer", "Answer the worker question and continue the Goal.",
          {"id": {"type": "string"}, "text": {"type": "string"}}, ["id", "text"], {"readOnlyHint": False}),
    _tool("agentkit_code_search", "Search code passages using builtin or XERJ backend.",
          {"query": {"type": "string"}, "prefix": {"type": "string"}, "k": {"type": "integer"},
           "full": {"type": "integer"}, "backend": {"type": "string", "enum": ["auto", "builtin", "xerj"]}},
          ["query"], {"readOnlyHint": True}),
    _tool("agentkit_code_defs", "Find symbol definitions using builtin or XERJ backend.",
          {"symbol": {"type": "string"}, "prefix": {"type": "string"}, "k": {"type": "integer"},
           "kind": {"type": "string"}, "lang": {"type": "string"},
           "backend": {"type": "string", "enum": ["auto", "builtin", "xerj"]}},
          ["symbol"], {"readOnlyHint": True}),
    _tool("agentkit_code_refs", "Find symbol references using LSP or a labeled lexical fallback.",
          {"symbol": {"type": "string"}, "path": {"type": "string"}, "line": {"type": "integer"},
           "column": {"type": "integer"}, "k": {"type": "integer"},
           "include_declaration": {"type": "boolean"},
           "backend": {"type": "string", "enum": ["auto", "builtin", "lsp"]}},
          ["symbol"], {"readOnlyHint": True}),
    _tool("agentkit_code_index", "Index a folder with XERJ.",
          {"path": {"type": "string"}, "prefix": {"type": "string"}}, [], {"readOnlyHint": False}),
]


def _argv(name: str, a: dict) -> list:
    if name == "agentkit_doctor":
        return ["doctor"]
    if name == "agentkit_manual":
        return ["manual", *(a.get("words") or []), *(["--all"] if a.get("all") else [])]
    if name == "agentkit_repo_map":
        return ["repo_map", *([a["path"]] if a.get("path") else [])]
    if name == "agentkit_search":
        out = ["search", a["pattern"]]
        if a.get("path"):
            out.append(a["path"])
        for key, flag in (("glob", "--glob"), ("offset", "--offset")):
            if a.get(key) is not None:
                out += [flag, str(a[key])]
        if a.get("fixed"):
            out.append("--fixed")
        if a.get("ignore_case"):
            out.append("--ignore-case")
        return out
    if name == "agentkit_read":
        return ["read", a["spec"], *(["--allow-secret-file"] if a.get("allow_secret_file") else [])]
    if name == "agentkit_edit":
        out = ["edit", a["path"], "--old", a["old"], "--new", a["new"]]
        if a.get("all"):
            out.append("--all")
        if a.get("dry_run"):
            out.append("--dry-run")
        return out
    if name == "agentkit_run":
        out = ["run", a["command"]]
        for key, flag in (("timeout", "--timeout"), ("cwd", "--cwd")):
            if a.get(key) is not None:
                out += [flag, str(a[key])]
        if a.get("raw"):
            out.append("--raw")
        return out
    if name == "agentkit_verify":
        out = ["verify"]
        for check in a["checks"]:
            out += ["--check", check]
        if a.get("timeout") is not None:
            out += ["--timeout", str(a["timeout"])]
        if a.get("base"):
            out += ["--base", a["base"]]
        if a.get("receipt"):
            out.append("--receipt")
        return out
    if name == "agentkit_memory_search":
        return ["memory", "search", a["query"]]
    if name == "agentkit_credential_show":
        return ["credential", "show", a["target"]]
    if name == "agentkit_credential_revoke":
        return ["credential", "revoke", a["target"]]
    if name == "agentkit_task_create":
        out = ["task", "create", a["goal"]]
        for check in a["checks"]:
            out += ["--check", check]
        for path in a.get("allow") or []:
            out += ["--allow", path]
        for path in a.get("attachments") or []:
            out += ["--attach", path]
        for key, flag in (("model", "--model"), ("backend", "--backend"), ("tier", "--tier"),
                          ("budget_tokens", "--budget-tokens"), ("budget_usd", "--budget-usd"),
                          ("budget_minutes", "--budget-minutes")):
            if a.get(key) is not None:
                out += [flag, str(a[key])]
        if a.get("allow_baseline_pass"):
            out.append("--allow-baseline-pass")
        if a.get("fresh"):
            out.append("--fresh")
        return out
    if name == "agentkit_task_status":
        return ["task", "status", *(a.get("ids") or [])]
    if name == "agentkit_task_wait":
        out = ["task", "wait", *(a.get("ids") or [])]
        if a.get("timeout") is not None:
            out += ["--timeout", str(a["timeout"])]
        if a.get("no_report"):
            out.append("--no-report")
        return out
    if name == "agentkit_task_report":
        return ["task", "report", a["id"], *(["--details"] if a.get("details") or a.get("include_images") else [])]
    if name == "agentkit_task_diff":
        return ["task", "diff", a["id"], *(["--offset", str(a["offset"])] if a.get("offset") is not None else [])]
    if name == "agentkit_task_steer":
        return ["task", "steer", a["id"], a["text"]]
    if name == "agentkit_task_answer":
        return ["task", "answer", a["id"], a["text"]]
    if name == "agentkit_task_accept":
        return ["task", "accept", a["id"]]
    if name == "agentkit_task_reject":
        return ["task", "reject", a["id"], *([a["reason"]] if a.get("reason") else [])]
    if name == "agentkit_task_handoff":
        out = ["task", "handoff", a["id"]]
        if a.get("model"):
            out += ["--model", a["model"]]
        if a.get("backend"):
            out += ["--backend", a["backend"]]
        for key, flag in (("budget_tokens", "--budget-tokens"), ("budget_usd", "--budget-usd"),
                          ("budget_minutes", "--budget-minutes")):
            if a.get(key) is not None:
                out += [flag, str(a[key])]
        return out
    if name == "agentkit_goal_create":
        out = ["goal", "create", a["outcome"]]
        for check in a["checks"]:
            out += ["--check", check]
        for path in a.get("allow") or []:
            out += ["--allow", path]
        for path in a.get("attachments") or []:
            out += ["--attach", path]
        for model in a.get("failover") or []:
            out += ["--failover", model]
        for key, flag in (("model", "--model"), ("budget_tokens", "--budget-tokens"),
                          ("budget_usd", "--budget-usd"), ("budget_minutes", "--budget-minutes"),
                          ("max_iterations", "--max-iterations")):
            if a.get(key) is not None:
                out += [flag, str(a[key])]
        return out
    if name == "agentkit_goal_run":
        return ["goal", "run", a["id"], *(["--timeout", str(a["timeout"])] if a.get("timeout") is not None else [])]
    if name == "agentkit_goal_status":
        return ["goal", "status", a["id"]]
    if name == "agentkit_goal_wait":
        return ["goal", "wait", a["id"], *(["--timeout", str(a["timeout"])] if a.get("timeout") is not None else [])]
    if name == "agentkit_goal_report":
        return ["goal", "report", a["id"]]
    if name == "agentkit_goal_steer":
        return ["goal", "steer", a["id"], a["text"]]
    if name == "agentkit_goal_cancel":
        return ["goal", "cancel", a["id"]]
    if name == "agentkit_goal_resume":
        out = ["goal", "resume", a["id"]]
        for key, flag in (("model", "--model"), ("budget_tokens", "--budget-tokens"),
                          ("budget_usd", "--budget-usd"), ("budget_minutes", "--budget-minutes")):
            if a.get(key) is not None:
                out += [flag, str(a[key])]
        return out
    if name == "agentkit_goal_answer":
        return ["goal", "answer", a["id"], a["text"]]
    if name == "agentkit_code_search":
        out = ["code", "search", a["query"], "-k", str(a.get("k", 5)), "--full", str(a.get("full", 600)),
               "--backend", a.get("backend", "auto")]
        if a.get("prefix"):
            out += ["--prefix", a["prefix"]]
        return out
    if name == "agentkit_code_defs":
        out = ["code", "defs", a["symbol"], "-k", str(a.get("k", 5)), "--backend", a.get("backend", "auto")]
        for key, flag in (("prefix", "--prefix"), ("kind", "--kind"), ("lang", "--lang")):
            if a.get(key):
                out += [flag, a[key]]
        return out
    if name == "agentkit_code_refs":
        out = ["code", "refs", a["symbol"], "-k", str(a.get("k", 5)), "--backend", a.get("backend", "auto")]
        for key, flag in (("path", "--path"), ("line", "--line"), ("column", "--column")):
            if a.get(key) is not None:
                out += [flag, str(a[key])]
        if a.get("include_declaration"):
            out.append("--include-declaration")
        return out
    if name == "agentkit_code_index":
        out = ["code", "index", a.get("path") or "."]
        if a.get("prefix"):
            out += ["--prefix", a["prefix"]]
        return out
    raise KeyError(name)


def _call_tool(root: Path, name: str, arguments: dict) -> dict:
    from .cli import run_cli

    if name not in {t["name"] for t in TOOLS}:
        raise KeyError(name)
    if name == "agentkit_route":
        from .protocol import ModelProfile, ProtocolError, TaskRisk, ToolResult
        from .router import route

        try:
            risk = TaskRisk.from_dict({k: arguments[k] for k in
                                       ("complexity", "blast_radius", "context_pressure",
                                        "required_capabilities", "verification_strength") if k in arguments})
            profiles = [ModelProfile.from_dict(item) for item in arguments.get("profiles", [])]
            decision = route(arguments["task_id"], risk, profiles,
                             min_samples=int(arguments.get("min_samples", 5)),
                             capability_threshold=float(arguments.get("capability_threshold", 0.6)))
            env = ToolResult(ok=True, data=decision.to_dict()).to_dict()
        except (KeyError, TypeError, ValueError, ProtocolError) as e:
            env = ToolResult(ok=False, error={"what": str(e), "code": "AK-CONFIG"}).to_dict()
    else:
        env, _code, _human = run_cli(["--root", str(root), *_argv(name, arguments or {})], cwd=root)
    content = [{"type": "text", "text": dumps(env)}]
    if name == "agentkit_task_report" and arguments.get("include_images") and env.get("ok"):
        from . import images, tasks

        task = tasks.load(root, str(env["data"]["task"]))
        worktree = root / task["worktree"]
        for item in env["data"].get("images") or []:
            raw = images.image_bytes(worktree, item, base_dir=worktree / "artifacts")
            content.append({"type": "image", "data": base64.b64encode(raw).decode("ascii"),
                            "mimeType": item["mime"]})
    return {"resultType": "complete", "content": content,
            "structuredContent": env, "isError": not bool(env.get("ok"))}


def _resources(root: Path) -> list:
    resources = [
        {"uri": "agentkit://manual", "name": "AgentKit operating manual", "mimeType": "text/markdown"},
        {"uri": "agentkit://capabilities", "name": "AgentKit capabilities", "mimeType": "application/json"},
        {"uri": "agentkit://tasks", "name": "AgentKit tasks", "mimeType": "application/json"},
        {"uri": "agentkit://goals", "name": "AgentKit goals", "mimeType": "application/json"},
    ]
    from . import goals, tasks

    try:
        for task in tasks.all_tasks(root):
            identifier = str(task.get("id") or "")
            if identifier:
                resources.append({"uri": f"agentkit://task/{identifier}", "name": f"Task {identifier}",
                                  "mimeType": "application/json"})
        for goal in goals.list_goals(root, limit=1000):
            identifier = str(goal.get("id") or "")
            if identifier:
                resources.append({"uri": f"agentkit://goal/{identifier}", "name": f"Goal {identifier}",
                                  "mimeType": "application/json"})
    except Exception:
        pass
    return resources


def _page(items: list, cursor, state: dict, fingerprint: str) -> tuple[list, str | None]:
    pages = state.setdefault("pages", {})
    if cursor is None:
        offset = 0
    else:
        entry = pages.pop(cursor, None) if isinstance(cursor, str) else None
        if not entry or entry[0] != fingerprint:
            raise ValueError("invalid or expired cursor")
        offset = entry[1]
    if offset < 0 or offset > len(items):
        raise ValueError("invalid cursor")
    end = offset + 50
    next_cursor = None
    if end < len(items):
        next_cursor = secrets.token_hex(12)
        pages[next_cursor] = (fingerprint, end)
        if len(pages) > 128:
            pages.pop(next(iter(pages)))
    return items[offset:end], next_cursor


def _read_resource(root: Path, uri: str) -> dict:
    if uri == "agentkit://manual":
        from .manual import markdown_overview

        return {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": markdown_overview()}]}
    if uri == "agentkit://capabilities":
        from .backends import backend_capabilities
        from .config import load_config
        from .sandbox import capabilities as sandbox_capabilities
        from .sandbox import mode as sandbox_mode
        from .sandbox import no_sandbox_capabilities, policy_capabilities

        cfg = load_config(root)
        smode = sandbox_mode(cfg)
        sandbox_caps = (sandbox_capabilities() if smode not in ("off", "policy") else
                        policy_capabilities() if smode == "policy" else no_sandbox_capabilities())
        data = {"server": SERVER_INFO, "protocolVersions": sorted(PROTOCOL_VERSIONS), "root": str(root),
                "tools": [t["name"] for t in TOOLS],
                "backends": {name: backend_capabilities(name, cfg)
                             for name in ("builtin", "codex", "opencode", "qoder", "cursor")},
                "sandbox": {"mode": smode, "capabilities": sandbox_caps},
                "backend_variants": {"cursor_cli": backend_capabilities("cursor", cfg, transport="cli"),
                                     "cursor_sdk": backend_capabilities("cursor", cfg, transport="sdk")}}
        return {"contents": [{"uri": uri, "mimeType": "application/json", "text": dumps(data)}]}
    from . import goals, tasks
    from .core import Ctx

    if uri == "agentkit://tasks":
        data = {"tasks": [tasks.compact_status(root, task) for task in tasks.all_tasks(root)]}
    elif uri == "agentkit://goals":
        data = {"goals": goals.list_goals(root, limit=1000)}
    elif uri.startswith("agentkit://task/"):
        tid = uri.removeprefix("agentkit://task/")
        data = tasks.compact_status(root, tasks.load(root, tid))
    elif uri.startswith("agentkit://goal/"):
        gid = uri.removeprefix("agentkit://goal/")
        data = goals.status(Ctx(root=root, cwd=root), gid)
    else:
        raise KeyError(uri)
    return {"contents": [{"uri": uri, "mimeType": "application/json", "text": dumps(data)}]}


def _meta(version: str) -> dict:
    return {"io.modelcontextprotocol/serverInfo": SERVER_INFO,
            "io.modelcontextprotocol/protocolVersion": version}


def _result(req_id: Any, result: dict, version: str) -> dict:
    result = dict(result)
    result["_meta"] = _meta(version)
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str, data: Any = None) -> dict:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _state() -> dict:
    return {"version": "2025-11-25", "initialized": False, "subscriptions": set(), "stop": threading.Event(),
            "ready": threading.Event(), "pages": {}}


def _resource_state(root: Path) -> dict:
    state = {"agentkit://tasks": [], "agentkit://goals": []}
    for kind, folder, filename in (("task", "tasks", "task.json"), ("goal", "goals", "goal.json")):
        base = root / ".agentkit" / folder
        for path in sorted(base.glob(f"*/{filename}")):
            try:
                stat = path.stat()
            except OSError:
                continue
            identifier = path.parent.name
            uri = f"agentkit://{kind}/{identifier}"
            state[uri] = (stat.st_mtime_ns, stat.st_size)
            state[f"agentkit://{kind}s"].append((identifier, stat.st_mtime_ns, stat.st_size))
    state["agentkit://tasks"] = tuple(state["agentkit://tasks"])
    state["agentkit://goals"] = tuple(state["agentkit://goals"])
    return state


def _notification(method: str, params: dict, version: str) -> dict:
    return {"jsonrpc": "2.0", "method": method, "params": {**params, "_meta": _meta(version)}}


def _watch(root: Path, state: dict, write) -> None:
    try:
        previous = _resource_state(root)
    except Exception:
        previous = {}
    state["ready"].set()
    while not state["stop"].wait(0.5):
        try:
            current = _resource_state(root)
        except Exception:
            continue
        if not state.get("initialized"):
            previous = current
            continue
        try:
            if set(current) != set(previous):
                write(_notification("notifications/resources/list_changed", {}, state["version"]))
            changed = {uri for uri in set(current) | set(previous) if current.get(uri) != previous.get(uri)}
            updated = sorted(changed & state["subscriptions"])
            for collection, prefix in (("agentkit://tasks", "agentkit://task/"),
                                        ("agentkit://goals", "agentkit://goal/")):
                if collection in updated and any(uri.startswith(prefix) for uri in updated):
                    updated = [uri for uri in updated if not uri.startswith(prefix)]
            for uri in updated[:20]:
                write(_notification("notifications/resources/updated", {"uri": uri}, state["version"]))
        except Exception:
            pass
        previous = current


def handle(root: Path, msg: dict, state: dict | None = None) -> dict | None:
    state = state or _state()
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
    method = msg["method"]
    req_id = msg.get("id")
    params = msg.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _error(req_id, -32602, "Invalid params: expected an object")
    meta = params.get("_meta") or {}
    if not isinstance(meta, dict):
        return _error(req_id, -32602, "Invalid params: _meta must be an object")
    version = str(meta.get("io.modelcontextprotocol/protocolVersion") or "2025-11-25")
    if version not in PROTOCOL_VERSIONS:
        return _error(req_id, -32022, "Unsupported protocol version", {"supported": sorted(PROTOCOL_VERSIONS)})
    if method == "notifications/initialized":
        state["initialized"] = True
        state["version"] = version
        return None
    if method in ("notifications/cancelled", "notifications/progress"):
        return None
    if method == "exit":
        state["stop"].set()
        return None
    if method == "initialize":
        version = str((params or {}).get("protocolVersion") or version)
        if version not in PROTOCOL_VERSIONS:
            return _error(req_id, -32022, "Unsupported protocol version", {"supported": sorted(PROTOCOL_VERSIONS)})
        state["version"] = version
        resources = {"subscribe": True, "listChanged": True}
        return _result(req_id, {"protocolVersion": version,
                                "capabilities": {"tools": {}, "resources": resources},
                                "serverInfo": SERVER_INFO}, version)
    if method == "ping":
        return _result(req_id, {}, version)
    if method == "tools/list":
        return _result(req_id, {"resultType": "complete", "tools": TOOLS}, version)
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str):
            return _error(req_id, -32602, "Invalid params: tool name is required")
        tool = next((item for item in TOOLS if item["name"] == name), None)
        if tool is None:
            return _error(req_id, -32602, f"Unknown tool: {name}")
        arguments = params.get("arguments", {})
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return _error(req_id, -32602, f"Invalid arguments for {name}: expected an object")
        normalized, issues = _normalize_tool_arguments(name, arguments)
        issues += _schema_issues(tool["inputSchema"], normalized)
        if issues:
            return _error(req_id, -32602, f"Invalid arguments for {name}", {"tool": name, "issues": issues})
        try:
            return _result(req_id, _call_tool(root, name, normalized), version)
        except Exception as e:
            return _error(req_id, -32603, f"Tool dispatch failed: {type(e).__name__}: {e}")
    if method == "resources/list":
        try:
            items = _resources(root)
            fingerprint = hashlib.sha256(dumps(items).encode("utf-8")).hexdigest()
            page, next_cursor = _page(items, params.get("cursor"), state, fingerprint)
        except ValueError:
            return _error(req_id, -32602, "Invalid params: cursor")
        result = {"resultType": "complete", "resources": page}
        if next_cursor:
            result["nextCursor"] = next_cursor
        return _result(req_id, result, version)
    if method == "resources/templates/list":
        return _result(req_id, {"resultType": "complete", "resourceTemplates": []}, version)
    if method == "resources/read":
        uri = params.get("uri")
        if not isinstance(uri, str):
            return _error(req_id, -32602, "Invalid params: uri is required")
        try:
            return _result(req_id, _read_resource(root, uri), version)
        except (KeyError, AKError):
            return _error(req_id, -32602, f"Unknown resource: {uri}")
        except Exception as e:
            return _error(req_id, -32603, f"Resource read failed: {type(e).__name__}: {e}")
    if method in ("resources/subscribe", "resources/unsubscribe"):
        uri = params.get("uri")
        if not isinstance(uri, str):
            return _error(req_id, -32602, "Invalid params: uri is required")
        if uri not in {item["uri"] for item in _resources(root)}:
            return _error(req_id, -32602, f"Unknown resource: {uri}")
        if method == "resources/subscribe":
            state["subscriptions"].add(uri)
        else:
            state["subscriptions"].discard(uri)
        return _result(req_id, {}, version)
    if method == "shutdown":
        state["stop"].set()
        return _result(req_id, {}, version)
    return _error(req_id, -32601, f"Method not found: {method}")


def serve(root: Path) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    state = _state()
    write_lock = threading.Lock()

    def write(message: dict):
        with write_lock:
            sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()

    watcher = threading.Thread(target=_watch, args=(root, state, write), daemon=True)
    watcher.start()
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                response = _error(None, -32700, "Parse error")
            else:
                if isinstance(msg, list):
                    response = _error(None, -32600, "Batch requests are not supported")
                else:
                    try:
                        response = handle(root, msg, state)
                    except Exception as e:
                        response = _error(msg.get("id") if isinstance(msg, dict) else None, -32603,
                                          f"Internal error: {type(e).__name__}: {e}")
            if response is not None:
                write(response)
    finally:
        state["stop"].set()
        watcher.join(timeout=2)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="agentkit-mcp")
    p.add_argument("--root", default=None)
    a = p.parse_args(argv)
    cwd = Path.cwd().resolve()
    root = Path(a.root).resolve() if a.root else (find_git_root(cwd) or cwd)
    return serve(root)


if __name__ == "__main__":
    raise SystemExit(main())
