# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Local release artifacts, checksum manifests and explicit rollback."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from datetime import datetime, timezone
from email.parser import Parser
from pathlib import Path, PurePosixPath, PureWindowsPath

from . import __version__
from .core import AKError, Ctx, Result, atomic_write

SCHEMA_VERSION = "1.0"
MANIFEST = "release-manifest.json"
SUMS = "SHA256SUMS"
_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
_REQUIRED_MODULES = (
    "__init__.py", "__main__.py", "backends.py", "checkpoint.py", "cli.py", "codeintel.py", "compress.py",
    "config.py", "context.py", "core.py", "diag.py", "doctor.py", "edit.py", "events.py", "fsutil.py",
    "goals.py", "lifecycle.py", "lsp.py", "manual.py", "memory.py", "mcp_server.py", "models.py",
    "netproxy.py", "permissions.py", "protocol.py", "read.py", "redact.py", "release.py", "repomap.py",
    "run.py", "sandbox.py", "search.py", "state.py", "symbols.py", "tasks.py", "verify.py", "worker.py",
)
_REQUIRED_FILTERS = (
    "cargo-test.json", "eslint.json", "generic.json", "git-diff.json", "git-log.json", "git-status.json",
    "go-test.json", "jest.json", "mypy.json", "npm-install.json", "pip.json", "pytest.json", "ruff.json",
    "tsc.json", "unittest.json",
)


def _toml():
    if sys.version_info >= (3, 11):
        import tomllib
        return tomllib
    import tomli
    return tomli


def _project(root: Path) -> dict:
    path = root / "pyproject.toml"
    try:
        data = _toml().loads(path.read_text(encoding="utf-8"))["project"]
    except (OSError, KeyError, ValueError) as e:
        raise AKError("release project metadata is missing or invalid", str(e), "fix pyproject.toml", code="AK-RELEASE") from e
    name, version = str(data.get("name") or ""), str(data.get("version") or "")
    if name != "agentkit" or not _SEMVER.fullmatch(version) or version != __version__:
        raise AKError("release version is inconsistent", f"pyproject={version}, module={__version__}",
                      "keep pyproject.toml and agentkit/__init__.py in sync", code="AK-RELEASE")
    return {"name": name, "version": version, "requires_python": str(data.get("requires-python") or "")}


def _git_state(root: Path) -> dict:
    def run(*args):
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=30)
    head = run("rev-parse", "--verify", "HEAD")
    if head.returncode != 0:
        return {"commit": None, "dirty": None, "epoch": None}
    status = run("status", "--porcelain")
    epoch = run("show", "-s", "--format=%ct", "HEAD")
    return {"commit": head.stdout.strip(), "dirty": bool(status.stdout.strip()),
            "epoch": int(epoch.stdout.strip()) if epoch.returncode == 0 and epoch.stdout.strip().isdigit() else None}


def _out_dir(ctx: Ctx, raw: str) -> Path:
    return ctx.resolve(raw)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _artifact(path: Path) -> dict:
    return {"path": path.name, "sha256": _sha256(path), "bytes": path.stat().st_size}


def _check_pip_env() -> None:
    blocked = [key for key in ("PIP_TARGET", "PIP_PREFIX", "PIP_ROOT", "PIP_USER", "PIP_BREAK_SYSTEM_PACKAGES",
                               "PIP_CONFIG_FILE") if os.environ.get(key)]
    if blocked:
        raise AKError("release command refused a redirected pip environment", ", ".join(blocked),
                      "unset these variables; release never bypasses Python environment protections", code="AK-RELEASE")


def _run_build(root: Path, staging: Path, env: dict) -> None:
    env = {key: value for key, value in env.items() if not key.startswith("PIP_")}
    env["PIP_CONFIG_FILE"] = os.devnull
    command = [sys.executable, "-I", "-m", "build", "--sdist", "--wheel", "--outdir", str(staging), str(root)]
    result = subprocess.run(command, cwd=str(root), env=env, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=600)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-1000:]
        raise AKError("release build failed", detail, "pip install -e '.[dev]' then retry", code="AK-RELEASE")


