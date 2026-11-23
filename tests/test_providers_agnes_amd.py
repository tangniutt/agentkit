# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""C-11: providers 4 and 5 (Agnes AI, AMD Token Factory China). AMD nests errors under detail.error, answers
busy models with 429 model_concurrency_rate_limit_exceeded + Retry-After 1, and Qwen3.8 reasoning models put
their text in `reasoning` (not reasoning_content). Also the hardcoded_return_suspect false alarm on base cases."""
import json

import pytest

from agentkit.models import ChatClient, ModelError, ModelSpec, all_models, error_obj
from agentkit.tasks import _hardcode_suspect
from test_m2_tasks import CHECK, FIX, create, project, tjson, wait
from test_providers import _toml

KEY = "fk" + "-" + "amd" + "7" * 19
BUSY = {"detail": {"error": {"message": "Model 'GLM-5.3-Flash' is at capacity; please retry later or use another model",
                             "type": "rate_limit_error", "code": "model_concurrency_rate_limit_exceeded"}}}


def _spec(url, **kw):
    return ModelSpec(name="fake", base_url=url, model_id="m", key_env="FAKE_KEY", **kw)


@pytest.fixture
def keyenv(monkeypatch):
    monkeypatch.setenv("FAKE_KEY", KEY)


def test_model_add_agnes_and_amd_presets(ak, repo):
    r = ak("model", "add", "agnes", "--preset", "agnes", "--model", "agnes-3.0-flash")
    assert r["ok"] and r["data"]["preset"]["name"] == "agnes", r
    r = ak("model", "add", "amd", "--preset", "amd-cn", "--model", "DeepSeek-V4-Flash")
    assert r["ok"] and "00:00 Beijing" in r["data"]["preset"]["note"], r
    ms = all_models({"models": _toml(repo)["models"]})
    a, m = ms["agnes"], ms["amd"]
    assert a.base_url == "https://apihub.agnes-ai.com/v1" and a.key_env == "AGNES_API_KEY" and a.model_id == "agnes-3.0-flash"
    assert m.base_url == "https://developer.amd.com.cn/radeon/api/v1" and m.key_env == "AMD_CN_API_KEY"
    for s in (a, m):
        assert s.rpm and s.rpm <= 20 and s.max_wait == 65 and s.max_tokens == 8192 and s.price_in == 0
    assert a.rate_wait == 60 and m.rate_wait == 0  # Agnes 429s carry no Retry-After
    # key value never written, only the variable name
    assert "AMD_CN_API_KEY" in (repo / ".agentkit" / "config.toml").read_text()


def test_error_obj_finds_nested_detail_error():
    assert error_obj(BUSY)["code"] == "model_concurrency_rate_limit_exceeded"
    assert error_obj({"error": {"message": "x", "code": 401}})["code"] == 401
    assert error_obj({"detail": "Not authenticated"}) == {"message": "Not authenticated"}
    assert error_obj({"choices": []}) is None


def test_concurrency_429_waits_then_succeeds(fake, keyenv):
    fake.default = [{"text": "hi", "before": [{"http": 429, "body": BUSY, "headers": {"Retry-After": "1"}}] * 2}]
    slept = []
    r = ChatClient(_spec(fake.url, max_wait=65), sleep=slept.append).chat([{"role": "user", "content": "q"}])
    assert r["choices"][0]["message"]["content"] == "hi"
    assert slept == [5.0, 10.0], "busy model: growing waits, not the 1 s Retry-After hammered"


def test_concurrency_429_pauses_after_retries_with_readable_message(fake, keyenv):
    fake.default = [{"text": "hi", "before": [{"http": 429, "body": BUSY, "headers": {"Retry-After": "1"}}] * 10}]
    slept = []
    with pytest.raises(ModelError) as e:
        ChatClient(_spec(fake.url, max_wait=12), sleep=slept.append).chat([{"role": "user", "content": "q"}])
    assert e.value.kind == "rate_limited" and e.value.capacity and e.value.status == 429
    assert "at capacity" in str(e.value) and "model_concurrency_rate_limit_exceeded" in str(e.value)
    assert '{"detail"' not in str(e.value), "the provider's words, not raw JSON"
    # 3 capacity waits capped at max_wait, then the ordinary one-time Retry-After wait (1 s), then raise
    assert slept == [5.0, 10.0, 12, 1.0] and len(fake.requests) == 5


def test_quota_429_is_not_treated_as_capacity(fake, keyenv):
    """A plain 429 (quota) keeps the old policy: wait once if Retry-After is short, then pause."""
    quota = {"detail": {"error": {"message": "daily points exhausted", "type": "rate_limit_error", "code": "quota_exceeded"}}}
    fake.default = [{"text": "hi", "before": [{"http": 429, "body": quota, "headers": {"Retry-After": "3"}}] * 5}]
    slept = []
    with pytest.raises(ModelError) as e:
        ChatClient(_spec(fake.url, max_wait=65), sleep=slept.append).chat([{"role": "user", "content": "q"}])
    assert e.value.kind == "rate_limited" and not e.value.capacity and slept == [3.0]
    assert "daily points exhausted" in str(e.value)


def test_429_without_retry_after_waits_rate_wait_once(fake, keyenv):
    """Agnes free tier: HTTP 429 {"error":{"code":"","message":"You've reached the API rate limit for free users..."}}
    with no Retry-After header. Old behaviour (rate_wait 0) pauses at once; the preset sits out 60 s once."""
    agnes = {"error": {"code": "", "message": "You've reached the API rate limit for free users.", "type": "AgnesAI_error"}}
    fake.default = [{"text": "hi", "before": [{"http": 429, "body": agnes}]}]
    slept = []
    with pytest.raises(ModelError) as e:
        ChatClient(_spec(fake.url, max_wait=65), sleep=slept.append).chat([{"role": "user", "content": "q"}])
    assert e.value.kind == "rate_limited" and e.value.retry_after is None and not e.value.capacity and slept == []
    assert "rate limit for free users" in str(e.value) and '{"error"' not in str(e.value)
    fake.served_before.clear()
    r = ChatClient(_spec(fake.url, max_wait=65, rate_wait=60), sleep=slept.append).chat([{"role": "user", "content": "q"}])
    assert r["choices"][0]["message"]["content"] == "hi" and slept == [60]
    fake.served_before.clear()
    fake.default = [{"text": "hi", "before": [{"http": 429, "body": agnes}] * 3}]
    with pytest.raises(ModelError):  # only once per call, then pause
        ChatClient(_spec(fake.url, max_wait=65, rate_wait=60), sleep=slept.append).chat([{"role": "user", "content": "q"}])
    assert slept == [60, 60]


def test_nested_error_in_200_body_and_auth(fake, keyenv):
    fake.default = [{"text": "hi", "before": [{"body": BUSY}] * 10}]
    with pytest.raises(ModelError) as e:
        ChatClient(_spec(fake.url), sleep=lambda s: None).chat([{"role": "user", "content": "q"}])
    assert e.value.kind == "rate_limited" and e.value.capacity
    fake.served_before.clear()
    fake.default = [{"text": "hi", "before": [{"http": 401, "body": {"detail": {"error": {"message": "invalid key"}}}}]}]
    with pytest.raises(ModelError) as e:
        ChatClient(_spec(fake.url), sleep=lambda s: None).chat([{"role": "user", "content": "q"}])
    assert e.value.kind == "auth" and "invalid key" in str(e.value) and len(fake.served_before) == 1


def test_worker_pauses_on_busy_model_resumable(ak, repo, fake):
    project(repo, ak, fake, model_cfg="max_wait = 0.1")
    fake.add("[BUSY]", [{"call": "read", "args": {"spec": "calc.py"},
                         "before": [{"http": 429, "body": BUSY, "headers": {"Retry-After": "1"}}] * 6}])
    tid = create(ak, "fix add [BUSY]")
    s = wait(ak, tid)
    assert s["status"] == "paused" and "at capacity" in s["why"], s
    assert len(fake.requests_for("[BUSY]")) == 4  # first try + 3 capacity waits; Retry-After 1 s > max_wait 0.1


def test_model_test_reasoning_field(ak, repo, fake):
    """AMD Qwen3.8-27B: content empty, text in `reasoning`."""
    ak.env["FAKE_KEY"] = KEY
    ak("model", "add", "qwen", "--base-url", fake.url, "--key-env", "FAKE_KEY")
    fake.default = [{"text": "", "reasoning": "The user wants ok.", "reasoning_key": "reasoning", "finish": "length"}]
    r = ak("model", "test")
    assert r["ok"] and r["data"]["reasoning_only"] is True, r


def test_worker_never_sends_reasoning_field_back(ak, repo, fake):
    project(repo, ak, fake)
    steps = [dict(FIX[0], reasoning="read first", reasoning_key="reasoning"),
             {"text": "", "reasoning": "thinking " * 40, "reasoning_key": "reasoning", "finish": "length"},
             dict(FIX[1], reasoning="fix it", reasoning_key="reasoning"), FIX[2], FIX[3]]
    fake.add("[RFIELD]", steps)
    tid = create(ak, "fix add [RFIELD]")
    assert wait(ak, tid)["verdict"] == "pass"
    reqs = fake.requests_for("[RFIELD]")
    assert len(reqs) == 5
    for r in reqs:
        assert "thinking thinking" not in json.dumps(r["body"]["messages"])
        for m in r["body"]["messages"]:
            assert "reasoning" not in m and "reasoning_content" not in m
    assert any("output token limit" in str(m.get("content")) for m in reqs[-1]["body"]["messages"] if m["role"] == "user")


def test_hardcode_base_case_not_flagged():
    goal = "add human_bytes(n) that formats e.g. '0 B', '512 B', '1.0 KB'; tests cover 0, 512, 1536"
    assert not _hardcode_suspect('    if n == 0:', '        return "0 B"', goal)
    assert not _hardcode_suspect("    if n == 0: return '0 B'", "", goal)
    assert not _hardcode_suspect("    if n == 0:", "        return 1", "factorial")  # trivial literal
    # still suspect: test inputs special-cased, multi-part conditions, unexplained literals
    assert _hardcode_suspect("    if n == 1536:", '        return "1.5 KB"', goal)
    assert _hardcode_suspect("    if a == 2 and b == 3:", "        return 5", "Make add() return the sum.")
    assert _hardcode_suspect("    if n == 0 or n == 7:", '        return "0 B"', goal)
    assert _hardcode_suspect("    if n == 0:", '        return "zero bytes"', goal)


def test_hardcode_base_case_end_to_end_no_flag(ak, repo, fake):
    """Regression (NVIDIA dogfood 2026-11-23): a correct human_bytes() with `if n == 0: return "0 B"` was flagged."""
    project(repo, ak, fake)
    new = ('def add(a, b):\n    return a + b\n\n\ndef human_bytes(n):\n    if n == 0:\n        return "0 B"\n'
           '    return f"{n} B"\n')
    fake.add("[HB0]", [{"call": "edit", "args": {"path": "calc.py", "old": "def add(a, b):\n    return a - b", "new": new.rstrip()}},
                       FIX[2], FIX[3]])
    tid = create(ak, "fix add and add human_bytes(n) giving '0 B' for 0 [HB0]")
    assert wait(ak, tid)["verdict"] == "pass"
    rep = json.loads((repo / ".agentkit" / "tasks" / tid / "report.json").read_text())
    assert "hardcoded_return_suspect" not in [f["flag"] for f in rep.get("suspicious", [])], rep
    assert tjson(repo, tid)["verdict"] == "pass"
