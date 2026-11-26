# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Config loading: <repo>/.agentkit/config.toml, then ~/.config/agentkit/config.toml (model only)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

EXAMPLE = """# .agentkit/config.toml
# Saved-login opt-in belongs in the user config, never this project file:
# ~/.config/agentkit/config.toml -> [security] allow_saved_credentials=true
[output]
budget = 2000          # tokens (~4 chars each)

[run]
timeout = 120          # seconds
allow = []             # regexes; a matching command segment bypasses the built-in deny list
deny = []              # regexes; always refused (wins over allow)

[logs]
retain_days = 7        # .agentkit/logs older than this are deleted automatically (0 = keep forever)

[tasks]
concurrency = 2        # global worker limit
max_handoffs = 2       # model switches per task

[workers.codex]
command = ["codex"]    # argv, never a shell string
model = ""             # optional default Codex model id
key_env = ""           # optional dedicated Codex API key variable; never the key itself
sandbox = "workspace-write"  # read-only | workspace-write | danger-full-access
price_in = 0.0         # USD per million input tokens; 0 means cost unknown
price_out = 0.0        # USD per million output tokens; 0 means cost unknown
allow_unsandboxed = false  # test-only escape hatch; keep false in production

[workers.opencode]
command = ["opencode"]  # official CLI; argv, never a shell string
version = "v1"         # v1 | v2; V2 supports signed-out free models with rate limits
model = ""             # optional default in provider/model format (for example opencode/beam-free on V2)
agent = "build"        # primary AgentKit-controlled agent
key_env = ""           # optional dedicated key variable; provider env keys also work
allow_bash = false     # keep false: AgentKit runs acceptance checks independently
allow_account_login = false  # true explicitly binds real OpenCode credential/data directories
cost_known = false     # true only when zero reported cost is confirmed to mean free
price_in = 0.0         # optional fallback USD per million input tokens
price_out = 0.0        # optional fallback USD per million output tokens
allow_unsandboxed = false  # test-only escape hatch; keep false in production

[workers.qoder]
command = ["qodercli"]  # official CLI; uses `qodercli login` or QODER_PERSONAL_ACCESS_TOKEN
model = ""             # optional Qoder model alias or configured custom model id
config_dir = ""        # empty uses a dedicated XDG_CONFIG_HOME/agentkit/qoder directory; set explicitly to reuse another login
key_env = ""           # optional dedicated PAT variable; empty uses normal CLI login/PAT discovery
max_rounds = 40
max_retries = 0        # let AgentKit classify failures and hand off instead of retrying internally
price_in = 0.0         # optional fallback USD per million input tokens
price_out = 0.0        # optional fallback USD per million output tokens
cost_known = false     # true only for confirmed zero-USD models
allow_unsandboxed = false  # test-only escape hatch; keep false in production

[workers.cursor]
command = ["agent"]     # official Cursor CLI; used when transport = "cli"
transport = "cli"       # cli | sdk; sdk requires the optional cursor-sdk package
model = ""             # optional Cursor model id; required when transport = "sdk"
key_env = "CURSOR_API_KEY"
config_dir = ""        # empty uses XDG_CONFIG_HOME/agentkit/cursor; never store credentials here
max_rounds = 40
allow_unsandboxed = false
# CLI stream-json has no token/cost fields; SDK reports usage when the runtime provides it.

[sandbox]
mode = "auto"          # auto | required | off; bubblewrap on Linux, direct-only on Windows
network = true         # false = no network; ["api.openai.com"] = domain allowlist via isolated proxy

[codeintel]
backend = "auto"        # auto | builtin | xerj
xerj_path = ""          # optional absolute path; otherwise PATH
url = "http://127.0.0.1:9200"
prefix = "ax"
api_key_env = "XERJ_API_KEY"
worker_tools = false    # expose code_search/code_defs to the built-in worker

[codeintel.lsp.python]  # optional semantic references; no third-party code is bundled
command = ["pylsp"]
args = []
timeout = 30
# Credential-like environment variables are not inherited by the server.

[compress]             # C-08 context compression
enabled = true         # master switch
run_filters = true     # command-aware filters for `run` output (raw: agentkit logs RUN --raw)
dedup = true           # worker: identical tool results -> same_as_turn reference
aging = true           # worker: tool results older than the last 2 turns -> one-line summaries
age_min_chars = 4000   # age in batches of at least this much old text (keeps the cached prefix stable longer)
style = "auto"         # worker terse-style note: auto (big repo -> in the prompt from round 1; else appended
                       # once when the transcript grows) | terse (always) | normal (never)
style_min_files = 40   # auto: tracked files that count as a big repo
style_after_chars = 4000  # auto: transcript size (beyond system + task) that triggers the appended note
"""


def _read(p: Path) -> dict:
    try:
        with open(p, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:  # malformed config: surface it but do not crash
        return {"_error": f"{p}: {e}"}


def user_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "agentkit" / "config.toml"


def load_config(root: Path) -> dict:
    cfg = _read(root / ".agentkit" / "config.toml")
    user = _read(user_config_path())
    for k in ("model", "models"):
        if k in user and k not in cfg:
            cfg[k] = user[k]
    security = user.get("security")
    cfg["_user_security"] = security if isinstance(security, dict) else {}
    return cfg


def saved_credentials_allowed(cfg: dict) -> bool:
    security = cfg.get("_user_security")
    return isinstance(security, dict) and security.get("allow_saved_credentials") is True


def model_configured(cfg: dict) -> bool:
    if cfg.get("models"):
        return True
    m = cfg.get("model") or {}
    return bool(m.get("base_url") or m.get("name") or os.environ.get("AGENTKIT_MODEL"))


def repo_config_path(root: Path) -> Path:
    return root / ".agentkit" / "config.toml"


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def set_table(root: Path, header: str, values: dict) -> Path:
    """Replace (or append) one `[header]` table in the repo config, keeping everything else verbatim."""
    p = repo_config_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = p.read_text().splitlines() if p.exists() else []
    out, skip = [], False
    for ln in lines:
        st = ln.strip()
        if st.startswith("[") and not st.startswith("[["):
            skip = st == f"[{header}]"
            if skip:
                continue
        elif st.startswith("[["):
            skip = False
        if not skip:
            out.append(ln)
    while out and not out[-1].strip():
        out.pop()
    if values is not None:
        if out:
            out.append("")
        out.append(f"[{header}]")
        out += [f"{k} = {_toml_value(v)}" for k, v in values.items() if v is not None]
    p.write_text("\n".join(out) + "\n")
    return p
