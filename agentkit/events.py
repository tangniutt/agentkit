# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""C-03 diagnosability: run ids, stable error codes, append-only JSONL event logs.

- Every CLI command and every task has a run id (tasks use their task id).
- Events go to .agentkit/logs/events.jsonl (commands, shared, keyed by run_id) or
  .agentkit/logs/events-<task id>.jsonl (one file per task). Lines are redacted.
- Error codes are stable API: never rename or reuse one; add new ones at the end.
  docs/errors.md lists them all (a test keeps the two in sync).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Optional

from .redact import redact

# code -> (one-line cause, fix hint)
ERRORS: dict = {
    "AK-USAGE": ("bad command line arguments", "agentkit <command> --help"),
    "AK-CONFIG": ("unreadable .agentkit/config.toml or bad config value", "fix the TOML; agentkit doctor"),
    "AK-PATH": ("path missing, outside the repo, a directory, or protected", "check the path with agentkit ls / glob"),
    "AK-READ-BINARY": ("file is binary", "binary files are not read; pick a text file"),
    "AK-READ-SECRET": ("refused to read a secret file (.env, ~/.ssh ...)", "do not read secrets; use --allow-secret-file only if the human asks"),
    "AK-READ-SPEC": ("bad read spec or range", "use file, file:START-END or file#Symbol"),
    "AK-SEARCH-REGEX": ("invalid regular expression", "fix the regex or pass --fixed"),
    "AK-TOOL-EDIT-NOMATCH": ("old text not found (0 matches)", "read the file again; candidates are in data.candidates"),
    "AK-TOOL-EDIT-MULTI": ("old text matches more than once", "add surrounding lines to make it unique, or --all"),
    "AK-TOOL-EDIT-SYNTAX": ("the edit would introduce a syntax error", "fix the new text at the reported line"),
    "AK-TOOL-EDIT-STALE": ("file changed since it was last read", "read the file again, then edit"),
    "AK-TOOL-EDIT-OP": ("bad edit/patch operation (exists, not found, bad JSON, unknown op)", "check the op list and paths"),
    "AK-TOOL-WRITE": ("writing files failed; everything was restored", "check disk space / permissions"),
    "AK-RUN-DENIED": ("command refused by a permission rule (M-22)", "ask the human, or allow it in .agentkit/config.toml [run]"),
    "AK-CHECKPOINT": ("unknown checkpoint or checkpoint failure", "agentkit checkpoint list"),
    "AK-MEMORY": ("unknown memory id, category or anchor", "agentkit memory list"),
    "AK-VERIFY-NOCHECK": ("verify/task has no check command", 'pass --check "<test command>"'),
    "AK-VERIFY-FAIL": ("checks ran and failed", "read the failing check's why/log; fix the code"),
    "AK-VERIFY-INVALID": ("a check command is missing or broken", "fix the check command itself, not the code"),
    "AK-VERIFY-SUSPICIOUS": ("checks passed but suspicious changes were found", "inspect the diff on the task branch before merging"),
    "AK-MODEL-NONE": ("no worker model configured (direct mode)", "use the tools directly, or agentkit model add"),
    "AK-MODEL-KEYENV": ("the model's key environment variable is not set", "export the variable named by key_env"),
    "AK-MODEL-AUTH": ("model rejected the key (401/403)", "agentkit model test NAME; check the key"),
    "AK-MODEL-429": ("model rate limit / quota exhausted (429)", "wait for Retry-After or the quota reset, then re-submit"),
    "AK-MODEL-TIMEOUT": ("model did not answer after 2 retries", "check the endpoint / network; re-submit later"),
    "AK-MODEL-NETWORK": ("cannot reach the model endpoint", "check base_url and network; agentkit model test"),
    "AK-MODEL-HTTP": ("model endpoint returned an HTTP error (e.g. 400/404 bad model id)", "check the model id and base_url"),
    "AK-MODEL-BAD-RESPONSE": ("model endpoint did not return an OpenAI chat completion", "check that base_url is OpenAI-compatible"),
    "AK-WORKER-MALFORMED": ("worker produced 3 malformed tool calls in a row", "try a stronger model tier"),
    "AK-WORKER-STUCK": ("worker made no progress (no new diff / same error repeated)", "narrow the goal or use a stronger model"),
    "AK-WORKER-ROUNDS": ("worker hit the max rounds limit", "raise [worker] max_rounds or split the task"),
    "AK-WORKER-CRASH": ("the worker loop raised an unexpected exception", "agentkit logs ID --errors; agentkit debug-bundle ID"),
    "AK-BUDGET": ("task stopped: token / USD / time budget exceeded", "raise --budget-* or simplify the task"),
    "AK-TASK-CANCELLED": ("task was cancelled", "re-submit with agentkit task create if needed"),
    "AK-TASK-INTERRUPTED": ("worker process died (crash / kill / reboot)", "agentkit task report ID; re-submit to retry"),
    "AK-TASK-NOGIT": ("dispatch needs a git repo with at least one commit", "git init && git add -A && git commit -m init"),
    "AK-TASK-UNKNOWN": ("unknown or invalid task id", "agentkit task status"),
    "AK-TASK-STATE": ("operation not allowed in the task's current state", "agentkit task status ID"),
    "AK-TASK-WAITING": ("worker is waiting for a supervisor answer", "agentkit task answer ID 'answer'"),
    "AK-GIT": ("a git command failed", "agentkit doctor"),
    "AK-DISK-FULL": ("not enough free disk space", "agentkit task clean --all"),
    "AK-LOGS-UNKNOWN": ("no events for that run / task id", "ids are in error.run, task ids in task status"),
    "AK-INTERNAL": ("bug in agentkit (uncaught exception)", "agentkit debug-bundle RUN and attach it to an issue"),
    "AK-MODEL-UNAVAILABLE": ("model endpoint overloaded / server error (500/502/503) after 2 retries", "re-submit later or use another model"),
    "AK-RELEASE": ("release operation failed", "agentkit release plan"),
    "AK-RELEASE-HASH": ("artifact or manifest invalid", "restore or rebuild artifacts"),
}

