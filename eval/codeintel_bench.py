# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Reproducible builtin-vs-XERJ code retrieval benchmark."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from agentkit.codeintel import search
from agentkit.config import load_config
from agentkit.core import Ctx

QUERIES = [
    {"query": "OpenAI compatible chat retry rate limit", "expect": ["models.py"], "keyword": "retry"},
    {"query": "resume paused task with compacted transcript", "expect": ["worker.py", "tasks.py"], "keyword": "resume"},
    {"query": "memory id path validation", "expect": ["memory.py"], "keyword": "memory"},
    {"query": "MCP stdio tools call JSON-RPC", "expect": ["mcp_server.py"], "keyword": "tools/call"},
    {"query": "verify git changes Unicode paths", "expect": ["verify.py"], "keyword": "git_changes"},
    {"query": "checkpoint snapshot rollback", "expect": ["checkpoint.py"], "keyword": "checkpoint"},
]


def run(root: Path, prefix: str, backends: list[str]) -> dict:
    ctx = Ctx(root=root, cwd=root, config=load_config(root))
    rows = []
    for case in QUERIES:
        for backend in backends:
            t0 = time.monotonic()
            try:
                result = search(ctx, case["query"], prefix=prefix, k=5, backend=backend)
                hits = result.data.get("hits", [])
                error = None
            except Exception as e:
                hits, error = [], str(e)
            elapsed = time.monotonic() - t0
            ranks = []
            for rank, hit in enumerate(hits, 1):
                path = str(hit.get("path", "")).replace("\\", "/")
                if any(x in path for x in case["expect"]):
                    ranks.append(rank)
            rows.append({
                "query": case["query"], "backend": result.data.get("backend", backend) if not error else backend,
                "hits": len(hits), "recall_at_5": bool(ranks), "first_rank": min(ranks) if ranks else None,
                "chars": sum(len(str(h.get("text", ""))) for h in hits), "seconds": round(elapsed, 4), "error": error,
            })
    summary = {}
    for backend in backends:
        selected = [r for r in rows if r["backend"] == backend]
        summary[backend] = {
            "queries": len(selected),
            "recall_at_5": sum(r["recall_at_5"] for r in selected),
            "mrr": round(sum(1 / r["first_rank"] if r["first_rank"] else 0 for r in selected) / max(1, len(selected)), 4),
            "chars": sum(r["chars"] for r in selected),
            "seconds": round(sum(r["seconds"] for r in selected), 4),
            "errors": sum(bool(r["error"]) for r in selected),
        }
    return {"root": str(root), "prefix": prefix, "summary": summary, "rows": rows}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m eval.codeintel_bench")
    p.add_argument("--root", default=".")
    p.add_argument("--prefix", default="ax")
    p.add_argument("--backends", default="builtin,xerj")
    p.add_argument("--out")
    a = p.parse_args(argv)
    backends = [x.strip() for x in a.backends.split(",") if x.strip()]
    data = run(Path(a.root).resolve(), a.prefix, backends)
    text = json.dumps(data, ensure_ascii=False, indent=1)
    if a.out:
        Path(a.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if all(v["errors"] == 0 for v in data["summary"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
