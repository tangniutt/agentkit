# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""M-12 model access: `agentkit model add/list/test` and a stdlib OpenAI-compatible chat client.

Keys are never stored: config holds only the NAME of an environment variable (`key_env`).
The value is read at call time, registered with the redactor, and sent only in the
Authorization header (M-17). Failure policy (baseline section 4):
  timeout / network -> 2 retries with exponential backoff, then ModelError(kind="timeout")
  429               -> honor Retry-After (header or Gemini RetryInfo) once if short, then kind="rate_limited"
  401 / 403         -> kind="auth", no retry
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

from .config import load_config, load_user_config, set_table, set_user_table
from .core import AKError, Ctx, Result
from .redact import redact, redact_text, register_secret

TIERS = ("low", "mid", "high")
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
RETRY_AFTER_CAP_S = 30.0

# `agentkit model add NAME --preset P --model ID`: provider defaults; any explicit flag wins.
# rpm / max_wait are pacing for free tiers (requests per minute this worker sends; longest Retry-After
# it sits out once per call before pausing the task). Values are conservative, not provider promises.
PRESETS = {
    "nvidia": {"base_url": "https://integrate.api.nvidia.com/v1", "key_env": "NVIDIA_API_KEY", "context": 128000,
               "rpm": 30, "max_wait": 65, "max_tokens": 8192,
               "note": "NVIDIA NIM (build.nvidia.com) free trial: ~40 RPM, testing use only"},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "key_env": "GEMINI_API_KEY",
               "context": 128000, "max_wait": 65,
               "note": "Gemini OpenAI-compatible endpoint; free tier has low per-minute and per-day limits"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "key_env": "OPENROUTER_API_KEY", "context": 32000,
                   "max_wait": 65, "note": "use a ':free' model id or pass prices"},
    # Agnes AI (international): flash models zero-priced as a vendor promotion (vendor statement); free tier measured
    # 2026-11-23 at ~10 requests per rolling minute, 429 has no Retry-After -> rpm 6 + rate_wait 60.
    # Pro models (agnes-2.5-pro-*, agnes-3.0-pro) may be paid: don't default to them.
    "agnes": {"base_url": "https://apihub.agnes-ai.com/v1", "key_env": "AGNES_API_KEY", "context": 128000,
              "rpm": 6, "max_wait": 65, "max_tokens": 8192, "rate_wait": 60,
              "note": "Agnes AI: use a flash model (agnes-3.0-flash; promotional zero price); free tier ~10 requests "
                      "per rolling minute, 429 without Retry-After"},
    # AMD Radeon Cloud Token Factory (China site): daily points quota resets 00:00 Asia/Shanghai; typical ~20 RPM and
    # 8 concurrent requests per key; busy models answer 429 model_concurrency_rate_limit_exceeded (retried, then pause).
    "amd-cn": {"base_url": "https://developer.amd.com.cn/radeon/api/v1", "key_env": "AMD_CN_API_KEY", "context": 128000,
               "rpm": 15, "max_wait": 65, "max_tokens": 8192,
               "note": "AMD Token Factory (China): daily points reset 00:00 Beijing; ~20 RPM, 8 concurrent per key"},
}

# 429 codes that mean "this model is busy right now" (not "your quota is used up"): worth a few short waits.
CAPACITY_CODES = ("model_concurrency_rate_limit_exceeded", "concurrency", "at capacity", "overloaded")
CAPACITY_RETRIES = 3


class ModelError(Exception):
    def __init__(self, kind: str, msg: str, retry_after: Optional[float] = None, status: Optional[int] = None):
        super().__init__(msg)
        self.kind = kind  # auth | rate_limited | timeout | network | unavailable | bad_response | http
        self.retry_after = retry_after
        self.status = status
        self.capacity = False  # 429 because the model is busy (concurrency), not because a quota is used up


def _capacity(code, msg: str) -> bool:
    t = f"{code or ''} {msg or ''}".lower()
    return any(c in t for c in CAPACITY_CODES)


def error_obj(data) -> Optional[dict]:
    """The provider's error object, wherever it nests: {"error": {...}} (OpenAI / OpenRouter) or
    {"detail": {"error": {...}}} (AMD Token Factory, FastAPI style), or {"detail": "text"}."""
    if not isinstance(data, dict):
        return None
    err = data.get("error")
    det = data.get("detail")
    if not err and isinstance(det, dict):
        err = det.get("error") or (det if det.get("message") or det.get("code") else None)
    elif not err and isinstance(det, str) and det:
        err = {"message": det}
    if err and not isinstance(err, dict):
        err = {"message": str(err)}
    return err or None