def _write_release_files(staging: Path, manifest: dict) -> None:
    atomic_write(staging / MANIFEST, (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    lines = [f"{item['sha256']}  {item['path']}" for item in manifest["artifacts"]]
    atomic_write(staging / SUMS, ("\n".join(lines) + "\n").encode("ascii"))


def plan(ctx: Ctx, a) -> Result:
    project = _project(ctx.root)
    out = _out_dir(ctx, a.out)
    git = _git_state(ctx.root)
    return Result({"schema_version": SCHEMA_VERSION, "name": project["name"], "version": project["version"],
                   "out": ctx.rel(out), "artifacts": ["wheel", "sdist", MANIFEST, SUMS],
                   "git": {"commit": git["commit"], "dirty": git["dirty"]},
                   "dry_run": True, "allow_dirty": bool(a.allow_dirty), "overwrite": bool(a.overwrite),
                   "next": ctx.next_cmd("release", "build", "--out", a.out,
                                        *(["--allow-dirty"] if a.allow_dirty else []),
                                        *(["--overwrite"] if a.overwrite else []))})


def _publish(staging: Path, out: Path, names: list[str], overwrite: bool) -> None:
    existing = [name for name in names if (out / name).exists()]
    if existing and not overwrite:
        raise AKError("release artifacts already exist", ", ".join(existing),
                      "use --overwrite only after reviewing the existing release", code="AK-RELEASE")
    backup = Path(tempfile.mkdtemp(prefix=".release-backup-", dir=str(out))) if existing else None
    backed, published = [], []
    success = False
    try:
        if backup is not None:
            for name in existing:
                os.replace(out / name, backup / name)
                backed.append(name)
        for name in names:
            os.replace(staging / name, out / name)
            published.append(name)
        success = True
    except Exception:
        for name in published:
            (out / name).unlink(missing_ok=True)
        if backup is not None:
            for name in backed:
                os.replace(backup / name, out / name)
        raise
    finally:
        if backup is not None and success:
            shutil.rmtree(backup, ignore_errors=True)


def build(ctx: Ctx, a) -> Result:
    _check_pip_env()
    project = _project(ctx.root)
    git = _git_state(ctx.root)
    if not git["commit"]:
        raise AKError("release build requires a git commit", "the manifest must identify an immutable source revision",
                      "git init && git add -A && git commit -m init", code="AK-RELEASE")
    if git["dirty"] and not a.allow_dirty:
        raise AKError("release build requires a clean worktree", "commit or stash changes, or use --allow-dirty for a test build",
                      "git status --short", code="AK-RELEASE")
    out = _out_dir(ctx, a.out)
    out.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".release-", dir=str(out)))
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if git["epoch"]:
        env["SOURCE_DATE_EPOCH"] = str(git["epoch"])
    try:
        _run_build(ctx.root, staging, env)
        wheels = sorted(staging.glob("*.whl"))
        sdists = sorted(staging.glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            raise AKError("release build did not produce exactly one wheel and one sdist",
                          f"wheels={len(wheels)}, sdists={len(sdists)}", "check the build backend", code="AK-RELEASE")
        artifacts = [_artifact(wheels[0]), _artifact(sdists[0])]
        manifest = {"schema_version": SCHEMA_VERSION, "name": project["name"], "version": project["version"],
                    "requires_python": project["requires_python"],
                    "created_utc": datetime.now(timezone.utc).isoformat(), "git_commit": git["commit"],
                    "git_dirty": bool(git["dirty"]), "python": sys.version.split()[0], "artifacts": artifacts}
        _write_release_files(staging, manifest)
        names = [wheels[0].name, sdists[0].name, MANIFEST, SUMS]
        _publish(staging, out, names, bool(a.overwrite))
        return Result({"schema_version": SCHEMA_VERSION, "version": project["version"], "out": ctx.rel(out),
                       "manifest": ctx.rel(out / MANIFEST), "checksums": ctx.rel(out / SUMS),
                       "artifacts": artifacts, "git_commit": git["commit"], "git_dirty": bool(git["dirty"]),
                       "verify": ctx.next_cmd("release", "verify", str(out / MANIFEST))})
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _load_manifest(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise AKError("release manifest is unreadable", str(e), "run agentkit release build", code="AK-RELEASE-HASH") from e
    required = {"schema_version", "name", "version", "created_utc", "git_commit", "artifacts"}
    if not isinstance(data, dict) or set(data) != required | {"requires_python", "git_dirty", "python"}:
        raise AKError("release manifest schema is invalid", "unknown or missing top-level fields",
                      "rebuild the release manifest", code="AK-RELEASE-HASH")
    if data.get("schema_version") != SCHEMA_VERSION or data.get("name") != "agentkit" or not _SEMVER.fullmatch(str(data.get("version") or "")):
        raise AKError("release manifest identity is invalid", "schema, name or version is unsupported",
                      "rebuild the release manifest", code="AK-RELEASE-HASH")
    if not isinstance(data.get("git_commit"), str) or not re.fullmatch(r"[0-9a-f]{40,64}", data["git_commit"]):
        raise AKError("release manifest has no valid git commit", "the source revision is required",
                      "rebuild the release manifest", code="AK-RELEASE-HASH")
    if not isinstance(data.get("artifacts"), list) or len(data["artifacts"]) != 2:
        raise AKError("release manifest must contain one wheel and one sdist", "invalid artifact list",
                      "rebuild the release manifest", code="AK-RELEASE-HASH")
    return data


def _safe_archive_path(name: str) -> bool:
    if not isinstance(name, str) or not name:
        return False
    posix = PurePosixPath(name)
    windows = PureWindowsPath(name)
    return not (posix.is_absolute() or windows.is_absolute() or windows.drive or windows.root
                or ".." in posix.parts or ".." in windows.parts)


def _safe_artifact(base: Path, name: str) -> Path:
    if (not _safe_archive_path(name) or PurePosixPath(name).name != name or PureWindowsPath(name).name != name
            or name in (MANIFEST, SUMS)):
        raise AKError("release artifact path is invalid", str(name), "rebuild the release manifest", code="AK-RELEASE-HASH")
    path = base / name
    if not path.is_file():
        raise AKError("release artifact is missing", name, "restore the artifact next to the manifest", code="AK-RELEASE-HASH")
    return path


def _check_wheel(path: Path, version: str) -> None:
    try:
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            names = {info.filename for info in infos}
            if len(names) != len(infos) or any(not _safe_archive_path(info.filename) for info in infos):
                raise ValueError("wheel contains duplicate or unsafe paths")
            if any(stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1 for info in infos):
                raise ValueError("wheel contains a symlink or encrypted entry")
            metadata = [name for name in names if name.endswith(".dist-info/METADATA")]
            if len(metadata) != 1:
                raise ValueError("wheel must contain exactly one METADATA")
            info = Parser().parsestr(z.read(metadata[0]).decode("utf-8"))
            if info.get("Name") != "agentkit" or info.get("Version") != version:
                raise ValueError("wheel metadata name/version mismatch")
            required = {f"agentkit/{name}" for name in _REQUIRED_MODULES}
            required |= {f"agentkit/filters/{name}" for name in _REQUIRED_FILTERS}
            required |= {f"{metadata[0].rsplit('/', 1)[0]}/licenses/LICENSE",
                         f"{metadata[0].rsplit('/', 1)[0]}/licenses/NOTICE"}
            if not required <= names:
                raise ValueError("wheel is missing required AgentKit modules, filters or licenses")
    except (OSError, ValueError, zipfile.BadZipFile, UnicodeError) as e:
        raise AKError("release wheel validation failed", str(e), "rebuild the wheel", code="AK-RELEASE-HASH") from e


def _check_sdist(path: Path, version: str) -> None:
    try:
        with tarfile.open(path, "r:gz") as tar:
            members = tar.getmembers()
            names = {member.name for member in members}
            if any(not _safe_archive_path(member.name) for member in members):
                raise ValueError("sdist contains an unsafe path")
            if any(not (member.isfile() or member.isdir()) for member in members):
                raise ValueError("sdist contains a link or special file")
            prefix = f"agentkit-{version}"
            required = {f"{prefix}/pyproject.toml", f"{prefix}/README.md", f"{prefix}/LICENSE", f"{prefix}/NOTICE"}
            required |= {f"{prefix}/agentkit/{name}" for name in _REQUIRED_MODULES}
            required |= {f"{prefix}/agentkit/filters/{name}" for name in _REQUIRED_FILTERS}
            if not required <= names:
                raise ValueError("sdist is missing project metadata, source, filters or licenses")
    except (OSError, ValueError, tarfile.TarError) as e:
        raise AKError("release sdist validation failed", str(e), "rebuild the sdist", code="AK-RELEASE-HASH") from e


def verify(ctx: Ctx, a) -> Result:
    manifest_path = ctx.resolve(a.manifest)
    manifest = _load_manifest(manifest_path)
    base = manifest_path.parent
    checked = []
    wheel = sdist = None
    for item in manifest["artifacts"]:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "bytes"}:
            raise AKError("release artifact entry is invalid", "expected path, sha256 and bytes",
                          "rebuild the release manifest", code="AK-RELEASE-HASH")
        path = _safe_artifact(base, item["path"])
        if not re.fullmatch(r"[0-9a-f]{64}", str(item["sha256"])) or not isinstance(item["bytes"], int) or item["bytes"] < 0:
            raise AKError("release artifact hash or size is invalid", item["path"], "rebuild the release manifest", code="AK-RELEASE-HASH")
        if path.stat().st_size != item["bytes"] or _sha256(path) != item["sha256"]:
            raise AKError("release artifact checksum mismatch", item["path"], "restore the original artifact", code="AK-RELEASE-HASH")
        if item["path"].endswith(".whl"):
            wheel = path
        elif item["path"].endswith(".tar.gz"):
            sdist = path
        else:
            raise AKError("release artifact type is unsupported", item["path"], "rebuild the release", code="AK-RELEASE-HASH")
        checked.append(item)
    if wheel is None or sdist is None:
        raise AKError("release manifest must contain one wheel and one sdist", "invalid artifact types",
                      "rebuild the release manifest", code="AK-RELEASE-HASH")
    _check_wheel(wheel, manifest["version"])
    _check_sdist(sdist, manifest["version"])
    sums = base / SUMS
    if sums.exists():
        expected = [f"{item['sha256']}  {item['path']}" for item in checked]
        if sums.read_text(encoding="ascii").splitlines() != expected:
            raise AKError("release checksum file does not match the manifest", SUMS,
                          "restore SHA256SUMS or rebuild the release", code="AK-RELEASE-HASH")
    return Result({"verified": True, "trusted": False, "manifest": ctx.rel(manifest_path), "version": manifest["version"],
                   "git_commit": manifest["git_commit"], "artifacts": checked})


def _installed_version() -> str:
    try:
        from importlib.metadata import version
        return version("agentkit")
    except Exception:
        return __version__


def _running_console_script() -> bool:
    name = Path(sys.argv[0]).name.lower()
    return os.name == "nt" and (name in ("agentkit.exe", "agentkit-script.py") or name.startswith("agentkit"))


def _stage_wheel(wheel: Path, item: dict, version: str) -> tuple[Path, Path]:
    directory = Path(tempfile.mkdtemp(prefix="agentkit-rollback-"))
    staged = directory / wheel.name
    try:
        shutil.copyfile(wheel, staged)
        if staged.stat().st_size != item["bytes"] or _sha256(staged) != item["sha256"]:
            raise AKError("rollback wheel changed while staging", wheel.name, "retry from a stable artifact directory",
                          code="AK-RELEASE-HASH")
        _check_wheel(staged, version)
        return staged, directory
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def _schedule_windows_rollback(command: list[str], status_file: Path, log_file: Path, cleanup_dir: Path) -> int:
    script = (
        "import ctypes,json,os,shutil,subprocess,sys,time;from ctypes import wintypes;"
        f"pid={os.getpid()};command={command!r};status={str(status_file)!r};log={str(log_file)!r};cleanup={str(cleanup_dir)!r};"
        "k=ctypes.WinDLL('kernel32',use_last_error=True);"
        "k.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];k.OpenProcess.restype=wintypes.HANDLE;"
        "k.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD];k.WaitForSingleObject.restype=wintypes.DWORD;"
        "k.CloseHandle.argtypes=[wintypes.HANDLE];"
        "h=k.OpenProcess(0x00100000,False,pid);"
        "\nif h:\n"
        " k.WaitForSingleObject(h,60000);k.CloseHandle(h)\n"
        "try:\n"
        " with open(log,'wb') as f: r=subprocess.run(command,stdout=f,stderr=subprocess.STDOUT)\n"
        " result={'installed':r.returncode==0,'exit_code':r.returncode}\n"
        "except Exception as e: result={'installed':False,'error':str(e)}\n"
        "shutil.rmtree(cleanup,ignore_errors=True)\n"
        "with open(status,'w',encoding='utf-8') as f: json.dump(result,f)\n"
    )
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP") if key in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    with open(log_file, "ab") as log:
        proc = subprocess.Popen([sys.executable, "-I", "-c", script], stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, env=env, close_fds=True, creationflags=flags)
    return proc.pid


def rollback(ctx: Ctx, a) -> Result:
    _check_pip_env()
    manifest_path = ctx.resolve(a.manifest)
    manifest = _load_manifest(manifest_path)
    for item in manifest["artifacts"]:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "bytes"}:
            raise AKError("release artifact entry is invalid", "expected path, sha256 and bytes",
                          "rebuild the release manifest", code="AK-RELEASE-HASH")
    wheel_item = next((item for item in manifest["artifacts"] if str(item["path"]).endswith(".whl")), None)
    if wheel_item is None:
        raise AKError("release manifest has no wheel to roll back to", "invalid artifact list",
                      "rebuild the release manifest", code="AK-RELEASE-HASH")
    wheel = _safe_artifact(manifest_path.parent, wheel_item["path"])
    if wheel.stat().st_size != wheel_item["bytes"] or _sha256(wheel) != wheel_item["sha256"]:
        raise AKError("rollback wheel checksum mismatch", wheel.name, "restore the original wheel", code="AK-RELEASE-HASH")
    _check_wheel(wheel, manifest["version"])
    command = [sys.executable, "-I", "-m", "pip", "--isolated", "install", "--no-deps", "--force-reinstall", str(wheel)]
    summary = {"current_version": _installed_version(), "target_version": manifest["version"], "wheel": str(wheel),
               "command": shlex.join(command), "preserves_state": True, "dry_run": not a.yes}
    if not a.yes:
        summary["hint"] = "nothing installed yet; show this plan to your human, then run next"
        return Result(summary, False, ctx.next_cmd("release", "rollback", a.manifest, "--yes"))
    staged, staging_dir = _stage_wheel(wheel, wheel_item, manifest["version"])
    command[-1] = str(staged)
    summary["staged_wheel"] = str(staged)
    summary["command"] = shlex.join(command)
    if _running_console_script():
        status_file = manifest_path.parent / f"rollback-{manifest['version']}-result.json"
        log_file = manifest_path.parent / f"rollback-{manifest['version']}.log"
        status_file.unlink(missing_ok=True)
        try:
            helper_pid = _schedule_windows_rollback(command, status_file, log_file, staging_dir)
        except Exception:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise
        summary.update({"dry_run": False, "installed": False, "scheduled": True, "helper_pid": helper_pid,
                        "status_file": str(status_file), "log_file": str(log_file), "restart_required": True,
                        "hint": "Windows cannot replace the running agentkit.exe; the helper installs after this process exits"})
        return Result(summary)
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
    if result.returncode != 0:
        raise AKError("release rollback failed", (result.stderr or result.stdout).strip()[-1000:],
                      "fix the Python environment and retry", code="AK-RELEASE")
    summary.update({"dry_run": False, "installed": True, "output_tail": (result.stdout or result.stderr).strip()[-500:]})
    return Result(summary)


def cmd_release(ctx: Ctx, a) -> Result:
    command = getattr(a, "release_cmd", None) or "plan"
    if command in ("verify", "rollback") and not getattr(a, "manifest", ""):
        raise AKError(f"release {command} needs a manifest", "pass release-manifest.json",
                      f"agentkit release {command} dist/release-manifest.json", code="AK-USAGE")
    if command == "plan":
        return plan(ctx, a)
    if command == "build":
        return build(ctx, a)
    if command == "verify":
        return verify(ctx, a)
    if command == "rollback":
        return rollback(ctx, a)
    raise AKError(f"unknown release subcommand '{command}'", "use plan|build|verify|rollback", "agentkit release --help", code="AK-USAGE")
