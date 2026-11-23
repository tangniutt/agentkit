# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""M-12 model config + OpenAI-compatible client: retries, 429, timeouts, invalid key."""
import pytest

from agentkit.models import ChatClient, ModelError, ModelSpec

# fake key assembled at runtime (never a real credential, never a literal that scanners flag)
KEY = "fk" + "-" + "unit" + "7" * 18


def _spec(url, **kw):
    return ModelSpec(name="fake", base_url=url, model_id="m", key_env="FAKE_KEY", **kw)


@pytest.fixture
def keyenv(monkeypatch):
    monkeypatch.setenv("FAKE_KEY", KEY)


def test_model_add_list_never_stores_key(ak, repo, fake):
    ak.env["FAKE_KEY"] = KEY
    r = ak("model", "add", "cheap", "--base-url", fake.url, "--key-env", "FAKE_KEY", "--model", "x/y-1.5",
           "--price-in", "0.1", "--price-out", "0.4", "--tier", "low", "--context", "8000")
    assert r["ok"] and r["data"]["key_env_set"] is True
    cfg = (repo / ".agentkit" / "config.toml").read_text()
    assert "FAKE_KEY" in cfg and KEY not in cfg
    lst = ak("model", "list")["data"]
    assert lst["models"][0]["name"] == "cheap" and lst["models"][0]["key_set"] is True and lst["default"] == "cheap"
    bad = ak("model", "add", "x", "--base-url", fake.url, "--key-env", "sk-" + "a" * 30)
    assert not bad["ok"] and "NAME of an environment variable" in bad["error"]["what"]
    assert ak("model", "add", "y", "--base-url", fake.url, "--tier", "ultra")["ok"] is False
    assert ak("model", "remove", "cheap")["ok"] and ak("model", "list")["data"]["models"] == []


def test_model_test_ok_and_invalid_key(ak, repo, fake):
    ak.env["FAKE_KEY"] = KEY
    ak("model", "add", "fake", "--base-url", fake.url, "--key-env", "FAKE_KEY")
    r = ak("model", "test")
    assert r["ok"] and r["data"]["reply"] == "ok"
    assert fake.requests[-1]["headers"]["Authorization"] == "Bearer " + KEY
    fake.default = [{"text": "x", "before": [{"http": 401}] * 5}]
    n = len(fake.requests)
    r = ak("model", "test")
    assert not r["ok"] and r["data"]["kind"] == "auth"
    assert len(fake.requests) == n + 1, "invalid key must not be retried"
    assert KEY not in r["_raw"]
    # missing env var
    r = ak("model", "test", env={"FAKE_KEY": ""})
    assert not r["ok"] and "FAKE_KEY" in r["error"]["what"]


def test_client_429_honors_retry_after_then_succeeds(fake, keyenv):
    fake.default = [{"text": "hi", "before": [{"http": 429, "retry_after": 1}]}]
    slept = []
    cl = ChatClient(_spec(fake.url), sleep=slept.append)
    resp = cl.chat([{"role": "user", "content": "q"}])
    assert resp["choices"][0]["message"]["content"] == "hi"
    assert slept == [1.0] and len(fake.requests) == 2


def test_client_429_twice_raises_rate_limited(fake, keyenv):
    fake.default = [{"text": "hi", "before": [{"http": 429, "retry_delay": 2}, {"http": 429, "retry_delay": 2}]}]
    slept = []
    with pytest.raises(ModelError) as e:
        ChatClient(_spec(fake.url), sleep=slept.append).chat([{"role": "user", "content": "q"}])
    assert e.value.kind == "rate_limited" and e.value.retry_after == 2.0  # Gemini RetryInfo parsed
    assert slept == [2.0] and len(fake.requests) == 2


def test_client_long_retry_after_is_not_waited(fake, keyenv):
    fake.default = [{"text": "hi", "before": [{"http": 429, "retry_after": 3600}]}]
    slept = []
    with pytest.raises(ModelError) as e:
        ChatClient(_spec(fake.url), sleep=slept.append).chat([{"role": "user", "content": "q"}])
    assert e.value.kind == "rate_limited" and slept == []


def test_client_timeout_retries_twice_with_backoff(fake, keyenv):
    fake.default = [{"text": "hi", "before": [{"sleep": 1.5}, {"sleep": 1.5}, {"sleep": 1.5}]}]
    slept = []
    with pytest.raises(ModelError) as e:
        ChatClient(_spec(fake.url, timeout=0.4), backoff=0.5, sleep=slept.append).chat([{"role": "user", "content": "q"}])
    assert e.value.kind == "timeout"
    assert slept == [0.5, 1.0] and len(fake.requests) == 3


def test_client_timeout_then_recovers(fake, keyenv):
    fake.default = [{"text": "hi", "before": [{"sleep": 1.5}]}]
    resp = ChatClient(_spec(fake.url, timeout=0.4), backoff=0.01).chat([{"role": "user", "content": "q"}])
    assert resp["choices"][0]["message"]["content"] == "hi"


def test_client_bad_response_and_network(keyenv):
    with pytest.raises(ModelError) as e:
        ChatClient(_spec("http://127.0.0.1:9/v1"), retries=0).chat([{"role": "user", "content": "q"}])
    assert e.value.kind in ("network", "timeout")