def _rate_limited(msg: str, retry_after, code=None, text: str = "") -> ModelError:
    e = ModelError("rate_limited", msg, retry_after, status=429)
    e.capacity = _capacity(code, text or msg)
    return e


@dataclass
class ModelSpec:
    name: str
    base_url: str
    model_id: str
    key_env: Optional[str] = None
    context: int = 32000
    price_in: float = 0.0  # USD per 1M input tokens
    price_out: float = 0.0
    tier: str = "low"
    timeout: float = 120.0
    rpm: float = 0.0          # >0: space calls at least 60/rpm s apart (free-tier pacing)
    max_wait: float = RETRY_AFTER_CAP_S  # longest Retry-After sat out (once per call) before pausing
    max_tokens: int = 0       # >0: sent as max_tokens (reasoning models need room to think AND call a tool)
    rate_wait: float = 0.0    # >0: a 429 WITHOUT Retry-After is sat out this long once (per-minute limits that
                              # don't say when they reset, e.g. Agnes free tier); 0 = pause at once (old behaviour)

    def key(self) -> Optional[str]:
        if not self.key_env:
            return None
        v = os.environ.get(self.key_env)
        if v:
            register_secret(v)
        return v

    def cost(self, tin: int, tout: int) -> float:
        return (tin * self.price_in + tout * self.price_out) / 1e6


def all_models(cfg: dict) -> dict:
    out = {}
    for name, m in (cfg.get("models") or {}).items():
        if not isinstance(m, dict) or not m.get("base_url"):
            continue
        out[name] = ModelSpec(
            name=name, base_url=str(m["base_url"]), model_id=str(m.get("model") or name),
            key_env=m.get("key_env") or None, context=int(m.get("context", 32000)),
            price_in=float(m.get("price_in", 0)), price_out=float(m.get("price_out", 0)),
            tier=str(m.get("tier", "low")), timeout=float(m.get("timeout", 120)),
            rpm=float(m.get("rpm", 0) or 0), max_wait=float(m.get("max_wait", RETRY_AFTER_CAP_S)),
            max_tokens=int(m.get("max_tokens", 0) or 0), rate_wait=float(m.get("rate_wait", 0) or 0),
        )
    return out


def pick(cfg: dict, name: Optional[str] = None, tier: Optional[str] = None) -> ModelSpec:
    ms = all_models(cfg)
    if not ms:
        raise AKError("no worker model configured", "direct mode", "agentkit model add NAME --base-url URL --key-env VAR")
    if name:
        if name not in ms:
            raise AKError(f"unknown model '{name}'", f"configured: {', '.join(ms)}", "agentkit model list")
        return ms[name]
    if tier:
        for m in ms.values():
            if m.tier == tier:
                return m
        raise AKError(f"no model with tier '{tier}'", f"configured: {', '.join(f'{m.name}({m.tier})' for m in ms.values())}",
                      f"agentkit model add NAME --tier {tier} ...")
    default = (cfg.get("worker") or {}).get("default_model")
    if default in ms:
        return ms[default]
    low = [m for m in ms.values() if m.tier == "low"]
    return (low or list(ms.values()))[0]


# ---------------------------------------------------------------- client


def _retry_after(headers, body: str) -> Optional[float]:
    v = headers.get("Retry-After") if headers else None
    if v:
        try:
            return float(v)
        except ValueError:
            pass
    reset = headers.get("X-RateLimit-Reset") if headers else None  # OpenRouter: epoch milliseconds
    if not reset:
        m = re.search(r'"X-RateLimit-Reset"\s*:\s*"?(\d{10,13})', body or "")
        reset = m.group(1) if m else None
    if reset:
        try:
            r = float(reset)
            r = r / 1000.0 if r > 1e11 else r
            return max(0.0, round(r - time.time(), 1))
        except ValueError:
            pass
    m = re.search(r'"retryDelay"\s*:\s*"([\d.]+)s"', body or "")
    if m:
        return float(m.group(1))
    m = re.search(r"retry in ([\d.]+)\s*s", body or "", re.I)
    return float(m.group(1)) if m else None


