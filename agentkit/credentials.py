# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Credential authorization metadata; secret values never enter this store."""
from __future__ import annotations

import json
import os
import re
import secrets
import time
from contextlib import nullcontext
from pathlib import Path

from .core import AKError, Result, atomic_write, file_lock
from .protocol import (CredentialAudit, CredentialBinding, CredentialGrant, CredentialRevocation,
                       canonical_json, sha256)

_ID = re.compile(r"^(cg|cb|cr|ca)_[0-9a-f]{16,}$")


def _base(root: Path) -> Path:
    path = Path(root) / ".agentkit" / "state" / "credentials"
    for name in ("grants", "bindings", "revocations"):
        (path / name).mkdir(parents=True, exist_ok=True)
    return path


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


def _path(root: Path, kind: str, identifier: str) -> Path:
    if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
        raise ValueError("invalid credential metadata id")
    expected = {"grant": "cg", "binding": "cb", "revocation": "cr"}[kind]
    if not identifier.startswith(expected + "_"):
        raise ValueError("credential metadata id has the wrong kind")
    return _base(root) / {"grant": "grants", "binding": "bindings", "revocation": "revocations"}[kind] / f"{identifier}.json"


def _write(path: Path, data: dict) -> None:
    atomic_write(path, (json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"), mode=0o600)


def _load(path: Path, cls):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    return cls.from_dict(data)


def credential_id(*, provider: str, backend: str, auth_kind: str, ref_kind: str, ref: str) -> str:
    return sha256({"provider": provider, "backend": backend, "auth_kind": auth_kind,
                   "ref_kind": ref_kind, "ref": ref})[:24]


def issue_grant(root: Path, *, provider: str, backend: str, auth_kind: str, ref_kind: str, ref: str,
                owner: str = "user", scopes: list[str] | None = None, expires_at: float | None = None,
                policy_sha256: str = "", now: float | None = None) -> dict:
    issued = time.time() if now is None else now
    credential = credential_id(provider=provider, backend=backend, auth_kind=auth_kind,
                               ref_kind=ref_kind, ref=ref)
    grant = CredentialGrant(grant_id=_new_id("cg"), credential_id=credential, provider=provider,
                            backend=backend, auth_kind=auth_kind, ref_kind=ref_kind, ref=ref, owner=owner,
                            scopes=scopes or ["model:call"], issued_at=issued, expires_at=expires_at,
                            policy_sha256=policy_sha256)
    data = grant.to_dict()
    with file_lock(_base(root) / ".lock"):
        _write(_path(root, "grant", data["grant_id"]), data)
        audit(root, "granted", "allow", owner, credential_id=credential, grant_id=data["grant_id"],
              now=issued, lock=False)
    return data


def create_binding(root: Path, *, grant_id: str, task_id: str, run_id: str, backend: str,
                   session_id: str = "", expires_at: float | None = None, now: float | None = None) -> dict:
    grant = load_grant(root, grant_id)
    if grant["backend"] != backend:
        raise ValueError("binding backend does not match grant")
    binding = CredentialBinding(binding_id=_new_id("cb"), grant_id=grant_id, task_id=task_id, run_id=run_id,
                                backend=backend, session_id=session_id,
                                created_at=time.time() if now is None else now, expires_at=expires_at)
    data = binding.to_dict()
    with file_lock(_base(root) / ".lock"):
        _write(_path(root, "binding", data["binding_id"]), data)
        audit(root, "bound", "allow", grant["owner"], credential_id=grant["credential_id"],
              grant_id=grant_id, binding_id=data["binding_id"], task_id=task_id, run_id=run_id,
              session_id=session_id, now=data["created_at"], lock=False)
    return data


def update_binding(root: Path, binding_id: str, *, session_id: str | None = None,
                   status: str | None = None, expires_at: float | None = None) -> dict:
    data = load_binding(root, binding_id)
    if session_id is not None:
        data["session_id"] = session_id
    if status is not None:
        data["status"] = status
    if expires_at is not None:
        data["expires_at"] = expires_at
    data = CredentialBinding.from_dict(data).to_dict()
    with file_lock(_base(root) / ".lock"):
        _write(_path(root, "binding", binding_id), data)
    return data


def close_binding(root: Path, binding_id: str, *, reason_code: str = "run_finished",
                  now: float | None = None) -> dict:
    data = load_binding(root, binding_id)
    if data["status"] == "active":
        data = update_binding(root, binding_id, status="closed")
        grant = load_grant(root, data["grant_id"])
        audit(root, "closed", "allow", grant["owner"], credential_id=grant["credential_id"],
              grant_id=data["grant_id"], binding_id=binding_id, task_id=data["task_id"], run_id=data["run_id"],
              session_id=data["session_id"], reason_code=reason_code, now=now)
    return data


def load_grant(root: Path, grant_id: str) -> dict:
    return _load(_path(root, "grant", grant_id), CredentialGrant).to_dict()


def load_binding(root: Path, binding_id: str) -> dict:
    return _load(_path(root, "binding", binding_id), CredentialBinding).to_dict()


def _revocations(root: Path, target_kind: str, target_id: str) -> list[dict]:
    out = []
    for path in (_base(root) / "revocations").glob("*.json"):
        try:
            data = _load(path, CredentialRevocation).to_dict()
        except (OSError, ValueError):
            continue
        if data["target_kind"] == target_kind and data["target_id"] == target_id:
            out.append(data)
    return out


def revoke(root: Path, *, target_kind: str, target_id: str, reason_code: str, reason: str,
           actor: str = "user", now: float | None = None) -> dict:
    if target_kind not in ("grant", "binding"):
        raise ValueError("revocation target must be grant or binding")
    if target_kind == "grant":
        load_grant(root, target_id)
        owner = "user"
    else:
        binding = load_binding(root, target_id)
        owner = load_grant(root, binding["grant_id"])["owner"]
    revoked = time.time() if now is None else now
    data = CredentialRevocation(revocation_id=_new_id("cr"), target_kind=target_kind, target_id=target_id,
                                reason_code=reason_code, reason=reason, actor=actor, revoked_at=revoked).to_dict()
    with file_lock(_base(root) / ".lock"):
        _write(_path(root, "revocation", data["revocation_id"]), data)
        if target_kind == "binding":
            path = _path(root, "binding", target_id)
            binding = load_binding(root, target_id)
            binding["status"] = "revoked"
            _write(path, binding)
        audit(root, "revoked", "allow", actor, grant_id=target_id if target_kind == "grant" else binding["grant_id"],
              binding_id=target_id if target_kind == "binding" else "", reason_code=reason_code,
              now=revoked, lock=False)
    return data


def is_grant_active(root: Path, grant_id: str, now: float | None = None) -> bool:
    grant = load_grant(root, grant_id)
    current = time.time() if now is None else now
    return not (grant.get("expires_at") is not None and current >= grant["expires_at"]) and not _revocations(root, "grant", grant_id)


def is_binding_active(root: Path, binding_id: str, now: float | None = None) -> bool:
    binding = load_binding(root, binding_id)
    current = time.time() if now is None else now
    return (binding["status"] == "active" and
            not (binding.get("expires_at") is not None and current >= binding["expires_at"]) and
            not _revocations(root, "binding", binding_id) and is_grant_active(root, binding["grant_id"], current))


def audit(root: Path, action: str, decision: str, actor: str, *, credential_id: str = "", grant_id: str = "",
          binding_id: str = "", task_id: str = "", run_id: str = "", session_id: str = "",
          reason_code: str = "", now: float | None = None, lock: bool = True) -> dict:
    data = CredentialAudit(audit_id=_new_id("ca"), ts=time.time() if now is None else now, action=action,
                           decision=decision, actor=actor, credential_id=credential_id, grant_id=grant_id,
                           binding_id=binding_id, task_id=task_id, run_id=run_id, session_id=session_id,
                           reason_code=reason_code).to_dict()
    path = _base(root) / "audit.jsonl"
    with file_lock(_base(root) / ".lock") if lock else nullcontext():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(canonical_json(data) + "\n")
    return data


def cmd_credential(ctx, a) -> Result:
    from . import tasks

    target = a.target
    try:
        if a.action == "show":
            if target.startswith("cg_"):
                grant = load_grant(ctx.root, target)
                return Result({"grant": grant, "active": is_grant_active(ctx.root, target)})
            if target.startswith("cb_"):
                binding = load_binding(ctx.root, target)
                return Result({"binding": binding, "active": is_binding_active(ctx.root, target)})
            task = tasks.load(ctx.root, target)
            grant_id = str(task.get("credential_grant_id") or "")
            binding_id = str(task.get("credential_binding_id") or "")
            if not grant_id and not binding_id:
                return Result({"task_id": task["id"], "credential": None})
            grant = load_grant(ctx.root, grant_id) if grant_id else None
            binding = load_binding(ctx.root, binding_id) if binding_id else None
            return Result({"task_id": task["id"], "grant": grant, "binding": binding,
                           "grant_active": is_grant_active(ctx.root, grant_id) if grant_id else None,
                           "binding_active": is_binding_active(ctx.root, binding_id) if binding_id else None})
        kind = "grant" if target.startswith("cg_") else "binding"
        return Result(revoke(ctx.root, target_kind=kind, target_id=target, reason_code="user_revoked",
                             reason="", actor="user"))
    except (OSError, ValueError) as e:
        raise AKError("credential operation failed", str(e), "agentkit credential show TASK_ID",
                      code="AK-CONFIG") from e
