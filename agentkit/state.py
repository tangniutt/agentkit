# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Read-hash ledger: which content hash the agent last saw for each file (M-07 stale-write guard)."""

from __future__ import annotations

import json
import os
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
    d = load(ctx)
    if sha is None:
        d.pop(rel, None)
    else:
        d[rel] = sha
    p = _path(ctx)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d))
    os.replace(tmp, p)


def last_seen(ctx: Ctx, rel: str) -> Optional[str]:
    return load(ctx).get(rel)