def is_openrouter(base_url: str) -> bool:
    host = (urllib.parse.urlsplit(base_url).hostname or "").lower()
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def _raise_body_error(data, raw: str) -> None:
    """Gateways such as OpenRouter report upstream/provider failures inside an HTTP 200 body:
    {"error": {"code": 429, "message": ..., "metadata": {...}}} at the top level or on a choice
    (finish_reason "error"). Map them like the equivalent HTTP status."""
    if not isinstance(data, dict):
        return
    err = error_obj(data)
    if not err:
        for ch in data.get("choices") or []:
            if isinstance(ch, dict) and (ch.get("error") or ch.get("finish_reason") == "error"):
                err = ch.get("error") or {"message": "provider returned finish_reason=error"}
                break
    if not err:
        return
    if not isinstance(err, dict):
        err = {"message": str(err)}
    code = raw_code = err.get("code")
    try:
        code = int(code)
    except (TypeError, ValueError):
        code = None
    msg = redact_text(str(err.get("message") or ""))[:200]
    if code is None and (("rate_limit" in str(err.get("type") or "").lower())
                         or (isinstance(raw_code, str) and ("rate_limit" in raw_code.lower() or _capacity(raw_code, "")))):
        code = 429  # string codes (AMD: type rate_limit_error, code model_concurrency_rate_limit_exceeded)
    meta = err.get("metadata") or {}
    prov = meta.get("provider_name") if isinstance(meta, dict) else None
    where = f" [provider {prov}]" if prov else ""
    if code in (401, 403):
        raise ModelError("auth", f"{code} in body: {msg}{where}", status=code)
    if code == 429:
        tag = f" [{raw_code}]" if isinstance(raw_code, str) and raw_code else ""
        raise _rate_limited(f"429 in body: {msg}{tag}{where}", _retry_after({}, raw), raw_code, msg)
    if code == 408:
        raise ModelError("timeout", f"{code} in body: {msg}{where}", status=code)
    if code in (500, 502, 503, 504) or code is None:
        raise ModelError("unavailable", f"provider error in body: {msg}{where}", status=code)
    if code == 402:
        raise ModelError("http", f"402 in body (credits / paid model): {msg}{where}", status=402)
    raise ModelError("http", f"{code} in body: {msg}{where}", status=code)


