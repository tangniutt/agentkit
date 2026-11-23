# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Third provider (NVIDIA NIM) + free-tier pacing: presets, 504 as upstream-unavailable, reasoning models
(content null, text in reasoning_content, finish_reason=length), Retry-After up to max_wait, rpm pacing."""
import json

import pytest

from agentkit.models import ChatClient, ModelError, ModelSpec, all_models
from test_m2_tasks import CHECK, FIX, create, project, tjson, wait

KEY = "fk" + "-" + "prov" + "8" * 18


def _spec(url, **kw):
    return ModelSpec(name="fake", base_url=url, model_id="m", key_env="FAKE_KEY", **kw)


@pytest.fixture
def keyenv(monkeypatch):
    monkeypatch.setenv("FAKE_KEY", KEY)


def test_model_add_presets(ak, repo):
    r = ak("model", "add", "nim", "--preset", "nvidia", "--model", "nvidia/nemotron-3.5-lightning-30b-a3b")
    assert r["ok"] and r["data"]["preset"]["name"] == "nvidia", r
    m = all_models({"models": _toml(repo)["models"]})["nim"]
    assert m.base_url == "https://integrate.api.nvidia.com/v1" and m.key_env == "NVIDIA_API_KEY"
    assert m.context == 128000 and m.rpm == 30 and m.max_wait == 65 and m.max_tokens == 8192
    # explicit flags win over the preset
    assert ak("model", "add", "g", "--preset", "gemini", "--model", "gemini-3-flash-preview", "--context", "64000",
              "--max-wait", "40", "--key-env", "MY_GEMINI")["ok"]
    g = all_models({"models": _toml(repo)["models"]})["g"]
    assert g.context == 64000 and g.max_wait == 40 and g.key_env == "MY_GEMINI" and "generativelanguage" in g.base_url
    bad = ak("model", "add", "x", "--preset", "nope")
    assert not bad["ok"] and "unknown preset" in bad["error"]["what"]
    nob = ak("model", "add", "y")
    assert not nob["ok"] and "--base-url" in nob["error"]["what"]
    # no preset: old defaults unchanged
    assert ak("model", "add", "plain", "--base-url", "http://127.0.0.1:9/v1")["ok"]
    p = all_models({"models": _toml(repo)["models"]})["plain"]
    assert p.context == 32000 and p.rpm == 0 and p.max_wait == 30 and p.max_tokens == 0


def _toml(repo):
    try:
        import tomllib
    except ImportError:  # 3.10
        import tomli as tomllib
    return tomllib.loads((repo / ".agentkit" / "config.toml").read_text())


def test_504_is_upstream_unavailable_retried_then_raised(fake, keyenv):
    """NVIDIA NIM z-ai/glm-5.3-flash answered 504 once: a gateway timeout is the upstream model, so it is retried
    and then maps to 'unavailable' (task pauses, resumable), both as HTTP status and inside a 200 body."""
    fake.default = [{"text": "hi", "before": [{"http": 504}, {"http": 504}]}]
    slept = []
    r = ChatClient(_spec(fake.url), backoff=0.1, sleep=slept.append).chat([{"role": "user", "content": "q"}])
    assert r["choices"][0]["message"]["content"] == "hi" and slept == [0.1, 0.2]
    fake.default = [{"text": "hi", "before": [{"http": 504}] * 3}]
    fake.served_before.clear()
    with pytest.raises(ModelError) as e:
        ChatClient(_spec(fake.url), backoff=0.1, sleep=lambda s: None).chat([{"role": "user", "content": "q"}])
    assert e.value.kind == "unavailable" and e.value.status == 504
    fake.default = [{"text": "hi", "before": [{"body": {"error": {"code": 504, "message": "gateway"}}}] * 3}]
    fake.served_before.clear()
    with pytest.raises(ModelError) as e:
        ChatClient(_spec(fake.url), backoff=0.1, sleep=lambda s: None).chat([{"role": "user", "content": "q"}])
    assert e.value.kind == "unavailable"


def test_retry_after_waited_up_to_max_wait(fake, keyenv):
    """Gemini free tier answers per-minute 429s with Retry-After ~50 s: the default 30 s cap pauses the task,
    a model with max_wait=65 sits it out once and carries on."""
    fake.default = [{"text": "hi", "before": [{"http": 429, "retry_after": "50"}]}]
    with pytest.raises(ModelError) as e:
        ChatClient(_spec(fake.url), sleep=lambda s: None).chat([{"role": "user", "content": "q"}])
    assert e.value.kind == "rate_limited" and e.value.retry_after == 50
    fake.served_before.clear()
    slept = []
    r = ChatClient(_spec(fake.url, max_wait=65), sleep=slept.append).chat([{"role": "user", "content": "q"}])
    assert r["choices"][0]["message"]["content"] == "hi" and slept == [50]


def test_rpm_paces_calls_and_max_tokens_sent(fake, keyenv):
    slept = []
    cl = ChatClient(_spec(fake.url, rpm=60, max_tokens=4096), sleep=slept.append)
    cl.chat([{"role": "user", "content": "a"}])
    cl.chat([{"role": "user", "content": "b"}])
    assert slept and 0.5 < slept[0] <= 1.0, slept  # 60 rpm -> >= 1 s apart
    assert all(r["body"]["max_tokens"] == 4096 for r in fake.requests)
    assert ChatClient(_spec(fake.url, max_tokens=4096)).chat([{"role": "user", "content": "c"}], max_tokens=16)
    assert fake.requests[-1]["body"]["max_tokens"] == 16  # an explicit small budget (model test) still wins


def test_model_test_reasoning_only_reply_is_ok(ak, repo, fake):
    ak.env["FAKE_KEY"] = KEY
    ak("model", "add", "nim", "--base-url", fake.url, "--key-env", "FAKE_KEY")
    fake.default = [{"reasoning": "The user wants the word ok. I should", "finish": "length"}]
    r = ak("model", "test")
    assert r["ok"] and r["data"]["reasoning_only"] is True and r["data"]["finish_reason"] == "length", r


def test_worker_with_reasoning_model(ak, repo, fake):
    """content null + reasoning_content, and one reply cut at the token limit while thinking: the task still
    passes, the worker says what happened, and reasoning is never sent back to the model."""
    project(repo, ak, fake)
    steps = [dict(FIX[0], reasoning="I should read calc.py first."),
             {"reasoning": "Hmm, the bug is a - b. Let me think about all the cases " * 5, "finish": "length"},
             dict(FIX[1], reasoning="Fix it."), FIX[2], FIX[3]]
    fake.add("[REASON]", steps)
    tid = create(ak, "fix add [REASON]")
    assert wait(ak, tid)["verdict"] == "pass"
    reqs = fake.requests_for("[REASON]")
    for r in reqs:
        for m in r["body"]["messages"]:
            assert "reasoning_content" not in m and "reasoning" not in m
    texts = [str(m.get("content")) for m in reqs[-1]["body"]["messages"] if m["role"] == "user"]
    assert any("output token limit" in t for t in texts)
    assert tjson(repo, tid)["verdict"] == "pass"
