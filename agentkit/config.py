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
    if "model" in user and "model" not in cfg:
        cfg["model"] = user["model"]
    return cfg


def model_configured(cfg: dict) -> bool:
    m = cfg.get("model") or {}
    return bool(m.get("base_url") or m.get("name") or os.environ.get("AGENTKIT_MODEL"))
