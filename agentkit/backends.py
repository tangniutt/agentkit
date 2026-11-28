# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""External worker backends using official headless JSONL interfaces."""
from __future__ import annotations

import importlib.util
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional
from unittest.mock import patch

from .config import saved_credentials_allowed
from .protocol import BackendCapabilities
from .redact import redact_text
from .worker import Outcome


def codex_config(cfg: dict) -> dict:
    return ((cfg or {}).get("workers") or {}).get("codex") or {}


def _command(cfg: dict, backend: str = "codex", executable: Optional[str] = None) -> list[str]:
    raw = cfg["command"] if "command" in cfg else [executable or backend]
    if isinstance(raw, str):
        raw = shlex.split(raw)
    if not isinstance(raw, list) or not raw or any(not isinstance(x, str) or not x for x in raw):
        raise ValueError(f"workers.{backend}.command must be a non-empty argv array")
    return list(raw)


def codex_available(cfg: dict) -> bool:
    try:
        command = _command(codex_config(cfg))
    except ValueError:
        return False
    return shutil.which(command[0]) is not None or Path(command[0]).is_file()


def codex_prices(cfg: dict) -> tuple[float, float]:
    table = codex_config(cfg)
    try:
        prices = float(table.get("price_in") or 0), float(table.get("price_out") or 0)
    except (TypeError, ValueError) as e:
        raise ValueError("workers.codex prices must be non-negative numbers") from e
    if any(x < 0 for x in prices):
        raise ValueError("workers.codex prices must be non-negative numbers")
    return prices


def opencode_config(cfg: dict) -> dict:
    return ((cfg or {}).get("workers") or {}).get("opencode") or {}


def opencode_available(cfg: dict) -> bool:
    try:
        command = _command(opencode_config(cfg), "opencode")
    except ValueError:
        return False
    return shutil.which(command[0]) is not None or Path(command[0]).is_file()


def opencode_prices(cfg: dict) -> tuple[float, float]:
    table = opencode_config(cfg)
    try:
        prices = float(table.get("price_in") or 0), float(table.get("price_out") or 0)
    except (TypeError, ValueError) as e:
        raise ValueError("workers.opencode prices must be non-negative numbers") from e
    if any(x < 0 for x in prices):
        raise ValueError("workers.opencode prices must be non-negative numbers")
    return prices


def opencode_usd_budget_known(cfg: dict) -> bool:
    table = opencode_config(cfg)
    declared = table.get("cost_known", False)
    if not isinstance(declared, bool):
        raise ValueError("workers.opencode.cost_known must be true or false")
    return declared or any(opencode_prices(cfg))


def opencode_state_paths(cfg: Optional[dict] = None) -> list[Path]:
    if cfg is not None and opencode_config(cfg).get("allow_account_login", False) and saved_credentials_allowed(cfg):
        home = Path.home()
        data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / "opencode"
        cache = Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache") / "opencode"
        state = Path(os.environ.get("XDG_STATE_HOME") or home / ".local" / "state") / "opencode"
        return [data, cache, state]
    return []


def qoder_config(cfg: dict) -> dict:
    return ((cfg or {}).get("workers") or {}).get("qoder") or {}


def qoder_available(cfg: dict) -> bool:
    try:
        command = _command(qoder_config(cfg), "qoder")
    except ValueError:
        return False
    return shutil.which(command[0]) is not None or Path(command[0]).is_file()


def qoder_prices(cfg: dict) -> tuple[float, float]:
    table = qoder_config(cfg)
    try:
        prices = float(table.get("price_in") or 0), float(table.get("price_out") or 0)
    except (TypeError, ValueError) as e:
        raise ValueError("workers.qoder prices must be non-negative numbers") from e
    if any(x < 0 for x in prices):
        raise ValueError("workers.qoder prices must be non-negative numbers")
    return prices


def qoder_usd_budget_known(cfg: dict) -> bool:
    declared = qoder_config(cfg).get("cost_known", False)
    if not isinstance(declared, bool):
        raise ValueError("workers.qoder.cost_known must be true or false")
    return declared or any(qoder_prices(cfg))


def qoder_config_dir(cfg: dict, root: Optional[Path] = None) -> Path:
    raw = qoder_config(cfg).get("config_dir") or os.environ.get("QODER_CONFIG_DIR")
    if not raw:
        raw = str(Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "agentkit" / "qoder")
    path = Path(str(raw)).expanduser()
    return path.resolve() if path.is_absolute() else (root or Path.cwd()).resolve() / path


def qoder_state_paths(cfg: dict, root: Optional[Path] = None) -> list[Path]:
    return [qoder_config_dir(cfg, root)]


def cursor_config(cfg: dict) -> dict:
    return ((cfg or {}).get("workers") or {}).get("cursor") or {}


def cursor_available(cfg: dict) -> bool:
    table = cursor_config(cfg)
    transport = str(table.get("transport") or "cli")
    if transport not in ("cli", "sdk"):
        return False
    if transport == "sdk":
        try:
            return importlib.util.find_spec("cursor_sdk") is not None
        except (ImportError, ValueError):
            return False
    try:
        command = _command(table, "cursor", "agent")
    except ValueError:
        return False
    return shutil.which(command[0]) is not None or Path(command[0]).is_file()


def cursor_prices(cfg: dict) -> tuple[float, float]:
    return 0.0, 0.0


def cursor_usd_budget_known(cfg: dict) -> bool:
    return False


def cursor_config_dir(cfg: dict, root: Optional[Path] = None) -> Path:
    raw = cursor_config(cfg).get("config_dir") or str(Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
                                                     / "agentkit" / "cursor")
    path = Path(str(raw)).expanduser()
    return path.resolve() if path.is_absolute() else (root or Path.cwd()).resolve() / path


def cursor_state_paths(cfg: dict, root: Optional[Path] = None) -> list[Path]:
    return [cursor_config_dir(cfg, root)]


def _capabilities(backend: str, protocol: str, **flags) -> dict:
    return BackendCapabilities(backend=backend, event_protocol=protocol, **flags).to_dict()


def builtin_capabilities() -> dict:
    return _capabilities("builtin", "internal", headless=True, streaming=True, api_key=True,
                         usage_reporting=True, cost_reporting=True, cancel=True, image_input=True)


def backend_capabilities(name: str, cfg: Optional[dict] = None, transport: Optional[str] = None) -> dict:
    if name == "builtin":
        return builtin_capabilities()
    if name == "codex":
        return _capabilities(name, "jsonl", headless=True, streaming=True, session_resume=False,
                             api_key=True, usage_reporting=True, cancel=True)
    if name == "opencode":
        return _capabilities(name, "jsonl", headless=True, streaming=True, session_resume=True,
                             oauth=True, api_key=True, usage_reporting=True, cost_reporting=True, cancel=True)
    if name == "qoder":
        return _capabilities(name, "jsonl", headless=True, streaming=True, session_resume=True,
                             oauth=True, api_key=True, usage_reporting=True, credits_reporting=True, cancel=True)
    if name == "cursor":
        sdk = transport == "sdk" or (transport is None and
                                      str(cursor_config(cfg or {}).get("transport") or "cli") == "sdk")
        return _capabilities(name, "sdk" if sdk else "jsonl", headless=True, streaming=True,
                             session_resume=True, api_key=True, usage_reporting=sdk,
                             cost_reporting=sdk, cancel=True)
    raise ValueError(f"unknown worker backend: {name}")


def cursor_settings() -> dict:
    return {"permissions": {
        "allow": [],
        "deny": ["Shell(*)", "WebFetch(*)", "Mcp(*:*)",
                 "Read(.env*)", "Read(**/.env*)", "Read(**/.ssh/**)", "Read(**/.aws/**)",
                 "Read(**/.cursor/**)", "Read(**/.qoder/**)",
                 "Write(.env*)", "Write(**/.env*)", "Write(.git/**)", "Write(.agentkit/**)",
                 "Write(**/.cursor/**)", "Write(**/.qoder/**)"],
    }}