_RULES = [
    (r"^usage:|needs a value|must be an integer|needs task ids|needs a task id|needs a goal|unknown .*subcommand|bad (model name|tier|category)|takes the NAME", "AK-USAGE"),
    (r"no --check given|edit needs --old", None),  # resolved below by command
    (r"changed since you last read", "AK-TOOL-EDIT-STALE"),
    (r"syntax error in", "AK-TOOL-EDIT-SYNTAX"),
    (r"matches \d+ times", "AK-TOOL-EDIT-MULTI"),
    (r"no match|0 matches|old text not found", "AK-TOOL-EDIT-NOMATCH"),
    (r"write failed", "AK-TOOL-WRITE"),
    (r"empty old text|exists: |destination exists|unknown op|patch input is not JSON|^(replace|create|delete|rename).*not found", "AK-TOOL-EDIT-OP"),
    (r"refused by permission rule|check refused", "AK-RUN-DENIED"),
    (r"^binary file", "AK-READ-BINARY"),
    (r"secret", "AK-READ-SECRET"),
    (r"bad read spec|beyond end of file", "AK-READ-SPEC"),
    (r"bad regex", "AK-SEARCH-REGEX"),
    (r"checkpoint", "AK-CHECKPOINT"),
    (r"memory|anchor not found", "AK-MEMORY"),
    (r"direct mode|no worker model|no model with tier|unknown model", "AK-MODEL-NONE"),
    (r"environment variable .* is not set", "AK-MODEL-KEYENV"),
    (r"model test failed: auth", "AK-MODEL-AUTH"),
    (r"model test failed: rate_limited", "AK-MODEL-429"),
    (r"model test failed: timeout", "AK-MODEL-TIMEOUT"),
    (r"model test failed: network", "AK-MODEL-NETWORK"),
    (r"model test failed: unavailable", "AK-MODEL-UNAVAILABLE"),
    (r"model test failed: bad_response", "AK-MODEL-BAD-RESPONSE"),
    (r"model test failed", "AK-MODEL-HTTP"),
    (r"not a git repository|no commits", "AK-TASK-NOGIT"),
    (r"unknown task|invalid task id", "AK-TASK-UNKNOWN"),
    (r"^task t\w+ is |nothing to clean|worktree is gone", "AK-TASK-STATE"),
    (r"disk almost full", "AK-DISK-FULL"),
    (r"^git ", "AK-GIT"),
    (r"no events for", "AK-LOGS-UNKNOWN"),
    (r"path outside|not found:|is a directory|not a directory|refused: |no paths given", "AK-PATH"),
    (r"config", "AK-CONFIG"),
]


