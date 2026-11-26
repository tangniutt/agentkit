# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Opt-in smoke test against a real stdio language server."""
import json
import os

import pytest


@pytest.mark.skipif(not os.environ.get("AGENTKIT_LSP_TEST_COMMAND"),
                    reason="set AGENTKIT_LSP_TEST_COMMAND to a JSON argv array")
def test_real_lsp_references(ak, repo):
    command = json.loads(os.environ["AGENTKIT_LSP_TEST_COMMAND"])
    assert isinstance(command, list) and command and all(isinstance(item, str) for item in command)
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n\nadd(1, 2)\n", encoding="utf-8")
    config = repo / ".agentkit" / "config.toml"
    config.parent.mkdir(exist_ok=True)
    config.write_text("[codeintel.lsp.python]\ncommand = " + json.dumps(command) + "\nargs = []\ntimeout = 30\n",
                      encoding="utf-8")

    result = ak("code", "refs", "add", "--path", "calc.py", "--line", "1", "--column", "5",
                "--backend", "lsp")
    assert result["ok"], result
    assert result["data"]["backend"] == "lsp" and result["data"]["semantic"] is True
    assert [(hit["line"], hit["column"]) for hit in result["data"]["hits"]] == [(4, 1)]

    result = ak("code", "refs", "add", "--path", "calc.py", "--line", "1", "--column", "5",
                "--backend", "lsp", "--include-declaration")
    assert result["ok"], result
    assert [(hit["line"], hit["column"]) for hit in result["data"]["hits"]] == [(1, 5), (4, 1)]