def qoder_settings() -> dict:
    tools = ["Read", "Edit", "Write", "Grep", "Glob"]
    return {
        "general": {"enableAutoUpdate": False, "defaultPermissionMode": "dont_ask", "maxAttempts": 1,
                    "retryFetchErrors": False, "fileCheckpointing": {"enabled": False},
                    "plan": {"enabled": False}},
        "permissions": {
            "allow": tools,
            "deny": ["Bash", "WebFetch", "WebSearch", "Agent", "mcp__*", "Read(*.env)",
                     "Read(*.env.*)", "Read(*.pem)", "Read(*.key)", "Read(~/.ssh/**)",
                     "Read(~/.aws/**)", "Read(~/.qoder/**)", "Read(~/.config/agentkit/**)",
                     "Edit(*.env)", "Edit(*.env.*)",
                     "Edit(~/.ssh/**)", "Edit(~/.aws/**)", "Edit(~/.qoder/**)",
                     "Edit(~/.config/agentkit/**)", "Edit(/.git/**)", "Edit(/.qoder/**)"],
            "additionalDirectories": [],
        },
        "tools": {"core": tools},
        "mcpServers": {},
        "mcp": {"allowed": [], "enableAllProjectMcpServers": False, "enabledProjectMcpServers": []},
        "privacy": {"usageStatisticsEnabled": False},
        "aiCodeStatistics": {"enabled": False},
        "security": {"disableYoloMode": True, "environmentVariableRedaction": {"enabled": True}},
        "autoMemoryEnabled": False,
        "enabledPlugins": {},
    }


def opencode_policy(allow_bash: bool = False) -> dict:
    read = {"*": "allow", "*.env": "deny", "*.env.*": "deny", "*/.ssh/*": "deny"}
    edit = {"*": "allow", "*.env": "deny", "*.env.*": "deny", "*/.git/*": "deny",
            "*/.agentkit/*": "deny"}
    bash = {"*": "deny"}
    if allow_bash:
        bash = {"*": "allow", "*git commit*": "deny", "*git push*": "deny"}
    return {
        "*": "deny",
        "read": read,
        "edit": edit,
        "glob": "allow",
        "grep": "allow",
        "list": "allow",
        "bash": bash,
        "todowrite": "allow",
        "external_directory": "deny",
        "task": "deny",
        "webfetch": "deny",
        "websearch": "deny",
        "lsp": "deny",
        "skill": "deny",
        "doom_loop": "deny",
        "question": "deny",
        "plan_enter": "deny",
        "plan_exit": "deny",
    }


def opencode_v2_settings(allow_bash: bool = False) -> dict:
    permissions = [
        {"action": "*", "resource": "*", "effect": "allow"},
        {"action": "read", "resource": "*.env", "effect": "deny"},
        {"action": "read", "resource": "*/.env*", "effect": "deny"},
        {"action": "read", "resource": "*/.ssh/*", "effect": "deny"},
        {"action": "read", "resource": "*/.aws/*", "effect": "deny"},
        {"action": "edit", "resource": "*.env", "effect": "deny"},
        {"action": "edit", "resource": "*/.env*", "effect": "deny"},
        {"action": "edit", "resource": "*/.git/*", "effect": "deny"},
        {"action": "edit", "resource": "*/.agentkit/*", "effect": "deny"},
        {"action": "external_directory", "resource": "*", "effect": "deny"},
    ]
    policies = [
        {"action": "tool.use", "resource": "webfetch:*", "effect": "deny"},
        {"action": "tool.use", "resource": "websearch:*", "effect": "deny"},
        {"action": "tool.use", "resource": "subagent:*", "effect": "deny"},
        {"action": "tool.use", "resource": "external_directory:*", "effect": "deny"},
        {"action": "mcp.use", "resource": "*", "effect": "deny"},
        {"action": "skill.use", "resource": "*", "effect": "deny"},
        {"action": "plugin.use", "resource": "*", "effect": "deny"},
        {"action": "websearch.use", "resource": "*", "effect": "deny"},
    ]
    if not allow_bash:
        policies.insert(0, {"action": "tool.use", "resource": "shell:*", "effect": "deny"})
    else:
        policies += [{"action": "tool.use", "resource": "shell:git commit*", "effect": "deny"},
                     {"action": "tool.use", "resource": "shell:git push*", "effect": "deny"}]
    return {"$schema": "https://opencode.ai/config.json", "update": "disable", "snapshot": False,
            "formatter": False, "default_agent": "build", "permissions": permissions, "policies": policies}


def _inject_credential(env: dict, key_env: str, value: str) -> dict:
    if key_env and value:
        env[key_env] = value
    return env


def _failure(message: str) -> Outcome:
    text = message.lower()
    if re.search(r"\b(401|403)\b|unauthorized|invalid.*key|authentication|login expired|not authenticated", text):
        return Outcome("failed", message[:300], code="AK-MODEL-AUTH")
    if re.search(r"\b429\b|rate.?limit|quota", text):
        return Outcome("paused", message[:300], code="AK-MODEL-429")
    if "timeout" in text or "timed out" in text:
        return Outcome("paused", message[:300], code="AK-MODEL-TIMEOUT")
    if re.search(r"network|connection|fetch failed|dns|socket", text):
        return Outcome("paused", message[:300], code="AK-MODEL-NETWORK")
    if re.search(r"\b5\d\d\b|overloaded|unavailable|server error", text):
        return Outcome("paused", message[:300], code="AK-MODEL-UNAVAILABLE")
    return Outcome("failed", message[:300], code="AK-MODEL-BAD-RESPONSE")