def classify(what: str, cmd: Optional[str] = None) -> str:
    w = what or ""
    if re.search(r"no --check given", w):
        return "AK-VERIFY-NOCHECK"
    if re.search(r"edit needs --old", w):
        return "AK-USAGE"
    for pat, code in _RULES:
        if code and re.search(pat, w, re.I):
            return code
    return "AK-INTERNAL" if w.startswith("internal error") else "AK-USAGE" if cmd is None else _generic(cmd)


def _generic(cmd: str) -> str:
    return {"read": "AK-PATH", "search": "AK-PATH", "ls": "AK-PATH", "glob": "AK-PATH", "edit": "AK-TOOL-EDIT-OP",
            "patch": "AK-TOOL-EDIT-OP", "create": "AK-TOOL-EDIT-OP", "delete": "AK-TOOL-EDIT-OP", "rename": "AK-TOOL-EDIT-OP",
            "run": "AK-RUN-DENIED", "checkpoint": "AK-CHECKPOINT", "memory": "AK-MEMORY", "verify": "AK-VERIFY-INVALID",
            "model": "AK-MODEL-HTTP", "task": "AK-TASK-STATE", "logs": "AK-LOGS-UNKNOWN"}.get(cmd, "AK-USAGE")


def model_code(kind: str) -> str:
    return {"auth": "AK-MODEL-AUTH", "rate_limited": "AK-MODEL-429", "timeout": "AK-MODEL-TIMEOUT",
            "network": "AK-MODEL-NETWORK", "unavailable": "AK-MODEL-UNAVAILABLE", "bad_response": "AK-MODEL-BAD-RESPONSE"}.get(kind, "AK-MODEL-HTTP")


def hint(code: str) -> str:
    return ERRORS.get(code, ("", ""))[1]


def new_run_id() -> str:
    return "r" + time.strftime("%m%d%H%M%S") + secrets.token_hex(2)


RUN_ID_RE = re.compile(r"^(r\d{10}[0-9a-f]{4}|t[0-9a-f]{6,12})$")
MAX_SHARED_BYTES = 5 * 1024 * 1024


def log_path(root: Path, run_id: str) -> Path:
    d = root / ".agentkit" / "logs"
    if run_id.startswith("t"):
        return d / f"events-{run_id}.jsonl"
    return d / "events.jsonl"


class EventLog:
    """Append-only, redacted JSONL. Never raises (logging must not break the tool)."""

    def __init__(self, root: Path, run_id: str, component: str = "cli"):
        self.root, self.run_id, self.component = Path(root), run_id, component
        self.path = log_path(self.root, run_id)

    def emit(self, event: str, step=None, component: Optional[str] = None, duration: Optional[float] = None,
             error_code: Optional[str] = None, **extra) -> None:
        rec = {"ts": round(time.time(), 3), "run_id": self.run_id, "step": step, "component": component or self.component,
               "event": event, "duration": None if duration is None else round(duration, 3), "error_code": error_code}
        rec.update({k: v for k, v in extra.items() if v is not None})
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.name == "events.jsonl" and self.path.exists() and self.path.stat().st_size > MAX_SHARED_BYTES:
                os.replace(self.path, self.path.with_suffix(".jsonl.1"))
            line = json.dumps(redact(rec), ensure_ascii=False, default=str)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def pointer(self) -> str:
        return f"agentkit logs {self.run_id} --errors"


def read_events(root: Path, run_id: str) -> list:
    paths = [log_path(root, run_id)]
    if not run_id.startswith("t"):
        paths.insert(0, paths[0].with_suffix(".jsonl.1"))
    out = []
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if rec.get("run_id") == run_id:
                        out.append(rec)
        except FileNotFoundError:
            pass
    return out
