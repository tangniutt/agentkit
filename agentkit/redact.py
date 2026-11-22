# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
#
# Portions adapted from worker-bridge (src/worker_bridge/redaction.py)
#   https://github.com/trollbot2012/worker-bridge
#   Copyright (c) 2026 worker-bridge contributors
#   Licensed under the MIT License (see NOTICE).
"""Secret redaction applied to every output and every log AgentKit writes."""

from __future__ import annotations

import re
from typing import Any

MASK = "[REDACTED]"

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Authorization headers, optionally bearer-prefixed.
    (re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;'\"]+"), r"\1" + MASK),
    # PEM private-key blocks (multi-line).
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*?PRIVATE KEY-----.*?-----END [A-Z0-9 ]*?PRIVATE KEY-----",
            re.DOTALL,
        ),
        "[REDACTED PRIVATE KEY]",
    ),
    # Credentials embedded in a URL: scheme://user:PASSWORD@host
    (re.compile(r"(?i)(?<![a-z0-9+.\-])([a-z][a-z0-9+.\-]{0,30}://[^:@/\s]{1,256}:)[^@/\s]{1,256}(@)"), r"\1" + MASK + r"\2"),
    # Provider token prefixes (OpenAI/Anthropic sk-, GitHub, Slack, Stripe, HF, Groq, xAI ...).
    (
        re.compile(
            r"\b(?:sk|rk|pk_live|sk_live|ghp|gho|ghu|ghs|ghr|github_pat|glpat|xox[baprs]|hf|gsk|xai|ms)"
            r"[-_][A-Za-z0-9_\-]{16,}"
        ),
        MASK,
    ),
    # AWS access-key identifiers.
    (re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA)[0-9A-Z]{16}\b"), MASK),
    # Google API keys.
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), MASK),
    # key/token/password/secret assignments with a literal-looking value (>=12 chars).
    # Narrower than worker-bridge on purpose so ordinary code (`token = get()`) is not mangled.
    (
        re.compile(
            r"(?i)((?:api[_-]?key|access[_-]?token|auth[_-]?token|secret[_-]?key|client[_-]?secret|password|passwd)"
            r"[\"']?\s*[:=]\s*[\"']?)([A-Za-z0-9_\-/+=.]{12,})"
        ),
        r"\1" + MASK,
    ),
)


_EXTRA: set = set()


def register_secret(value) -> None:
    """Mask this exact value everywhere from now on (configured model keys, M-17)."""
    if value and len(str(value)) >= 6:
        _EXTRA.add(str(value))


def redact_text(value: str) -> str:
    result = str(value)
    for sec in _EXTRA:
        if sec in result:
            result = result.replace(sec, MASK)
    for pattern, replacement in _PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def redact(value: Any) -> Any:
    """Recursively redact strings inside dicts / lists."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value