class CodexBackend:
    name = "codex"

    def capabilities(self) -> dict:
        return backend_capabilities(self.name)

    def __init__(self, cfg: dict, task: dict, worktree: Path, task_dir: Path, *,
                 credential_env: str = "", credential_value: str = "",
                 on_progress: Callable[[dict], None] = lambda d: None,
                 check_stop: Callable[[], Optional[str]] = lambda: None,
                 events=None, max_rounds: int = 40):
        self.cfg = codex_config(cfg)
        self.credential_env = credential_env
        self.credential_value = credential_value
        self.task = task
        self.worktree = worktree
        self.task_dir = task_dir
        self.on_progress = on_progress
        self.check_stop = check_stop
        self.events = events
        self.max_rounds = max(1, int(max_rounds))
        self.price_in, self.price_out = codex_prices(cfg)
        prior = task.get("prior") or {}
        self.tokens_in = int(prior.get("tokens_in") or 0)
        self.tokens_out = int(prior.get("tokens_out") or 0)
        self.usd = float(prior.get("usd") or 0)
        self.prior_tokens_in = self.tokens_in
        self.prior_tokens_out = self.tokens_out
        self.prior_usd = self.usd
        self.cost_known = bool(self.price_in or self.price_out) and bool(task.get("cost_known", True))
        self.rounds = int(prior.get("rounds") or 0)
        self.compactions = 0
        self.repairs = 0
        self.denied: list = []
        self.last_component = "codex"
        self.last_step = 0
        self.thread_id = ""
        self.turn_id = ""
        self.item_id = ""
        self.claim = ""
        self.completed = False
        self.error = ""

    def stats(self) -> dict:
        return {"rounds": self.rounds, "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                "usd": round(self.usd, 6), "cost_known": self.cost_known,
                "compactions": self.compactions, "repairs": self.repairs,
                "compression": {"deduped": 0, "aged": 0, "saved_chars": 0, "style": "off", "backend": "codex"}}

    def _ev(self, event: str, **extra):
        self.last_step = self.rounds
        extra.setdefault("session_id", self.thread_id or None)
        extra.setdefault("turn_id", self.turn_id or None)
        extra.setdefault("item_id", self.item_id or None)
        if self.events is not None:
            self.events.emit(event, component="codex", step=self.rounds, **extra)

    def _prompt(self, brief: str, resume: Optional[str]) -> str:
        task = self.task
        lines = [
            "You are an AgentKit external worker running in an isolated git worktree.",
            "Make the smallest change that satisfies the goal. Do not commit or push.",
            "Run the acceptance checks yourself before finishing. AgentKit will independently re-run them.",
            "",
            "GOAL:",
            task["goal"],
            "",
            "ACCEPTANCE CHECKS:",
            *[f"- {c}" for c in task.get("checks") or []],
            "",
            "ALLOWED PATHS: " + (", ".join(task.get("allow") or []) or "(any)"),
            "",
            "PROJECT MEMORY (brief):",
            brief or "(none)",
        ]
        if resume:
            lines += ["", "RESUME CONTEXT:", resume]
        return "\n".join(lines) + "\n"

    def _over_budget(self) -> Optional[str]:
        budget = self.task.get("budget") or {}
        if budget.get("tokens") and self.tokens_in + self.tokens_out >= int(budget["tokens"]):
            return "budget"
        if budget.get("usd") and self.cost_known and self.usd >= float(budget["usd"]):
            return "budget"
        return None

    def _consume(self, event: dict):
        kind = event.get("type")
        if kind == "thread.started":
            self.thread_id = str(event.get("thread_id") or "")
            self._ev("backend_thread", thread_id=self.thread_id)
        elif kind == "turn.started":
            self.rounds += 1
            self.turn_id = str(event.get("turn_id") or event.get("turnId") or
                               f"{self.thread_id or 'codex'}:turn:{self.rounds}")
            self.item_id = ""
            self._ev("model_call")
            self.on_progress(self.stats())
            if self.rounds > self.max_rounds:
                return Outcome("stopped", f"max rounds ({self.max_rounds})", stopped="rounds", code="AK-WORKER-ROUNDS")
        elif kind == "turn.completed":
            usage = event.get("usage") or {}
            current_in = int(usage.get("input_tokens") or 0)
            current_out = int(usage.get("output_tokens") or 0)
            self.tokens_in = self.prior_tokens_in + current_in
            self.tokens_out = self.prior_tokens_out + current_out
            if self.price_in or self.price_out:
                self.usd = self.prior_usd + (current_in * self.price_in + current_out * self.price_out) / 1_000_000
                self.cost_known = True
            self.completed = True
            self.item_id = ""
            self._ev("model_usage", input_tokens=self.tokens_in, output_tokens=self.tokens_out,
                     cached_input_tokens=usage.get("cached_input_tokens", 0),
                     reasoning_output_tokens=usage.get("reasoning_output_tokens", 0))
            self.on_progress(self.stats())
            over = self._over_budget()
            if over:
                return Outcome("stopped", "budget exceeded", stopped=over, code="AK-BUDGET")
        elif kind == "item.completed":
            item = event.get("item") or {}
            self.item_id = str(item.get("id") or event.get("item_id") or event.get("id") or "")
            item_kind = item.get("type")
            if item_kind == "agent_message":
                self.claim = str(item.get("text") or "")[:500]
            elif item_kind == "error":
                self.error = str(item.get("message") or self.error or "codex item error")
            elif item_kind == "command_execution" and item.get("status") == "declined":
                self.denied.append(str(item.get("command") or "")[:200])
            self._ev("tool", tool=item_kind or "unknown", status=item.get("status"))
        elif kind == "turn.failed":
            self.error = str((event.get("error") or {}).get("message") or "codex turn failed")
        elif kind == "error":
            self.error = str(event.get("message") or self.error or "codex error")
        return None

    @staticmethod
    def _kill(proc: subprocess.Popen):
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGTERM)
            elif proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=2)
        except (ProcessLookupError, subprocess.TimeoutExpired, OSError):
            try:
                if os.name == "posix":
                    os.killpg(proc.pid, signal.SIGKILL)
                elif proc.poll() is None:
                    proc.kill()
            except (ProcessLookupError, OSError):
                pass

    def run(self, brief: str, resume: Optional[str] = None) -> Outcome:
        schema = self.task_dir / "codex-output-schema.json"
        final = self.task_dir / "codex-final.json"
        event_log = self.task_dir / "codex-events.jsonl"
        err_log = self.task_dir / "codex-stderr.log"
        schema.write_text(json.dumps({"type": "object", "properties": {"summary": {"type": "string"}},
                                      "required": ["summary"], "additionalProperties": False}), encoding="utf-8")
        command = _command(self.cfg)
        args = [*command, "exec", "-C", str(self.worktree)]
        model = str(self.task.get("model") or self.cfg.get("model") or "").strip()
        if model:
            args += ["-m", model]
        sandbox = str(self.cfg.get("sandbox") or "workspace-write")
        if sandbox not in ("read-only", "workspace-write", "danger-full-access"):
            raise ValueError("workers.codex.sandbox must be read-only, workspace-write, or danger-full-access")
        args += ["-s", sandbox, "--json", "--output-schema", str(schema), "-o", str(final), "-"]
        stopped = self.check_stop()
        if stopped:
            return Outcome("stopped", f"stopped: {stopped}", stopped=stopped,
                           code="AK-TASK-CANCELLED" if stopped == "cancelled" else "AK-BUDGET")
        env = _inject_credential(dict(os.environ), self.credential_env, self.credential_value)
        with open(err_log, "wb") as err, open(event_log, "a", encoding="utf-8") as transcript:
            proc = subprocess.Popen(args, cwd=str(self.worktree), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=err, env=env, start_new_session=os.name == "posix")
            try:
                try:
                    proc.stdin.write(self._prompt(brief, resume).encode("utf-8"))
                    proc.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
                for raw in proc.stdout:
                    stop = self.check_stop()
                    if stop:
                        return Outcome("stopped", f"stopped: {stop}", stopped=stop,
                                       code="AK-TASK-CANCELLED" if stop == "cancelled" else "AK-BUDGET")
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    transcript.write(redact_text(line) + "\n")
                    transcript.flush()
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    outcome = self._consume(event)
                    if outcome:
                        return outcome
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._kill(proc)
                    return Outcome("paused", "codex exec did not exit after its event stream", code="AK-MODEL-TIMEOUT")
            finally:
                self._kill(proc)
        if self.error:
            return _failure(self.error)
        if proc.returncode != 0:
            tail = err_log.read_text(encoding="utf-8", errors="replace")[-500:]
            return Outcome("failed", f"codex exec exited {proc.returncode}: {tail}", code="AK-WORKER-CRASH")
        if not self.completed:
            return Outcome("failed", "codex exec ended without turn.completed", code="AK-MODEL-BAD-RESPONSE")
        if not self.claim and final.exists():
            try:
                data = json.loads(final.read_text(encoding="utf-8"))
                self.claim = str(data.get("summary") or "")[:500]
            except (OSError, ValueError, AttributeError):
                pass
        elif self.claim:
            try:
                data = json.loads(self.claim)
                self.claim = str(data.get("summary") or self.claim)[:500]
            except (ValueError, AttributeError):
                pass
        return Outcome("submitted", "codex exec completed", claim=self.claim)


