# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Linux bubblewrap adapter for worker filesystem and process isolation."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .protocol import SandboxCapabilities


class SandboxUnavailable(RuntimeError):
    pass


def available() -> bool:
    if os.name != "posix" or not shutil.which("bwrap"):
        return False
    try:
        r = subprocess.run(["bwrap", "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev", "true"],
                           capture_output=True, timeout=5)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def capabilities() -> dict:
    return SandboxCapabilities(provider="bubblewrap", snapshot_kinds=["git", "files"],
                               network_modes=["shared", "isolated", "allowlist"]).to_dict()


def no_sandbox_capabilities() -> dict:
    return SandboxCapabilities(provider="none").to_dict()


def mode(cfg: dict) -> str:
    value = str(((cfg or {}).get("sandbox") or {}).get("mode", "auto")).lower()
    return value if value in ("auto", "required", "off") else "auto"


def network_enabled(cfg: dict) -> bool:
    value = ((cfg or {}).get("sandbox") or {}).get("network", True)
    if isinstance(value, bool):
        return value
    if isinstance(value, list):
        return False
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise ValueError("sandbox network must be true, false, or an array of hostnames/IP addresses")


def network_domains(cfg: dict) -> Optional[list[str]]:
    value = ((cfg or {}).get("sandbox") or {}).get("network", True)
    if not isinstance(value, list):
        network_enabled(cfg)
        return None
    from .netproxy import normalize_domain

    domains = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("sandbox network allowlist entries must be strings")
        domain = normalize_domain(item)
        if domain not in domains:
            domains.append(domain)
    return domains


def network_label(cfg: dict) -> str:
    domains = network_domains(cfg)
    if domains is not None:
        return "allowlist:" + (",".join(domains[:3]) + (f",+{len(domains) - 3}" if len(domains) > 3 else ""))
    return "shared" if network_enabled(cfg) else "isolated"


def restricted_network(cfg: dict) -> bool:
    return network_domains(cfg) is not None or not network_enabled(cfg)


def command_prefix(worktree: Path, task_dir: Path, log_dir: Path, *, network: bool = True,
                   ro_paths: Optional[list[Path]] = None, writable_paths: Optional[list[Path]] = None,
                   masked_paths: Optional[list[Path]] = None) -> list[str]:
    if not available():
        raise SandboxUnavailable("bubblewrap is unavailable or cannot create a sandbox in this environment")
    worktree = str(worktree.resolve())
    task_dir = str(task_dir.resolve())
    log_dir = str(log_dir.resolve())
    args = [
        "bwrap", "--new-session", "--unshare-all",
        "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
    ]
    for path in ro_paths or []:
        resolved = str(path.resolve())
        args += ["--ro-bind", resolved, resolved]
    requested_masks = []
    for path in sorted(masked_paths or [], key=lambda p: len(p.resolve().parts)):
        resolved = path.resolve()
        if resolved != Path("/") and resolved.exists() and resolved not in requested_masks:
            requested_masks.append(resolved)
    masks = []
    for resolved in requested_masks:
        if not any(mask in resolved.parents for mask in masks):
            masks.append(resolved)
            args += ["--tmpfs", str(resolved)]
    bind_paths = [Path(p).resolve() for p in (ro_paths or []) + (writable_paths or []) +
                  [Path(worktree), Path(task_dir), Path(log_dir)]]
    parents = set()
    for path in bind_paths:
        for mask in masks:
            try:
                path.relative_to(mask)
            except ValueError:
                continue
            for parent in path.parents:
                if parent == mask:
                    break
                if mask in parent.parents:
                    parents.add(parent)
    for parent in sorted(parents, key=lambda p: len(p.parts)):
        args += ["--dir", str(parent)]
    rebound_roots = []
    for path in ro_paths or []:
        resolved_path = path.resolve()
        if any(mask in resolved_path.parents for mask in masks):
            resolved = str(resolved_path)
            args += ["--ro-bind", resolved, resolved]
            rebound_roots.append(resolved_path)
    for mask in requested_masks:
        if any(root in mask.parents for root in rebound_roots):
            args += ["--tmpfs", str(mask)]
    for path in writable_paths or []:
        resolved = str(path.resolve())
        args += ["--bind", resolved, resolved]
    args += ["--bind", worktree, worktree, "--bind", task_dir, task_dir, "--bind", log_dir, log_dir,
             "--chdir", worktree]
    if network:
        args.append("--share-net")
    return args


def wrap(command: list[str], worktree: Path, task_dir: Path, log_dir: Path, *, network: bool = True,
         ro_paths: Optional[list[Path]] = None, writable_paths: Optional[list[Path]] = None,
         masked_paths: Optional[list[Path]] = None) -> list[str]:
    return command_prefix(worktree, task_dir, log_dir, network=network, ro_paths=ro_paths,
                          writable_paths=writable_paths, masked_paths=masked_paths) + list(command)
