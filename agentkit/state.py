# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Read-hash ledger: which content hash the agent last saw for each file (M-07 stale-write guard)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .core import Ctx


def _path(ctx: Ctx):
    return ctx.state("state") / "reads.json"


def load(ctx: Ctx) -> dict:
    try:
        return json.loads(_path(ctx).read_text())
    except (FileNotFoundError, ValueError):
        return {}


def record(ctx: Ctx, rel: str, sha: Optional[str]) -> None:
    from .core import AKError, atomic_write, file_lock

    if not isinstance(rel, str) or not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
        raise AKError("invalid state path", "ledger keys must be repo-relative paths", None, code="AK-PATH")
    ctx.state_path("state", create=True)
    p = _path(ctx)
    with file_lock(ctx.state_path("state", "reads.lock")):
        d = load(ctx)
        if sha is None:
            d.pop(rel, None)
        else:
            d[rel] = sha
        atomic_write(p, json.dumps(d, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def last_seen(ctx: Ctx, rel: str) -> Optional[str]:
    return load(ctx).get(rel)