class OpenCodeBackend:
    name = "opencode"

    def capabilities(self) -> dict:
        return backend_capabilities(self.name)

    def __init__(self, cfg: dict, task: dict, worktree: Path, task_dir: Path, *,
                 credential_env: str = "", credential_value: str = "",
                 on_progress: Callable[[dict], None] = lambda d: None,
                 check_stop: Callable[[], Optional[str]] = lambda: None,
                 events=None, max_rounds: int = 40):
        self.cfg = opencode_config(cfg)
        self.credential_env = credential_env
        self.credential_value = credential_value
        self.task = task
        self.worktree = worktree
        self.task_dir = task_dir
        self.on_progress = on_progress
        self.check_stop = check_stop
        self.events = events
        self.max_rounds = max(1, int(max_rounds))
        self.price_in, self.price_out = opencode_prices(cfg)
        prior = task.get("prior") or {}
        self.tokens_in = int(prior.get("tokens_in") or 0)
        self.tokens_out = int(prior.get("tokens_out") or 0)
        self.usd = float(prior.get("usd") or 0)
        self.cost_known = bool(task.get("cost_known", True))
        self.declared_cost_known = self.cfg.get("cost_known", False)
        if not isinstance(self.declared_cost_known, bool):
            raise ValueError("workers.opencode.cost_known must be true or false")
        self.allow_bash = self.cfg.get("allow_bash", False)
        if not isinstance(self.allow_bash, bool):
            raise ValueError("workers.opencode.allow_bash must be true or false")
        self.version = str(self.cfg.get("version") or "v1")
        if self.version not in ("v1", "v2"):
            raise ValueError("workers.opencode.version must be v1 or v2")
        self.allow_account_login = self.cfg.get("allow_account_login", False)
        if not isinstance(self.allow_account_login, bool):
            raise ValueError("workers.opencode.allow_account_login must be true or false")
        self.allow_account_login = self.allow_account_login and saved_credentials_allowed(cfg)
        self.rounds = int(prior.get("rounds") or 0)
        self.compactions = 0
        self.repairs = 0
        self.denied: list = []
        self.last_component = "opencode"
        self.last_step = 0
        self.session_id = str(task.get("backend_session") or "")
        self.turn_id = ""
        self.item_id = ""
        self.claim = ""
        self.completed = False
        self.error = ""
        self.seen_steps: set[str] = set()

    def stats(self) -> dict:
        return {"rounds": self.rounds, "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                "usd": round(self.usd, 6), "cost_known": self.cost_known,
                "compactions": self.compactions, "repairs": self.repairs,
                "compression": {"deduped": 0, "aged": 0, "saved_chars": 0, "style": "off", "backend": "opencode"}}

    def _ev(self, event: str, **extra):
        self.last_step = self.rounds
        extra.setdefault("session_id", self.session_id or None)
        extra.setdefault("turn_id", self.turn_id or None)
        extra.setdefault("item_id", self.item_id or None)
        if self.events is not None:
            self.events.emit(event, component="opencode", step=self.rounds, **extra)

    def _prompt(self, brief: str, resume: Optional[str]) -> str:
        task = self.task
        lines = [
            "You are an AgentKit external worker running in an isolated git worktree.",
            "Make the smallest change that satisfies the goal. Do not commit or push.",
            "Use read/edit/write/glob/grep tools. AgentKit independently runs the acceptance checks.",
            "",
            "GOAL:",
            task["goal"],
            "",
            "ACCEPTANCE CHECKS:",
            *[f"- {c}" for c in task.get("checks") or []],
            "",
            "ALLOWED PATHS: " + (", ".join(task.get("allow") or []) or "(any)"),
            "",
            "PROJECT MEMORY (brief):",
            brief or "(none)",
        ]
        if self.allow_bash:
            lines.insert(3, "Shell access is enabled inside the OS sandbox; never read credentials or access external directories.")
        if resume:
            lines += ["", "RESUME CONTEXT:", resume]
        return "\n".join(lines) + "\n"

    def _over_budget(self) -> Optional[str]:
        budget = self.task.get("budget") or {}
        if budget.get("tokens") and self.tokens_in + self.tokens_out >= int(budget["tokens"]):
            return "budget"
        if budget.get("usd") and self.cost_known and self.usd >= float(budget["usd"]):
            return "budget"
        return None

    @staticmethod
    def _number(value) -> float:
        try:
            number = float(value or 0)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, number) if math.isfinite(number) else 0.0

    @staticmethod
    def _error_text(error) -> str:
        if isinstance(error, dict):
            data = error.get("data") or {}
            message = data.get("message") if isinstance(data, dict) else None
            return str(message or error.get("message") or error.get("name") or error)
        return str(error)

    def _consume(self, event: dict):
        kind = event.get("type")
        if event.get("sessionID") and str(event["sessionID"]) != self.session_id:
            self.session_id = str(event["sessionID"])
            self._ev("backend_session", session_id=self.session_id)
        if kind == "step_start":
            self.rounds += 1
            part = event.get("part") or {}
            self.turn_id = str(part.get("id") or event.get("turn_id") or
                               f"{self.session_id or 'opencode'}:step:{self.rounds}")
            self.item_id = ""
            self._ev("model_call")
            self.on_progress(self.stats())
            if self.rounds > self.max_rounds:
                return Outcome("stopped", f"max rounds ({self.max_rounds})", stopped="rounds", code="AK-WORKER-ROUNDS")
        elif kind == "step_finish":
            part = event.get("part") or {}
            part_id = str(part.get("id") or "")
            if part_id and part_id in self.seen_steps:
                return None
            if part_id:
                self.seen_steps.add(part_id)
            tokens = part.get("tokens") or {}
            cache = tokens.get("cache") or {}
            current_in = int(self._number(tokens.get("input")) + self._number(cache.get("read")) +
                             self._number(cache.get("write")))
            current_out = int(self._number(tokens.get("output")) + self._number(tokens.get("reasoning")))
            self.tokens_in += current_in
            self.tokens_out += current_out
            reported = self._number(part.get("cost"))
            if reported > 0:
                self.usd += reported
                self.cost_known = True
            elif self.price_in or self.price_out:
                self.usd += (current_in * self.price_in + current_out * self.price_out) / 1_000_000
                self.cost_known = True
            elif not self.declared_cost_known:
                self.cost_known = False
            self.completed = True
            self.item_id = ""
            self._ev("model_usage", input_tokens=self.tokens_in, output_tokens=self.tokens_out,
                     cached_input_tokens=int(self._number(cache.get("read"))),
                     reasoning_output_tokens=int(self._number(tokens.get("reasoning"))), usd=round(self.usd, 6))
            self.on_progress(self.stats())
            over = self._over_budget()
            if over:
                return Outcome("stopped", "budget exceeded", stopped=over, code="AK-BUDGET")
        elif kind == "text":
            part = event.get("part") or {}
            self.item_id = str(part.get("id") or event.get("item_id") or self.item_id)
            text = str(part.get("text") or "").strip()
            if text:
                self.claim = text[:500]
        elif kind == "tool_use":
            part = event.get("part") or {}
            self.item_id = str(part.get("id") or event.get("item_id") or self.item_id)
            state = part.get("state") or {}
            status = state.get("status")
            if status == "error":
                self.denied.append(str(state.get("error") or part.get("tool") or "tool error")[:200])
            self._ev("tool", tool=part.get("tool") or "unknown", status=status)
        elif kind == "error":
            error = event.get("error") or {}
            message = self._error_text(error) or "opencode error"
            if str(error.get("type") or "") == "provider.free-limit" or error.get("status") == 429:
                message = "429 rate limit: " + message
            self.error = message
        return None

    def run(self, brief: str, resume: Optional[str] = None) -> Outcome:
        command = _command(self.cfg, "opencode")
        model = str(self.task.get("model") or self.cfg.get("model") or "").strip()
        if model and (len(model.split("/", 1)) != 2 or not all(model.split("/", 1))):
            raise ValueError("workers.opencode.model must use provider/model format")
        agent = str(self.cfg.get("agent") or "build").strip()
        if not agent:
            raise ValueError("workers.opencode.agent must not be empty")
        config_dir = self.task_dir / "opencode-config"
        tmp_dir = self.task_dir / "opencode-tmp"
        config_dir.mkdir(parents=True, exist_ok=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        env = _inject_credential(dict(os.environ), self.credential_env, self.credential_value)
        env.pop("OPENCODE_CONFIG", None)
        env.update({"XDG_CONFIG_HOME": str(config_dir), "OPENCODE_CONFIG_DIR": str(config_dir),
                    "OPENCODE_DISABLE_PROJECT_CONFIG": "1", "OPENCODE_PURE": "1",
                    "OPENCODE_DISABLE_AUTOUPDATE": "1", "TMPDIR": str(tmp_dir)})
        if self.version == "v2":
            config = opencode_v2_settings(self.allow_bash)
            v2_config = config_dir / "opencode" / "opencode.json"
            v2_config.parent.mkdir(parents=True, exist_ok=True)
            v2_config.write_text(json.dumps(config, separators=(",", ":")), encoding="utf-8")
            env.pop("OPENCODE_CONFIG_CONTENT", None)
            env.pop("OPENCODE_PERMISSION", None)
            args = [*command, "--standalone", "run", "--format", "json", "--agent", agent]
        else:
            policy = opencode_policy(self.allow_bash)
            config = {"$schema": "https://opencode.ai/config.json", "share": "disabled", "autoupdate": False,
                      "snapshot": False, "formatter": False, "lsp": False, "default_agent": "build",
                      "permission": policy, "agent": {"build": {"mode": "primary", "permission": policy}}}
            env.update({"OPENCODE_CONFIG_CONTENT": json.dumps(config, separators=(",", ":")),
                        "OPENCODE_PERMISSION": json.dumps(policy, separators=(",", ":"))})
            args = [*command, "run", "--format", "json", "--dir", str(self.worktree), "--agent", agent]
        if not self.allow_account_login:
            data_dir = self.task_dir / "opencode-data"
            cache_dir = self.task_dir / "opencode-cache"
            state_dir = self.task_dir / "opencode-state"
            for path in (data_dir, cache_dir, state_dir):
                path.mkdir(parents=True, exist_ok=True)
            env.pop("OPENCODE_DB", None)
            env.update({"XDG_DATA_HOME": str(data_dir), "XDG_CACHE_HOME": str(cache_dir),
                        "XDG_STATE_HOME": str(state_dir)})
        if model:
            args += ["--model", model]
        if self.session_id:
            args += ["--session", self.session_id]
        stopped = self.check_stop()
        if stopped:
            return Outcome("stopped", f"stopped: {stopped}", stopped=stopped,
                           code="AK-TASK-CANCELLED" if stopped == "cancelled" else "AK-BUDGET")
        event_log = self.task_dir / "opencode-events.jsonl"
        err_log = self.task_dir / "opencode-stderr.log"
        with open(err_log, "wb") as err, open(event_log, "a", encoding="utf-8") as transcript:
            proc = subprocess.Popen(args, cwd=str(self.worktree), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=err, env=env, start_new_session=os.name == "posix")
            try:
                try:
                    proc.stdin.write(self._prompt(brief, resume).encode("utf-8"))
                    proc.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
                for raw in proc.stdout:
                    stop = self.check_stop()
                    if stop:
                        return Outcome("stopped", f"stopped: {stop}", stopped=stop,
                                       code="AK-TASK-CANCELLED" if stop == "cancelled" else "AK-BUDGET")
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    transcript.write(redact_text(line) + "\n")
                    transcript.flush()
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    outcome = self._consume(event)
                    if outcome:
                        return outcome
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    CodexBackend._kill(proc)
                    return Outcome("paused", "opencode run did not exit after its event stream", code="AK-MODEL-TIMEOUT")
            finally:
                CodexBackend._kill(proc)
        if self.error:
            return _failure(self.error)
        if proc.returncode != 0:
            tail = err_log.read_text(encoding="utf-8", errors="replace")[-500:]
            return _failure(tail or f"opencode run exited {proc.returncode}")
        if not self.completed:
            return Outcome("failed", "opencode run ended without step_finish", code="AK-MODEL-BAD-RESPONSE")
        return Outcome("submitted", "opencode run completed", claim=self.claim)


def _qoder_failure(code, message: str) -> Outcome:
    text = message.lower()
    if code == 105 or "login" in text or "access token" in text or "unauthorized" in text:
        return Outcome("failed", message[:300], code="AK-MODEL-AUTH")
    if code in (110, 113, 114, 115, 116, 117, 118, 119, 122) or re.search(r"\b429\b|quota|rate.?limit", text):
        return Outcome("paused", message[:300], code="AK-MODEL-429")
    if code == 10408 or "timed out" in text or "timeout" in text:
        return Outcome("paused", message[:300], code="AK-MODEL-TIMEOUT")
    if code == 500 or re.search(r"network|connection|fetch failed|dns|socket", text):
        return Outcome("paused", message[:300], code="AK-MODEL-NETWORK")
    if code in (10500, 10605) or re.search(r"\b5\d\d\b|overloaded|unavailable|server error", text):
        return Outcome("paused", message[:300], code="AK-MODEL-UNAVAILABLE")
    return Outcome("failed", message[:300], code="AK-MODEL-BAD-RESPONSE")


class QoderBackend:
    name = "qoder"

    def capabilities(self) -> dict:
        return backend_capabilities(self.name)

    def __init__(self, cfg: dict, task: dict, worktree: Path, task_dir: Path, *,
                 root: Optional[Path] = None, credential_env: str = "", credential_value: str = "",
                 on_progress: Callable[[dict], None] = lambda d: None,
                 check_stop: Callable[[], Optional[str]] = lambda: None,
                 events=None, max_rounds: int = 40):
        self.cfg = qoder_config(cfg)
        self.credential_env = credential_env
        self.credential_value = credential_value
        self.task = task
        self.worktree = worktree
        self.task_dir = task_dir
        self.root = root or worktree
        self.on_progress = on_progress
        self.check_stop = check_stop
        self.events = events
        self.max_rounds = max(1, int(max_rounds))
        self.price_in, self.price_out = qoder_prices(cfg)
        prior = task.get("prior") or {}
        self.prior_rounds = int(prior.get("rounds") or 0)
        self.rounds = self.prior_rounds
        self.tokens_in = int(prior.get("tokens_in") or 0)
        self.tokens_out = int(prior.get("tokens_out") or 0)
        self.usd = float(prior.get("usd") or 0)
        self.prior_usd = self.usd
        self.credits = float(prior.get("credits") or 0)
        self.credits_known = False
        self.credits_seen = False
        self.cost_known = bool(self.cfg.get("cost_known", False)) or bool(self.price_in or self.price_out)
        self.session_id = str(task.get("backend_session") or "")
        self.turn_id = ""
        self.item_id = ""
        self.qodercli_version = ""
        self.initialized = False
        self.claim = ""
        self.completed = False
        self.error = ""
        self.denied: list = []
        self.compactions = 0
        self.repairs = 0
        self.last_component = "qoder"
        self.last_step = 0

    def stats(self) -> dict:
        return {"rounds": self.rounds, "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                "usd": round(self.usd, 6), "cost_known": self.cost_known,
                "credits": round(self.credits, 6), "credits_known": self.credits_known,
                "compactions": self.compactions, "repairs": self.repairs,
                "compression": {"deduped": 0, "aged": 0, "saved_chars": 0, "style": "off", "backend": "qoder"}}

    def _ev(self, event: str, **extra):
        self.last_step = self.rounds
        extra.setdefault("session_id", self.session_id or None)
        extra.setdefault("turn_id", self.turn_id or None)
        extra.setdefault("item_id", self.item_id or None)
        if self.events is not None:
            self.events.emit(event, component="qoder", step=self.rounds, **extra)

    def _prompt(self, brief: str, resume: Optional[str]) -> str:
        task = self.task
        lines = [
            "You are an AgentKit external worker running in an isolated git worktree.",
            "Make the smallest change that satisfies the goal. Do not commit or push.",
            "Use Read/Edit/Write/Grep/Glob only. AgentKit independently runs the acceptance checks.",
            "",
            "GOAL:",
            task["goal"],
            "",
            "ACCEPTANCE CHECKS:",
            *[f"- {c}" for c in task.get("checks") or []],
            "",
            "ALLOWED PATHS: " + (", ".join(task.get("allow") or []) or "(any)"),
            "",
            "PROJECT MEMORY (brief):",
            brief or "(none)",
        ]
        if resume:
            lines += ["", "RESUME CONTEXT:", resume]
        return "\n".join(lines) + "\n"

    def _over_budget(self) -> Optional[str]:
        budget = self.task.get("budget") or {}
        if budget.get("tokens") and self.tokens_in + self.tokens_out >= int(budget["tokens"]):
            return "budget"
        if budget.get("usd") and self.cost_known and self.usd >= float(budget["usd"]):
            return "budget"
        return None

    @staticmethod
    def _number(value) -> float:
        try:
            number = float(value or 0)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, number) if math.isfinite(number) else 0.0

    def _consume(self, event: dict):
        kind = event.get("type")
        session = str(event.get("session_id") or "")
        if session and session != self.session_id:
            self.session_id = session
            self._ev("backend_session", session_id=session)
        if kind == "system" and event.get("subtype") == "init":
            self.qodercli_version = str(event.get("qodercli_version") or "")
            mode = str(event.get("permissionMode") or "").lower().replace("_", "")
            tools = set(event.get("tools") or [])
            allowed = {"Read", "Edit", "Write", "Grep", "Glob"}
            if mode != "dontask" or tools != allowed or event.get("mcp_servers"):
                self.error = "qoder policy verification failed: unexpected tools, permissions or MCP remained enabled"
                return Outcome("failed", self.error, code="AK-CONFIG")
            self.initialized = True
            cwd = event.get("cwd")
            if cwd and Path(str(cwd)).resolve() != self.worktree.resolve():
                self.error = "qoder started outside the AgentKit worktree"
                return Outcome("failed", self.error, code="AK-CONFIG")
            self._ev("backend_init", qodercli_version=self.qodercli_version, tools=sorted(tools))
        elif kind == "assistant":
            self.rounds += 1
            message = event.get("message") or {}
            self.turn_id = str(event.get("uuid") or message.get("id") or
                               f"{self.session_id or 'qoder'}:turn:{self.rounds}")
            self.item_id = ""
            self._ev("model_call")
            self.on_progress(self.stats())
            if self.rounds > self.max_rounds:
                return Outcome("stopped", f"max rounds ({self.max_rounds})", stopped="rounds", code="AK-WORKER-ROUNDS")
            for part in message.get("content") or []:
                if part.get("type") == "text":
                    self.item_id = str(part.get("id") or self.item_id)
                    self.claim = str(part.get("text") or "")[:500]
                elif part.get("type") == "tool_use":
                    self.item_id = str(part.get("id") or self.item_id)
                    self._ev("tool", tool=part.get("name") or "unknown", status="completed")
            usage = message.get("usage")
            if not isinstance(usage, dict):
                return None
            current_in = int(self._number(usage.get("input_tokens")))
            current_out = int(self._number(usage.get("output_tokens")))
            self.tokens_in += current_in
            self.tokens_out += current_out
            if self.price_in or self.price_out:
                self.usd += (current_in * self.price_in + current_out * self.price_out) / 1_000_000
                self.cost_known = True
            if usage.get("credits") is None:
                self.credits_known = False
            else:
                if not self.credits_seen:
                    self.credits_known = True
                self.credits_seen = True
                self.credits += self._number(usage.get("credits"))
            self.item_id = ""
            self._ev("model_usage", input_tokens=self.tokens_in, output_tokens=self.tokens_out,
                     cached_input_tokens=int(self._number(usage.get("cache_read_input_tokens"))),
                     credits=round(self.credits, 6), credits_known=self.credits_known)
            self.on_progress(self.stats())
            over = self._over_budget()
            if over:
                return Outcome("stopped", "budget exceeded", stopped=over, code="AK-BUDGET")
        elif kind == "system" and event.get("subtype") == "permission_denied":
            self.denied.append(str(event.get("tool_name") or event.get("message") or "permission denied")[:200])
        elif kind == "result":
            for denial in event.get("permission_denials") or []:
                self.denied.append(str(denial.get("tool_name") or "permission denied")[:200])
            turns = int(self._number(event.get("num_turns")))
            self.rounds = max(self.rounds, self.prior_rounds + turns)
            if not (self.price_in or self.price_out):
                reported = event.get("total_cost_usd", event.get("totalCostUsd"))
                if reported is not None:
                    self.usd = self.prior_usd + self._number(reported)
                    self.cost_known = self._number(reported) > 0 or bool(self.cfg.get("cost_known", False))
            if event.get("subtype") == "error_max_turns":
                return Outcome("stopped", "qoder reached max turns", stopped="rounds", code="AK-WORKER-ROUNDS")
            if event.get("subtype") != "success" or event.get("is_error"):
                code = event.get("error_code")
                errors = event.get("errors") or []
                message = "; ".join(str(x) for x in errors) or str(event.get("result") or "qoder execution failed")
                return _qoder_failure(code, message)
            self.completed = True
            self.claim = str(event.get("result") or self.claim)[:500]
            over = self._over_budget()
            if over:
                return Outcome("stopped", "budget exceeded", stopped=over, code="AK-BUDGET")
        return None

    def run(self, brief: str, resume: Optional[str] = None) -> Outcome:
        command = _command(self.cfg, "qoder")
        model = str(self.task.get("model") or self.cfg.get("model") or "").strip()
        retries = int(self.cfg.get("max_retries", 0))
        if retries < 0 or retries > 10:
            raise ValueError("workers.qoder.max_retries must be between 0 and 10")
        config_dir = qoder_config_dir({"workers": {"qoder": self.cfg}}, self.root)
        config_dir.mkdir(parents=True, exist_ok=True)
        mcp_config = self.task_dir / "qoder-mcp.json"
        mcp_config.write_text('{"mcpServers":{}}', encoding="utf-8")
        settings = json.dumps(qoder_settings(), separators=(",", ":"))
        tools = "Read,Edit,Write,Grep,Glob"
        denied = "Bash,WebFetch,WebSearch,Agent,mcp__*"
        args = [*command, "--print", "--output-format", "stream-json", "--input-format", "text",
                "--cwd", str(self.worktree), "--config-dir", str(config_dir),
                "--permission-mode", "dont_ask", "--setting-sources", "user", "--settings", settings,
                "--strict-mcp-config", "--mcp-config", str(mcp_config), "--tools", tools,
                "--allowed-tools", tools, "--disallowed-tools", denied,
                "--max-model-request-retries", str(retries)]
        if model:
            args += ["--model", model]
        if self.session_id:
            args += ["--resume", self.session_id]
        env = dict(os.environ)
        for key in ("QODER_MODEL", "QODER_SESSION_ID", "QODER_PERMISSION_MODE", "QODER_APPEND_SYSTEM_PROMPT"):
            env.pop(key, None)
        env.update({"QODER_CONFIG_DIR": str(config_dir), "QODER_PROGRAM_STATUS": "0"})
        if self.credential_value:
            env["QODER_PERSONAL_ACCESS_TOKEN"] = self.credential_value
        else:
            key_env = str(self.cfg.get("key_env") or "")
            if key_env and os.environ.get(key_env):
                env["QODER_PERSONAL_ACCESS_TOKEN"] = os.environ[key_env]
        stopped = self.check_stop()
        if stopped:
            return Outcome("stopped", f"stopped: {stopped}", stopped=stopped,
                           code="AK-TASK-CANCELLED" if stopped == "cancelled" else "AK-BUDGET")
        event_log = self.task_dir / "qoder-events.jsonl"
        err_log = self.task_dir / "qoder-stderr.log"
        with open(err_log, "wb") as err, open(event_log, "a", encoding="utf-8") as transcript:
            proc = subprocess.Popen(args, cwd=str(self.worktree), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=err, env=env, start_new_session=os.name == "posix")
            try:
                try:
                    proc.stdin.write(self._prompt(brief, resume).encode("utf-8"))
                    proc.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
                for raw in proc.stdout:
                    stop = self.check_stop()
                    if stop:
                        return Outcome("stopped", f"stopped: {stop}", stopped=stop,
                                       code="AK-TASK-CANCELLED" if stop == "cancelled" else "AK-BUDGET")
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    transcript.write(redact_text(line) + "\n")
                    transcript.flush()
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    outcome = self._consume(event)
                    if outcome:
                        return outcome
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    CodexBackend._kill(proc)
                    return Outcome("paused", "qodercli did not exit after its event stream", code="AK-MODEL-TIMEOUT")
            finally:
                CodexBackend._kill(proc)
        if self.error:
            return Outcome("failed", self.error, code="AK-CONFIG")
        if proc.returncode == 41:
            return Outcome("failed", "qodercli authentication failed", code="AK-MODEL-AUTH")
        if proc.returncode == 53:
            return Outcome("stopped", "qodercli reached its turn limit", stopped="rounds", code="AK-WORKER-ROUNDS")
        if proc.returncode == 44:
            return Outcome("failed", "qodercli sandbox failed", code="AK-TASK-STATE")
        if proc.returncode == 52:
            return Outcome("failed", "qodercli configuration failed", code="AK-CONFIG")
        if proc.returncode == 130:
            return Outcome("stopped", "qodercli interrupted", stopped="cancelled", code="AK-TASK-CANCELLED")
        if proc.returncode != 0:
            tail = err_log.read_text(encoding="utf-8", errors="replace")[-500:]
            return _qoder_failure(None, tail or f"qodercli exited {proc.returncode}")
        if not self.initialized:
            return Outcome("failed", "qodercli ended without a verified system/init policy snapshot", code="AK-CONFIG")
        if not self.completed:
            return Outcome("failed", "qodercli ended without a successful result", code="AK-MODEL-BAD-RESPONSE")
        return Outcome("submitted", "qodercli completed", claim=self.claim)


