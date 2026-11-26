# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Code intelligence adapter contracts using real XERJ response shapes."""
import sys

from agentkit import codeintel
from agentkit.core import Ctx

SEARCH_PAYLOAD = {
    "hits": {"total": {"value": 1}, "hits": [{
        "_score": 0.86,
        "_source": {"title": "calc.py", "language": "python", "start_line": 1, "ax_path": "calc.py"},
        "fields": {"_passage": [{"field": "body", "ordinal": 0, "start_offset": 0, "end_offset": 60,
                                  "text": "def add(a, b):\r\n    return a + b"}]},
        "match_line": 1,
    }]}
}
DEF_PAYLOAD = {
    "hits": {"total": {"value": 1}, "hits": [{
        "_score": 41.0,
        "_source": {"path": "calc.py", "language": "python", "name": "add", "kind": "function",
                    "line": 1, "start_line": 1, "code": "def add(a, b):", "ax_path": "calc.py"},
        "fields": {"_passage": [{"text": "def add(a, b):"}]},
    }]}
}


def test_normalize_real_xerj_payloads():
    hit = codeintel._normalize_search(SEARCH_PAYLOAD)[0]
    assert hit["path"] == "calc.py" and hit["line"] == 1 and "return a + b" in hit["text"]
    definition = codeintel._normalize_defs(DEF_PAYLOAD)[0]
    assert definition["name"] == "add" and definition["code"] == "def add(a, b):"


def test_xerj_backend_builds_cli_contract(monkeypatch):
    calls = []

    def run(cfg, args, ok_codes=(0,)):
        calls.append((args, ok_codes))
        return SEARCH_PAYLOAD if args[0] == "search" else DEF_PAYLOAD

    monkeypatch.setattr(codeintel, "_xerj_run", run)
    backend = codeintel.XerjBackend({"url": "http://127.0.0.1:9200", "prefix": "demo"})
    hits = backend.search("add numbers", "demo", 3, 20)
    assert hits[0]["path"] == "calc.py" and len(hits[0]["text"]) <= 20
    assert backend.definitions("add", "demo", 3)[0]["name"] == "add"
    assert calls[0][0][:2] == ["search", "add numbers"]
    assert "--json" in calls[0][0] and "-k" in calls[0][0]
    assert calls[1][0][:2] == ["def", "add"]


def test_builtin_search_and_definitions(repo):
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    ctx = Ctx(root=repo, cwd=repo)
    backend = codeintel.BuiltinBackend(ctx)
    assert backend.search("add", "", 5, 600)[0]["path"] == "calc.py"
    defs = backend.definitions("add", "", 5)
    assert defs[0]["name"] == "add" and defs[0]["line"] == 1


def test_builtin_references_are_labeled_lexical(repo):
    (repo / "calc.py").write_text("def add(a, b):\n    return add(a, b)\n", encoding="utf-8")
    (repo / "use.py").write_text("from calc import add\nadd(1, 2)\n", encoding="utf-8")
    result = codeintel.references(Ctx(root=repo, cwd=repo), "add", backend="builtin")
    assert result.data["backend"] == "builtin" and result.data["semantic"] is False
    assert {hit["path"] for hit in result.data["hits"]} == {"calc.py", "use.py"}


def test_lsp_references_use_configured_server(repo, monkeypatch):
    (repo / "calc.py").write_text("def add(a, b):\n    return add(a, b)\n", encoding="utf-8")
    calls = []

    def references(self, path, line, column, k, include_declaration=False):
        calls.append((path.name, line, column, k, include_declaration))
        return [{"path": "calc.py", "line": 2, "column": 5, "text": "return add(a, b)"}]

    monkeypatch.setattr(codeintel.LSPBackend, "references", references)
    ctx = Ctx(root=repo, cwd=repo, config={"codeintel": {"lsp": {"python": {"command": [sys.executable]}}}})
    result = codeintel.references(ctx, "add", path="calc.py", line=1, column=1, backend="lsp")
    assert result.data["backend"] == "lsp" and result.data["semantic"] is True
    assert calls == [("calc.py", 0, 0, 5, False)]


def test_auto_falls_back_without_xerj(repo, monkeypatch):
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    monkeypatch.setattr(codeintel, "xerj_available", lambda cfg: False)
    result = codeintel.search(Ctx(root=repo, cwd=repo), "add")
    assert result.data["backend"] == "builtin" and result.data["hits"]


def test_xerj_subprocess_does_not_inherit_model_keys(monkeypatch):
    monkeypatch.setenv("MODEL_SECRET", "model-key-value")
    monkeypatch.setenv("XERJ_API_KEY", "xerj-key-value")
    cfg = {"models": {"worker": {"key_env": "MODEL_SECRET"}}, "api_key_env": "XERJ_API_KEY"}
    env = codeintel._xerj_env(cfg)
    assert "MODEL_SECRET" not in env and env["XERJ_API_KEY"] == "xerj-key-value"


def test_lsp_environment_excludes_credentials(monkeypatch):
    monkeypatch.setenv("PATH", "keep-path")
    monkeypatch.setenv("MODEL_SECRET", "model-key-value")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-key-value")
    monkeypatch.setenv("QODER_PERSONAL_ACCESS_TOKEN", "qoder-token")
    monkeypatch.setenv("GITHUB_TOKEN", "github-token")
    monkeypatch.setenv("NORMAL_SETTING", "keep")
    config = {"models": {"worker": {"key_env": "MODEL_SECRET"}}}
    env = codeintel._lsp_env(config)
    assert env["PATH"] == "keep-path" and env["NORMAL_SETTING"] == "keep"
    assert not {"MODEL_SECRET", "OPENAI_API_KEY", "QODER_PERSONAL_ACCESS_TOKEN", "GITHUB_TOKEN"} & env.keys()


def test_memory_recall_uses_real_list_shape(monkeypatch):
    payload = {"hits": [{"id": "run-tests", "text": "Always run pytest before merging",
                         "metadata": {"source": "agentkit"}, "score": 0.016}],
               "namespace": "project"}
    monkeypatch.setattr(codeintel.XerjBackend, "_request", lambda self, path, body: payload)
    hits = codeintel.XerjBackend({}).memory_recall("project", "test command")
    assert hits[0]["id"] == "run-tests" and "pytest" in hits[0]["text"]


def test_memory_uses_http_contract(monkeypatch):
    seen = {}

    def request(self, path, body):
        seen.update(path=path, body=body)
        return {"ok": True}

    monkeypatch.setattr(codeintel.XerjBackend, "_request", request)
    backend = codeintel.XerjBackend({"url": "http://127.0.0.1:9200"})
    backend.memory_store("project notes", "use pytest", "run-tests")
    assert seen["path"] == "/_memory/project%20notes" and seen["body"]["id"] == "run-tests"