def test_doctor_reports_models(ak, repo, fake):
    ak.env["FAKE_KEY"] = KEY
    ak("model", "add", "fake", "--base-url", fake.url, "--key-env", "FAKE_KEY")
    r = ak("doctor", "--online")
    m = [c for c in r["data"]["checks"] if c["name"] == "model:fake"][0]
    assert m["ok"] and "reachable" in m["value"]
    r = ak("doctor", env={"FAKE_KEY": ""})
    m = [c for c in r["data"]["checks"] if c["name"] == "model:fake"][0]
    assert not m["ok"] and "FAKE_KEY" in m["fix"]


def test_client_503_is_retried_then_unavailable(fake, keyenv):
    fake.default = [{"text": "hi", "before": [{"http": 503}, {"http": 503}, {"http": 503}]}]
    slept = []
    with pytest.raises(ModelError) as e:
        ChatClient(_spec(fake.url), backoff=0.1, sleep=slept.append).chat([{"role": "user", "content": "q"}])
    assert e.value.kind == "unavailable" and slept == [0.1, 0.2] and len(fake.requests) == 3


# ---------------------------------------------------------------- OpenRouter specifics


def test_provider_errors_inside_http_200_are_mapped(fake, keyenv):
    """OpenRouter reports upstream failures in a 200 body; they must not count as a completion."""
    slept = []
    fake.default = [{"text": "hi", "before": [
        {"body": {"error": {"code": 502, "message": "upstream died", "metadata": {"provider_name": "X"}}}},
        {"body": {"choices": [{"index": 0, "finish_reason": "error", "error": {"code": 503, "message": "overloaded"},
                               "message": {"role": "assistant", "content": ""}}]}},
    ]}]
    resp = ChatClient(_spec(fake.url), sleep=slept.append).chat([{"role": "user", "content": "q"}])
    assert resp["choices"][0]["message"]["content"] == "hi" and len(slept) == 2, "provider errors retried like 5xx"
    fake.served_before.clear()
    fake.default = [{"text": "hi", "before": [{"body": {"error": {"code": 401, "message": "User not found."}}}] * 3}]
    n = len(fake.requests)
    with pytest.raises(ModelError) as e:
        ChatClient(_spec(fake.url), sleep=slept.append).chat([{"role": "user", "content": "q"}])
    assert e.value.kind == "auth" and len(fake.requests) == n + 1
    fake.served_before.clear()
    fake.default = [{"text": "hi", "before": [{"body": {"error": {"code": 402, "message": "Insufficient credits"}}}]}]
    with pytest.raises(ModelError) as e:
        ChatClient(_spec(fake.url), sleep=slept.append).chat([{"role": "user", "content": "q"}])
    assert e.value.kind == "http" and "402" in str(e.value)


def test_openrouter_429_reset_header_and_body(fake, keyenv):
    import time as _t

    slept = []
    reset_ms = int((_t.time() + 5) * 1000)
    fake.default = [{"text": "hi", "before": [{"http": 429, "body": {"error": {"code": 429, "message": "rate limited"}},
                                                "headers": {"X-RateLimit-Reset": str(reset_ms)}}]}]
    resp = ChatClient(_spec(fake.url), sleep=slept.append).chat([{"role": "user", "content": "q"}])
    assert resp["choices"][0]["message"]["content"] == "hi" and 3 <= slept[0] <= 6
    # free daily quota: reset hours away -> no waiting, paused as rate_limited; also when the 429 is in a 200 body
    far = int((_t.time() + 7200) * 1000)
    fake.served_before.clear()
    fake.default = [{"text": "hi", "before": [{"body": {"error": {"code": 429, "message": "free-models-per-day",
                                                                 "metadata": {"headers": {"X-RateLimit-Reset": str(far)}}}}}] * 3}]
    slept.clear()
    n = len(fake.requests)
    with pytest.raises(ModelError) as e:
        ChatClient(_spec(fake.url), sleep=slept.append).chat([{"role": "user", "content": "q"}])
    assert e.value.kind == "rate_limited" and e.value.retry_after > 3000 and slept == [] and len(fake.requests) == n + 1


def test_openrouter_attribution_headers_and_paid_model_warning(ak, repo, keyenv):
    import json as _j

    from agentkit.models import ChatClient as CC
    from agentkit.models import ModelSpec as MS

    seen = {}

    class Stop(Exception):
        pass

    def fake_open(self, req, timeout=None):
        seen.update({k.lower(): v for k, v in req.header_items()})
        seen["body"] = _j.loads(req.data)
        raise Stop()

    import urllib.request

    orig = urllib.request.OpenerDirector.open
    urllib.request.OpenerDirector.open = fake_open
    try:
        with pytest.raises(Stop):
            CC(MS(name="o", base_url="https://openrouter.ai/api/v1", model_id="google/gemma-4-31b-it:free", key_env="FAKE_KEY"),
               retries=0).chat([{"role": "user", "content": "q"}])
    finally:
        urllib.request.OpenerDirector.open = orig
    assert seen["http-referer"].startswith("https://github.com/tangniutt/agentkit") and seen["x-title"] == "AgentKit"
    assert seen["body"]["model"].endswith(":free")
    r = ak("model", "add", "paid", "--base-url", "https://openrouter.ai/api/v1", "--model", "openai/gpt-x")
    assert r["ok"] and "warning" in r["data"]
    r = ak("model", "add", "free", "--base-url", "https://openrouter.ai/api/v1", "--model", "google/gemma-4-31b-it:free")
    assert r["ok"] and "warning" not in r["data"]