class ChatClient:
    """Minimal OpenAI-compatible /chat/completions client (urllib only)."""

    def __init__(self, spec: ModelSpec, retries: int = 2, backoff: float = 1.0, sleep=time.sleep,
                 capacity_retries: int = CAPACITY_RETRIES):
        self.spec = spec
        self.capacity_retries = capacity_retries
        self.retries = retries
        self.backoff = backoff
        self.sleep = sleep
        self._last_call = 0.0

    def _pace(self):
        if self.spec.rpm and self.spec.rpm > 0:
            gap = 60.0 / self.spec.rpm - (time.monotonic() - self._last_call)
            if self._last_call and gap > 0:
                self.sleep(gap)
        self._last_call = time.monotonic()

    def _post(self, payload: dict, timeout: float) -> dict:
        url = self.spec.base_url.rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if is_openrouter(self.spec.base_url):  # optional attribution headers (openrouter.ai/docs/api-reference)
            headers["HTTP-Referer"] = "https://github.com/tangniutt/agentkit"
            headers["X-Title"] = "AgentKit"
        key = self.spec.key()
        if key:
            headers["Authorization"] = "Bearer " + key
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        opener = (urllib.request.build_opener(urllib.request.ProxyHandler({}))
                  if host in ("127.0.0.1", "localhost", "::1") and os.environ.get("AGENTKIT_NETWORK_MODE") != "allowlist"
                  else urllib.request.build_opener())
        try:
            with opener.open(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            short = redact_text(body)[:300]
            try:
                eo = error_obj(json.loads(body)) or {}
            except ValueError:
                eo = {}
            if eo.get("message"):  # nested errors (detail.error) -> say the provider's words, not raw JSON
                c = eo.get("code") or eo.get("type")
                short = redact_text(str(eo.get("message")))[:240] + (f" [{c}]" if c else "")
            if e.code in (401, 403):
                raise ModelError("auth", f"HTTP {e.code}: invalid or expired key ({short})", status=e.code)
            if e.code == 429:
                raise _rate_limited(f"HTTP 429: {short}", _retry_after(e.headers, body), eo.get("code"), short)
            if e.code == 408:
                raise ModelError("timeout", f"HTTP {e.code}", status=e.code)
            if e.code == 504:  # gateway timeout: the upstream model is slow / down, not our socket
                raise ModelError("unavailable", f"HTTP 504 (upstream gateway timeout): {short[:120]}",
                                 _retry_after(e.headers, body), status=504)
            if e.code in (500, 502, 503):
                raise ModelError("unavailable", f"HTTP {e.code}: {short[:120]}", _retry_after(e.headers, body), status=e.code)
            raise ModelError("http", f"HTTP {e.code}: {short}", status=e.code)
        except (socket.timeout, TimeoutError):
            raise ModelError("timeout", f"no response in {timeout:g}s")
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                raise ModelError("timeout", f"no response in {timeout:g}s")
            raise ModelError("network", redact_text(str(e.reason))[:200])
        except OSError as e:
            raise ModelError("network", redact_text(str(e))[:200])
        try:
            data = json.loads(raw)
        except ValueError:
            raise ModelError("bad_response", "response is not an OpenAI chat completion: " + redact_text(raw)[:200])
        _raise_body_error(data, raw)
        try:
            data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise ModelError("bad_response", "response is not an OpenAI chat completion: " + redact_text(raw)[:200])
        return data

    def chat(self, messages: list, tools: Optional[list] = None, timeout: Optional[float] = None,
             max_tokens: Optional[int] = None) -> dict:
        # defence in depth (M-17): nothing that looks like a secret, or equals a configured key, leaves in the body
        payload: dict = {"model": self.spec.model_id, "messages": redact(messages)}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        max_tokens = max_tokens or self.spec.max_tokens
        if max_tokens:
            payload["max_tokens"] = max_tokens
        timeout = timeout or self.spec.timeout
        attempt = 0
        waited_429 = False
        busy = 0
        while True:
            try:
                self._pace()
                return self._post(payload, timeout)
            except ModelError as e:
                if e.kind in ("timeout", "network", "unavailable") and attempt < self.retries:
                    self.sleep(self.backoff * (2 ** attempt))
                    attempt += 1
                    continue
                if e.kind == "rate_limited" and e.capacity and busy < self.capacity_retries:
                    # model busy (concurrency 429, Retry-After ~1 s): a few growing waits, then pause the task
                    self.sleep(min(self.spec.max_wait, max(e.retry_after or 0, 5.0 * (2 ** busy))))
                    busy += 1
                    continue
                ra = e.retry_after if e.retry_after is not None else (self.spec.rate_wait or None)
                if e.kind == "rate_limited" and not waited_429 and ra is not None and ra <= self.spec.max_wait:
                    self.sleep(ra)
                    waited_429 = True
                    continue
                raise


def reasoning_text(msg: dict) -> str:
    """Reasoning models put their thinking in reasoning_content (NVIDIA NIM, DeepSeek, Agnes 2.5) or
    reasoning (AMD Token Factory Qwen3.8). It is never sent back to the model (the worker keeps only content / tool_calls)."""
    return str(msg.get("reasoning_content") or msg.get("reasoning") or "")


def usage_of(resp: dict, messages: list) -> tuple:
    u = resp.get("usage") or {}
    tin = u.get("prompt_tokens")
    tout = u.get("completion_tokens")
    if tin is None:
        tin = len(json.dumps(messages)) // 4
    if tout is None:
        tout = len(json.dumps(resp["choices"][0]["message"])) // 4
    return int(tin), int(tout)


# ---------------------------------------------------------------- CLI


def cmd_model(ctx: Ctx, a) -> Result:
    sub = a.model_cmd or "list"
    if sub == "add":
        if not NAME_RE.match(a.name):
            raise AKError("bad model name", "use letters, digits, - or _ (the provider id goes in --model)", None)
        if a.tier not in TIERS:
            raise AKError(f"bad tier {a.tier}", "use low | mid | high", None)
        if a.key_env and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", a.key_env):
            raise AKError("--key-env takes the NAME of an environment variable, not a key",
                          "keys are never stored in config (M-17)", "export MY_KEY=...; agentkit model add NAME --key-env MY_KEY ...")
        pre = {}
        if getattr(a, "preset", None):
            if a.preset not in PRESETS:
                raise AKError(f"unknown preset '{a.preset}'", f"presets: {', '.join(PRESETS)}", None)
            pre = PRESETS[a.preset]
        base_url = a.base_url or pre.get("base_url")
        if not base_url:
            raise AKError("--base-url is required (or --preset)", f"presets: {', '.join(PRESETS)}",
                          f"agentkit model add {a.name} --preset nvidia --model nvidia/nemotron-3.5-lightning-30b-a3b")
        a.base_url = base_url
        a.key_env = a.key_env or pre.get("key_env")
        if a.key_env and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", a.key_env):
            raise AKError("--key-env takes the NAME of an environment variable, not a key",
                          "keys are never stored in config (M-17)", "export MY_KEY=...; agentkit model add NAME --key-env MY_KEY ...")
        vals = {"base_url": base_url, "model": a.model or a.name, "key_env": a.key_env,
                "context": a.context or pre.get("context", 32000),
                "price_in": a.price_in, "price_out": a.price_out, "tier": a.tier}
        for k in ("rpm", "max_wait", "max_tokens", "rate_wait"):
            v = getattr(a, k, None)
            v = v if v is not None else pre.get(k)
            if v is not None:
                vals[k] = v
        global_config = bool(getattr(a, "global_config", False))
        p = set_user_table(f"models.{a.name}", vals) if global_config else set_table(ctx.root, f"models.{a.name}", vals)
        if a.default:
            set_table(ctx.root, "worker", {**(load_config(ctx.root).get("worker") or {}), "default_model": a.name})
        data = {"added": a.name, "scope": "global" if global_config else "project",
                "config": str(p) if global_config else ctx.rel(p),
                "key_env_set": bool(a.key_env and os.environ.get(a.key_env))}
        if pre:
            data["preset"] = {"name": a.preset, "note": pre["note"]}
        if a.key_env and not data["key_env_set"]:
            data["hint"] = f"environment variable {a.key_env} is not set in this shell"
        mid = a.model or a.name
        if is_openrouter(a.base_url) and not mid.endswith(":free") and not (a.price_in or a.price_out):
            data["warning"] = ("OpenRouter model without ':free' suffix and price 0: --budget-usd cannot protect you; "
                               "pass --price-in/--price-out, or use a ':free' model id")
        return Result(data)
    cfg = load_config(ctx.root)
    if sub == "list":
        ms = all_models(cfg)
        return Result({"models": [
            {"name": m.name, "model": m.model_id, "tier": m.tier, "context": m.context, "price": [m.price_in, m.price_out],
             "key_env": m.key_env, "key_set": bool(m.key_env and os.environ.get(m.key_env)) if m.key_env else None,
             "base_url": m.base_url, "scope": (cfg.get("_model_scopes") or {}).get(m.name, "project")} for m in ms.values()],
            "default": pick(cfg).name if ms else None})
    if sub == "remove":
        if getattr(a, "global_config", False):
            user_models = load_user_config().get("models") or {}
            if a.name not in user_models:
                raise AKError(f"unknown global model '{a.name}'", "", "agentkit model list")
            set_user_table(f"models.{a.name}", None)
            return Result({"removed": a.name, "scope": "global"})
        if a.name not in all_models(cfg):
            raise AKError(f"unknown model '{a.name}'", "", "agentkit model list")
        set_table(ctx.root, f"models.{a.name}", None)
        return Result({"removed": a.name, "scope": "project"})
    if sub == "test":
        m = pick(cfg, a.name)
        if m.key_env and not os.environ.get(m.key_env):
            raise AKError(f"environment variable {m.key_env} is not set", "the key is read from it at call time",
                          f"export {m.key_env}=... then agentkit model test {m.name}")
        t0 = time.monotonic()
        try:
            cl = ChatClient(m, retries=0, capacity_retries=0)
            resp = cl.chat([{"role": "user", "content": "Reply with the single word: ok"}], timeout=a.timeout, max_tokens=16)
        except ModelError as e:
            raise AKError(f"model test failed: {e.kind}", str(e)[:300],
                          "check base_url / model id / key" if e.kind != "rate_limited" else "rate limited; retry later",
                          data={"model": m.name, "kind": e.kind, "retry_after": e.retry_after})
        ch = resp["choices"][0]
        msg = ch["message"]
        out = {"model": m.name, "ok": True, "latency_s": round(time.monotonic() - t0, 2),
               "reply": str(msg.get("content") or "")[:60], "usage": resp.get("usage")}
        if not msg.get("content") and reasoning_text(msg):
            # reasoning model: the short test budget went to thinking; the endpoint works
            out["reasoning_only"] = True
            out["finish_reason"] = ch.get("finish_reason")
        return Result(out)
    raise AKError(f"unknown model subcommand {sub}", "", "agentkit model list")
