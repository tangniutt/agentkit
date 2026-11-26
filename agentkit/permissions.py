# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""M-22 command permissions for `run` / `verify`: built-in deny list + config allow/deny.

Rule ideas from Grok Workhorse's guard plugins (MIT) and Claude Code / Codex approval
policies; implementation is our own. This is a guard rail against accidents, not a sandbox.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Optional

_SEP = re.compile(r"\|\||&&|;|\n|(?<![|>])\|(?!\|)|&(?!&)")


@dataclass
class Verdict:
    allowed: bool
    rule: Optional[str] = None
    why: Optional[str] = None
    segment: Optional[str] = None


def _tokens(seg: str) -> list:
    try:
        return shlex.split(seg, comments=False)
    except ValueError:
        return seg.split()


def _strip_prefix(tok: list) -> list:
    # Strip common wrappers before applying built-in rules. This remains a guard rail,
    # not a shell parser or sandbox.
    i = 0
    while i < len(tok):
        t = tok[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t):
            i += 1
            continue
        if t == "env":
            i += 1
            while i < len(tok):
                x = tok[i]
                if x in ("-u", "--unset", "-C", "--chdir"):
                    i += 2
                elif x.startswith("-") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", x):
                    i += 1
                else:
                    break
            continue
        if t in ("timeout", "nice"):
            i += 1
            while i < len(tok) and tok[i].startswith("-"):
                if tok[i] in ("-n", "--adjustment") and i + 1 < len(tok):
                    i += 2
                else:
                    i += 1
            if t == "timeout" and i < len(tok) and re.fullmatch(r"\d+(?:\.\d+)?[smhd]?", tok[i]):
                i += 1
            continue
        if t in ("command", "nohup", "time", "exec", "builtin"):
            i += 1
            while i < len(tok) and tok[i].startswith("-"):
                i += 1
            continue
        break
    return tok[i:]


def _git_sub(tok: list) -> Optional[str]:
    i = 1
    while i < len(tok):
        t = tok[i]
        if t in ("-c", "-C", "--git-dir", "--work-tree", "--namespace"):
            i += 2
        elif t.startswith("-"):
            i += 1
        else:
            return t
    return None


_DANGER_RM_TARGETS = {"/", "/*", "~", "~/*", "$HOME", "$HOME/*", "${HOME}", "${HOME}/*", "/.", "/..", "~/.", ""}
_PIPE_TO_SHELL = re.compile(r"\b(curl|wget|fetch)\b[^|;&]*\|\s*(sudo\s+)?(env\s+)?(ba|z|da|k|fi)?sh\b")
_SUBST_SHELL = re.compile(r"\b(ba|z|da)?sh\s+(-c\s+)?[\"']?\$\((curl|wget)\b|\bsource\s+<\(\s*(curl|wget)\b|\beval\s+\"?\$\((curl|wget)\b")
_SSH = re.compile(r"(~|\$HOME|\$\{HOME\}|/home/[^/\s]+|/root|^|\s)/?\.ssh(/|\b)")
_ENVFILE = re.compile(r"(^|[\s/'\"=<:])\.env(\.(?!example\b|sample\b|template\b|dist\b)[\w.-]+)?(?=$|[\s'\";|&)>])")


def builtin_rules(seg: str) -> Optional[Verdict]:
    tok = _strip_prefix(_tokens(seg))
    if not tok:
        return None
    head = tok[0].rsplit("/", 1)[-1]
    if "$(" in tok[0] or "`" in tok[0]:
        return Verdict(False, "obfuscated-command", "command substitution in command position is not allowed", seg)
    if head in ("sudo", "doas", "su", "pkexec"):
        return Verdict(False, "sudo", "privilege escalation is not allowed", seg)
    if head == "git":
        sub = _git_sub(tok)
        if sub == "push":
            return Verdict(False, "git-push", "pushing is the human's decision (no auto push)", seg)
        if sub == "commit":
            return Verdict(False, "git-commit", "committing is the host's decision; leave changes uncommitted", seg)
    if head == "rm":
        flags = "".join(t.lstrip("-") for t in tok[1:] if t.startswith("-") and not t.startswith("--"))
        long = [t for t in tok[1:] if t.startswith("--")]
        recursive = "r" in flags.lower() or "--recursive" in long
        targets = [t for t in tok[1:] if not t.startswith("-")]
        norm = [t if t == "/" else t.rstrip("/") for t in targets]
        if "--no-preserve-root" in long or (recursive and any(t in _DANGER_RM_TARGETS for t in norm)):
            return Verdict(False, "rm-root", "recursive delete of / or home is never allowed", seg)
    if _SSH.search(seg):
        return Verdict(False, "ssh-keys", "reading ~/.ssh is not allowed", seg)
    if _ENVFILE.search(seg):
        return Verdict(False, "env-file", ".env files hold secrets; reading them is not allowed", seg)
    return None


def check(command: str, config: dict) -> Verdict:
    run_cfg = (config or {}).get("run", {}) or {}
    allow = [re.compile(p) for p in run_cfg.get("allow", []) or []]
    deny = [re.compile(p) for p in run_cfg.get("deny", []) or []]
    # whole-command rules (pipes are segment separators, so check before splitting)
    if _PIPE_TO_SHELL.search(command) or _SUBST_SHELL.search(command):
        if not any(r.search(command) for r in allow):
            return Verdict(False, "pipe-to-shell", "downloading and executing a script (curl|sh) is not allowed", command)
    for r in deny:
        if r.search(command):
            return Verdict(False, "config-deny", f"matches deny pattern {r.pattern!r} in .agentkit/config.toml", command)
    for seg in _SEP.split(command):
        seg = seg.strip()
        if seg.startswith("(") and seg.endswith(")"):
            seg = seg[1:-1].strip()
        if not seg:
            continue
        # Unwrap shell -c forms, including combined flags such as `bash -lc`.
        tok = _strip_prefix(_tokens(seg))
        head = tok[0].rsplit("/", 1)[-1] if tok else ""
        if head in ("sh", "bash", "zsh", "dash"):
            for j in range(1, len(tok) - 1):
                if tok[j].startswith("-") and "c" in tok[j][1:]:
                    inner = check(tok[j + 1], config)
                    if not inner.allowed:
                        return inner
                    break
            else:
                v = builtin_rules(seg)
                if v and not any(r.search(seg) for r in allow):
                    return v
            continue
        for m in re.finditer(r"\$\(([^()]*)\)|`([^`]*)`", seg):
            inner = check(m.group(1) or m.group(2), config)
            if not inner.allowed:
                return inner
        v = builtin_rules(seg)
        if v and not any(r.search(seg) for r in allow):
            return v
    return Verdict(True)
