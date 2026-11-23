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
[output]
budget = 2000          # tokens (~4 chars each)

[run]
timeout = 120          # seconds
allow = []             # regexes; a matching command segment bypasses the built-in deny list
deny = []              # regexes; always refused (wins over allow)

[logs]
retain_days = 7        # .agentkit/logs older than this are deleted automatically (0 = keep forever)
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
    return cfg


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