class CursorBackend:
    name = "cursor"

    def capabilities(self) -> dict:
        return backend_capabilities(self.name, {"workers": {"cursor": self.cfg}})

    def __init__(self, cfg: dict, task: dict, worktree: Path, task_dir: Path, *,
                 root: Optional[Path] = None, credential_env: str = "", credential_value: str = "",
                 on_progress: Callable[[dict], None] = lambda d: None,
                 check_stop: Callable[[], Optional[str]] = lambda: None,
                 events=None, max_rounds: int = 40):
        self.cfg = cursor_config(cfg)
        self.credential_env = credential_env
        self.credential_value = credential_value
        self.transport = str(self.cfg.get("transport") or "cli")
        if self.transport not in ("cli", "sdk"):
            raise ValueError("workers.cursor.transport must be cli or sdk")
        self.task = task
        self.worktree = worktree
        self.task_dir = task_dir
        self.root = root or worktree
        self.on_progress = on_progress
        self.check_stop = check_stop
        self.events = events
        self.max_rounds = max(1, int(max_rounds))
        prior = task.get("prior") or {}
        self.prior_rounds = int(prior.get("rounds") or 0)
        self.rounds = self.prior_rounds
        self.tokens_in = int(prior.get("tokens_in") or 0)
        self.tokens_out = int(prior.get("tokens_out") or 0)
        self.usd = float(prior.get("usd") or 0)
        self.prior_usd = self.usd
        self.cost_known = False
        self.session_id = str(task.get("backend_session") or "")
        if task.get("backend_transport") and task["backend_transport"] != self.transport:
            self.session_id = ""
        self.turn_id = ""
        self.item_id = ""
        self.initialized = False
        self.completed = False
        self.claim = ""
        self.denied: list = []
        self.last_component = "cursor"
        self.last_step = 0

    def stats(self) -> dict:
        return {"rounds": self.rounds, "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                "usd": round(self.usd, 6), "cost_known": self.cost_known,
                "compactions": 0, "repairs": 0,
                "compression": {"deduped": 0, "aged": 0, "saved_chars": 0, "style": "off", "backend": "cursor"}}

    def _ev(self, event: str, **extra):
        self.last_step = self.rounds
        extra.setdefault("session_id", self.session_id or None)
        extra.setdefault("turn_id", self.turn_id or None)
        extra.setdefault("item_id", self.item_id or None)
        if self.events is not None:
            self.events.emit(event, component="cursor", step=self.rounds, **extra)

    def _prompt(self, brief: str, resume: Optional[str]) -> str:
        lines = [
            "You are an AgentKit external worker running in an isolated git worktree.",
            "Make the smallest change that satisfies the goal. Do not commit or push.",
            "Use workspace file read/search/edit tools only. AgentKit independently runs acceptance checks.",
            "", "GOAL:", self.task["goal"], "", "ACCEPTANCE CHECKS:",
            *[f"- {check}" for check in self.task.get("checks") or []], "",
            "ALLOWED PATHS: " + (", ".join(self.task.get("allow") or []) or "(any)"),
            "", "PROJECT MEMORY (brief):", brief or "(none)",
        ]
        if resume:
            lines += ["", "RESUME CONTEXT:", resume]
        return "\n".join(lines) + "\n"

    def _consume(self, event: dict):
        kind = event.get("type")
        session = str(event.get("session_id") or "")
        if session and session != self.session_id:
            self.session_id = session
            self._ev("backend_session", session_id=session)
        if kind == "system" and event.get("subtype") == "init":
            source = str(event.get("apiKeySource") or "").lower()
            mode = str(event.get("permissionMode") or "").lower()
            cwd = event.get("cwd")
            tools = {str(item).lower() for item in event.get("tools") or []}
            if (source not in ("env", "flag") or mode != "default" or event.get("mcp_servers")
                    or tools & {"shell", "bash", "webfetch", "websearch", "mcp"}):
                self.claim = "cursor policy verification failed: expected API-key auth, default permissions and no shell/network/MCP"
                return Outcome("failed", self.claim, code="AK-CONFIG")
            if cwd and Path(str(cwd)).resolve() != self.worktree.resolve():
                self.claim = "cursor started outside the AgentKit worktree"
                return Outcome("failed", self.claim, code="AK-CONFIG")
            self.initialized = True
            self._ev("backend_init", model=str(event.get("model") or ""), api_key_source=source)
        elif kind == "assistant":
            self.rounds += 1
            message = event.get("message") or {}
            self.turn_id = str(event.get("turn_id") or message.get("id") or
                               f"{self.session_id or 'cursor'}:turn:{self.rounds}")
            self.item_id = ""
            self._ev("model_call")
            self.on_progress(self.stats())
            if self.rounds > self.max_rounds:
                return Outcome("stopped", f"max rounds ({self.max_rounds})", stopped="rounds", code="AK-WORKER-ROUNDS")
            for part in message.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "text":
                    self.item_id = str(part.get("id") or self.item_id)
                    self.claim = str(part.get("text") or "")[:500]
        elif kind == "tool_call":
            tool = event.get("tool_call") or {}
            name = next((key for key in tool if isinstance(tool[key], dict)), "unknown")
            result = next(iter(tool.values()), {}) if isinstance(tool, dict) else {}
            self.item_id = str(event.get("item_id") or event.get("id") or result.get("id") or self.item_id)
            self._ev("tool", tool=name, status=event.get("subtype") or "unknown")
            details = result.get("result") if isinstance(result, dict) else None
            if isinstance(details, dict) and details.get("error"):
                self.denied.append(str(details["error"])[:200])
        elif kind == "result":
            if event.get("subtype") != "success" or event.get("is_error"):
                return _failure(str(event.get("result") or "cursor execution failed"))
            self.completed = True
            self.claim = str(event.get("result") or self.claim)[:500]
        return None

    def _over_budget(self) -> Optional[str]:
        budget = self.task.get("budget") or {}
        if budget.get("tokens") and self.tokens_in + self.tokens_out >= int(budget["tokens"]):
            return "budget"
        if budget.get("usd") and self.cost_known and self.usd >= float(budget["usd"]):
            return "budget"
        return None

    def _sdk_usage(self, usage) -> None:
        self.tokens_in += int(getattr(usage, "input_tokens", 0) or 0)
        self.tokens_out += int(getattr(usage, "output_tokens", 0) or 0)
        self.item_id = ""
        self._ev("model_usage", input_tokens=self.tokens_in, output_tokens=self.tokens_out)
        self.on_progress(self.stats())

    def _cancel_sdk(self, run) -> None:
        if getattr(run, "status", "") == "running":
            try:
                run.cancel()
            except Exception:
                pass

    def _run_sdk(self, brief: str, resume: Optional[str]) -> Outcome:
        model = str(self.task.get("model") or self.cfg.get("model") or "").strip()
        if not model:
            return Outcome("failed", "Cursor SDK requires an explicit model", code="AK-CONFIG")
        try:
            from cursor_sdk import Agent, AgentOptions, LocalAgentOptions, SandboxOptions
        except ImportError:
            return Outcome("failed", "cursor-sdk is not installed", code="AK-CONFIG")
        config_dir = cursor_config_dir({"workers": {"cursor": self.cfg}}, self.root)
        (config_dir / ".cursor").mkdir(parents=True, exist_ok=True)
        (config_dir / ".cursor" / "cli-config.json").write_text(
            json.dumps(cursor_settings(), separators=(",", ":")), encoding="utf-8")
        env = _inject_credential(dict(os.environ), "CURSOR_API_KEY", self.credential_value)
        for key in tuple(env):
            if key.startswith("CURSOR_") and key != "CURSOR_API_KEY":
                env.pop(key)
        env.update({"HOME": str(config_dir), "USERPROFILE": str(config_dir),
                    "XDG_CONFIG_HOME": str(config_dir), "CURSOR_PROGRAM_STATUS": "0"})
        stopped = self.check_stop()
        if stopped:
            return Outcome("stopped", f"stopped: {stopped}", stopped=stopped,
                           code="AK-TASK-CANCELLED" if stopped == "cancelled" else "AK-BUDGET")
        options = AgentOptions(
            model=model, mcp_servers={}, tools=["read", "edit", "write", "grep", "glob", "ls"],
            local=LocalAgentOptions(cwd=self.worktree, setting_sources=["user"],
                                    sandbox_options=SandboxOptions(enabled=False)))
        transcript = self.task_dir / "cursor-sdk-events.jsonl"
        deadline = time.monotonic() + float((self.task.get("budget") or {}).get("minutes") or 30) * 60
        usage_seen = False
        try:
            with patch.dict(os.environ, env, clear=True):
                agent = Agent.resume(self.session_id, options) if self.session_id else Agent.create(options)
                try:
                    self.session_id = str(agent.agent_id)
                    self.initialized = True
                    self._ev("backend_session", session_id=self.session_id)
                    self._ev("backend_init", transport="sdk", tools=options.tools)
                    run = agent.send(self._prompt(brief, resume))
                    self.turn_id = str(getattr(run, "id", "") or
                                       f"{self.session_id or 'cursor-sdk'}:turn:{self.rounds + 1}")
                    with open(transcript, "a", encoding="utf-8") as events:
                        for message in run.messages():
                            stop = self.check_stop()
                            if stop:
                                self._cancel_sdk(run)
                                return Outcome("stopped", f"stopped: {stop}", stopped=stop,
                                               code="AK-TASK-CANCELLED" if stop == "cancelled" else "AK-BUDGET")
                            if time.monotonic() >= deadline:
                                self._cancel_sdk(run)
                                return Outcome("stopped", "budget exceeded", stopped="budget", code="AK-BUDGET")
                            kind = str(getattr(message, "type", ""))
                            self.item_id = str(getattr(message, "id", "") or getattr(message, "uuid", "") or self.item_id)
                            if kind == "assistant":
                                self.rounds += 1
                                self._ev("model_call")
                                for block in getattr(getattr(message, "message", None), "content", None) or []:
                                    if getattr(block, "type", "") == "text":
                                        self.claim = str(getattr(block, "text", ""))[:500]
                                if self.rounds > self.max_rounds:
                                    self._cancel_sdk(run)
                                    return Outcome("stopped", f"max rounds ({self.max_rounds})",
                                                   stopped="rounds", code="AK-WORKER-ROUNDS")
                                self.on_progress(self.stats())
                            elif kind == "usage":
                                usage_seen = True
                                self._sdk_usage(getattr(message, "usage", None))
                                over = self._over_budget()
                                if over:
                                    self._cancel_sdk(run)
                                    return Outcome("stopped", "budget exceeded", stopped=over, code="AK-BUDGET")
                            elif kind == "tool_call":
                                self._ev("tool", tool=str(getattr(message, "name", "unknown")),
                                         status=str(getattr(message, "status", "unknown")))
                            record = {"type": kind, "run_id": str(getattr(run, "id", "")),
                                      "session_id": self.session_id, "turn_id": self.turn_id,
                                      "item_id": self.item_id}
                            if kind == "usage":
                                usage = getattr(message, "usage", None)
                                record["input_tokens"] = int(getattr(usage, "input_tokens", 0) or 0)
                                record["output_tokens"] = int(getattr(usage, "output_tokens", 0) or 0)
                            events.write(json.dumps(record, separators=(",", ":")) + "\n")
                            events.flush()
                        result = run.wait()
                        usage = getattr(result, "usage", None)
                        if usage is not None:
                            usage_seen = True
                            self.tokens_in = int(getattr(usage, "input_tokens", 0) or 0) + int(
                                (self.task.get("prior") or {}).get("tokens_in") or 0)
                            self.tokens_out = int(getattr(usage, "output_tokens", 0) or 0) + int(
                                (self.task.get("prior") or {}).get("tokens_out") or 0)
                        stop = self.check_stop()
                        if stop:
                            return Outcome("stopped", f"stopped: {stop}", stopped=stop,
                                           code="AK-TASK-CANCELLED" if stop == "cancelled" else "AK-BUDGET")
                        if time.monotonic() >= deadline:
                            return Outcome("stopped", "budget exceeded", stopped="budget", code="AK-BUDGET")
                        if getattr(result, "status", "") != "finished":
                            return _failure(str(getattr(result, "result", "") or "cursor execution failed"))
                        budget = self.task.get("budget") or {}
                        if budget.get("tokens") and not usage_seen:
                            return Outcome("failed", "Cursor SDK did not report token usage", code="AK-CONFIG")
                        try:
                            billed = agent.get_usage(run_id=str(getattr(run, "id", "")))
                            cost = getattr(billed, "cost", None)
                            if cost is not None:
                                self.usd = self.prior_usd + float(getattr(cost, "charged_cents", 0) or 0) / 100
                                self.cost_known = True
                        except Exception:
                            pass
                        over = self._over_budget()
                        if over:
                            return Outcome("stopped", "budget exceeded", stopped=over, code="AK-BUDGET")
                        self.completed = True
                        self.claim = str(getattr(result, "result", "") or self.claim)[:500]
                        self.on_progress(self.stats())
                        return Outcome("submitted", "cursor SDK completed", claim=self.claim)
                finally:
                    try:
                        agent.close()
                    except Exception:
                        pass
        except Exception as e:
            return _failure(str(e))

    def run(self, brief: str, resume: Optional[str] = None) -> Outcome:
        return self._run_sdk(brief, resume) if self.transport == "sdk" else self._run_cli(brief, resume)

    def _run_cli(self, brief: str, resume: Optional[str] = None) -> Outcome:
        command = _command(self.cfg, "cursor", "agent")
        config_dir = cursor_config_dir({"workers": {"cursor": self.cfg}}, self.root)
        (config_dir / ".cursor").mkdir(parents=True, exist_ok=True)
        (config_dir / ".cursor" / "cli-config.json").write_text(
            json.dumps(cursor_settings(), separators=(",", ":")), encoding="utf-8")
        model = str(self.task.get("model") or self.cfg.get("model") or "").strip()
        args = [*command, "--print", "--output-format", "stream-json", "--workspace", str(self.worktree)]
        if model:
            args += ["--model", model]
        if self.session_id:
            args += ["--resume", self.session_id]
        args.append(self._prompt(brief, resume))
        env = _inject_credential(dict(os.environ), "CURSOR_API_KEY", self.credential_value)
        for key in tuple(env):
            if key.startswith("CURSOR_") and key != "CURSOR_API_KEY":
                env.pop(key)
        env.update({"HOME": str(config_dir), "USERPROFILE": str(config_dir),
                    "XDG_CONFIG_HOME": str(config_dir), "CURSOR_PROGRAM_STATUS": "0"})
        stopped = self.check_stop()
        if stopped:
            return Outcome("stopped", f"stopped: {stopped}", stopped=stopped,
                           code="AK-TASK-CANCELLED" if stopped == "cancelled" else "AK-BUDGET")
        event_log = self.task_dir / "cursor-events.jsonl"
        err_log = self.task_dir / "cursor-stderr.log"
        deadline = time.monotonic() + float((self.task.get("budget") or {}).get("minutes") or 30) * 60
        with open(err_log, "w", encoding="utf-8") as err, open(event_log, "a", encoding="utf-8") as transcript:
            proc = subprocess.Popen(args, cwd=str(self.worktree), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=env, start_new_session=os.name == "posix")

            def pump_stderr():
                for raw in proc.stderr:
                    text = redact_text(raw.decode("utf-8", "replace"))
                    err.write(text[-65536:])
                    err.flush()

            reader = threading.Thread(target=pump_stderr, daemon=True)
            reader.start()
            try:
                for raw in proc.stdout:
                    stop = self.check_stop()
                    if stop:
                        return Outcome("stopped", f"stopped: {stop}", stopped=stop,
                                       code="AK-TASK-CANCELLED" if stop == "cancelled" else "AK-BUDGET")
                    if time.monotonic() >= deadline:
                        return Outcome("stopped", "budget exceeded", stopped="budget", code="AK-BUDGET")
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    transcript.write(redact_text(line) + "\n")
                    transcript.flush()
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict):
                        outcome = self._consume(event)
                        if outcome:
                            return outcome
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    return Outcome("paused", "cursor did not exit after its event stream", code="AK-MODEL-TIMEOUT")
            finally:
                CodexBackend._kill(proc)
                reader.join(timeout=2)
        if not self.initialized:
            return Outcome("failed", "cursor ended without a verified system/init policy snapshot", code="AK-CONFIG")
        if not self.completed:
            tail = err_log.read_text(encoding="utf-8", errors="replace")[-500:]
            return _failure(tail or f"cursor exited {proc.returncode}")
        return Outcome("submitted", "cursor completed", claim=self.claim)
