# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Local release build, manifest verification and rollback safety."""
import io
import json
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentkit import release
from agentkit.core import AKError, Ctx

VERSION = "0.2.0"


def _project(root: Path, version: str = VERSION):
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "agentkit"\nversion = "{version}"\nrequires-python = ">=3.10"\n', encoding="utf-8")


def _ctx(root: Path) -> Ctx:
    return Ctx(root=root, cwd=root)


def _args(**kw):
    base = {"out": "dist", "allow_dirty": False, "overwrite": False, "manifest": "dist/release-manifest.json",
            "artifact": "", "key": "", "signature": "", "public_key": "", "yes": False}
    base.update(kw)
    return SimpleNamespace(**base)


def _fake_build(root: Path, staging: Path, _env: dict):
    wheel = staging / f"agentkit-{VERSION}-py3-none-any.whl"
    metadata = (f"Metadata-Version: 2.1\nName: agentkit\nVersion: {VERSION}\n\n").encode()
    with zipfile.ZipFile(wheel, "w") as z:
        z.writestr(f"agentkit-{VERSION}.dist-info/METADATA", metadata)
        for name in release._REQUIRED_MODULES:
            z.writestr(f"agentkit/{name}", b"# test\n")
        for name in release._REQUIRED_FILTERS:
            z.writestr(f"agentkit/filters/{name}", b"{}\n")
        for name in release._REQUIRED_SKILL_FILES:
            z.writestr(f"agentkit/skills/cost-efficient-ai-development/{name}", b"# skill\n")
        dist_info = f"agentkit-{VERSION}.dist-info"
        z.writestr(f"{dist_info}/licenses/LICENSE", b"license\n")
        z.writestr(f"{dist_info}/licenses/NOTICE", b"notice\n")
    sdist = staging / f"agentkit-{VERSION}.tar.gz"
    with tarfile.open(sdist, "w:gz") as tar:
        files = {"pyproject.toml": b"[project]\n", "README.md": b"readme\n", "LICENSE": b"license\n", "NOTICE": b"notice\n"}
        files.update({f"agentkit/{name}": b"# test\n" for name in release._REQUIRED_MODULES})
        files.update({f"agentkit/filters/{name}": b"{}\n" for name in release._REQUIRED_FILTERS})
        files.update({f"agentkit/skills/cost-efficient-ai-development/{name}": b"# skill\n"
                      for name in release._REQUIRED_SKILL_FILES})
        for name, data in files.items():
            info = tarfile.TarInfo(f"agentkit-{VERSION}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def test_release_plan_is_read_only(tmp_path, monkeypatch):
    _project(tmp_path)
    monkeypatch.setattr(release, "_git_state", lambda _root: {"commit": "a" * 40, "dirty": False, "epoch": None})
    result = release.plan(_ctx(tmp_path), _args())
    assert result.data["dry_run"] is True and result.data["version"] == VERSION
    assert not (tmp_path / "dist").exists()


def test_release_build_and_verify_roundtrip(tmp_path, monkeypatch):
    _project(tmp_path)
    monkeypatch.setattr(release, "_git_state", lambda _root: {"commit": "a" * 40, "dirty": False, "epoch": 1})
    monkeypatch.setattr(release, "_run_build", _fake_build)
    built = release.build(_ctx(tmp_path), _args())
    assert built.data["git_commit"] == "a" * 40
    manifest = tmp_path / "dist" / release.MANIFEST
    assert manifest.exists() and (tmp_path / "dist" / release.SUMS).exists()
    verified = release.verify(_ctx(tmp_path), _args())
    assert verified.data["verified"] is True and verified.data["trusted"] is False and len(verified.data["artifacts"]) == 2


def test_release_sign_and_verify_with_openssl(tmp_path, monkeypatch):
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("OpenSSL is unavailable")
    _project(tmp_path)
    monkeypatch.setattr(release, "_git_state", lambda _root: {"commit": "a" * 40, "dirty": False, "epoch": 1})
    monkeypatch.setattr(release, "_run_build", _fake_build)
    release.build(_ctx(tmp_path), _args())
    private, public = tmp_path / "private.pem", tmp_path / "public.pem"
    subprocess.run([openssl, "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(private)],
                   check=True, capture_output=True, timeout=30)
    subprocess.run([openssl, "pkey", "-in", str(private), "-pubout", "-out", str(public)],
                   check=True, capture_output=True, timeout=30)
    signed = release.sign(_ctx(tmp_path), _args(artifact="dist/SHA256SUMS", key=str(private)))
    assert signed.data["signed"] is True and signed.data["algorithm"] == "SHA256"
    signature = tmp_path / "dist" / (release.SUMS + ".sig")
    verified = release.verify(_ctx(tmp_path), _args(signature=str(signature), public_key=str(public)))
    assert verified.data["signature_verified"] is True and verified.data["trusted"] is False
    sums = tmp_path / "dist" / release.SUMS
    sums.write_text(sums.read_text(encoding="ascii") + "tampered\n", encoding="ascii")
    with pytest.raises(AKError) as error:
        release.verify(_ctx(tmp_path), _args(signature=str(signature), public_key=str(public)))
    assert error.value.code == "AK-RELEASE-HASH"


def test_release_signature_requires_both_files(tmp_path):
    with pytest.raises(AKError) as error:
        release.verify(_ctx(tmp_path), _args(signature="sig"))
    assert error.value.code == "AK-USAGE"


def test_release_required_files_match_source():
    package = Path(release.__file__).parent
    assert set(release._REQUIRED_MODULES) == {path.name for path in package.glob("*.py")}
    assert set(release._REQUIRED_FILTERS) == {path.name for path in (package / "filters").glob("*.json")}
    skill = package / "skills" / "cost-efficient-ai-development"
    assert set(release._REQUIRED_SKILL_FILES) == {
        path.relative_to(skill).as_posix() for path in skill.rglob("*") if path.is_file()}


@pytest.mark.parametrize("name", ["../escape.whl", "..\\escape.whl", "C:/escape.whl", "\\\\server\\share\\escape.whl"])
def test_release_rejects_cross_platform_artifact_paths(tmp_path, name):
    with pytest.raises(AKError) as error:
        release._safe_artifact(tmp_path, name)
    assert error.value.code == "AK-RELEASE-HASH"


@pytest.mark.parametrize("name", ["../escape", "agentkit-0.2.0/..\\escape", "C:/escape", "/escape"])
def test_release_rejects_cross_platform_sdist_paths(tmp_path, name):
    path = tmp_path / "unsafe.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo(name)
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(AKError) as error:
        release._check_sdist(path, VERSION)
    assert "unsafe path" in error.value.why


def test_release_rejects_sdist_links(tmp_path):
    path = tmp_path / "link.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo("agentkit-0.2.0/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../escape"
        tar.addfile(info)
    with pytest.raises(AKError) as error:
        release._check_sdist(path, VERSION)
    assert "link or special file" in error.value.why


def test_release_wheel_requires_lsp_module(tmp_path, monkeypatch):
    _project(tmp_path)
    monkeypatch.setattr(release, "_git_state", lambda _root: {"commit": "a" * 40, "dirty": False, "epoch": 1})
    monkeypatch.setattr(release, "_run_build", _fake_build)
    release.build(_ctx(tmp_path), _args())
    wheel = next((tmp_path / "dist").glob("*.whl"))
    missing = tmp_path / "missing.whl"
    with zipfile.ZipFile(wheel) as source, zipfile.ZipFile(missing, "w") as target:
        for info in source.infolist():
            if info.filename != "agentkit/lsp.py":
                target.writestr(info, source.read(info))
    with pytest.raises(AKError) as error:
        release._check_wheel(missing, VERSION)
    assert "missing required" in error.value.why


def test_release_verify_detects_tampering(tmp_path, monkeypatch):
    _project(tmp_path)
    monkeypatch.setattr(release, "_git_state", lambda _root: {"commit": "a" * 40, "dirty": False, "epoch": 1})
    monkeypatch.setattr(release, "_run_build", _fake_build)
    release.build(_ctx(tmp_path), _args())
    wheel = next((tmp_path / "dist").glob("*.whl"))
    wheel.write_bytes(wheel.read_bytes() + b"tampered")
    with pytest.raises(AKError) as error:
        release.verify(_ctx(tmp_path), _args())
    assert error.value.code == "AK-RELEASE-HASH"


def test_release_verify_rejects_manifest_path_traversal(tmp_path, monkeypatch):
    _project(tmp_path)
    monkeypatch.setattr(release, "_git_state", lambda _root: {"commit": "a" * 40, "dirty": False, "epoch": 1})
    monkeypatch.setattr(release, "_run_build", _fake_build)
    release.build(_ctx(tmp_path), _args())
    path = tmp_path / "dist" / release.MANIFEST
    data = json.loads(path.read_text(encoding="utf-8"))
    data["artifacts"][0]["path"] = "../escape.whl"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(AKError) as error:
        release.verify(_ctx(tmp_path), _args())
    assert error.value.code == "AK-RELEASE-HASH"


def test_release_build_refuses_dirty_without_override(tmp_path, monkeypatch):
    _project(tmp_path)
    monkeypatch.setattr(release, "_git_state", lambda _root: {"commit": "a" * 40, "dirty": True, "epoch": 1})
    with pytest.raises(AKError) as error:
        release.build(_ctx(tmp_path), _args())
    assert error.value.code == "AK-RELEASE" and "clean worktree" in error.value.what


def test_release_rejects_version_mismatch(tmp_path):
    _project(tmp_path, "9.9.9")
    with pytest.raises(AKError) as error:
        release.plan(_ctx(tmp_path), _args())
    assert error.value.code == "AK-RELEASE"


def test_release_cli_plan_and_missing_manifest(ak, repo):
    _project(repo)
    result = ak("release", "plan")
    assert result["ok"] and result["data"]["dry_run"] is True
    missing = ak("release", "verify")
    assert not missing["ok"] and missing["error"]["code"] == "AK-USAGE"


def test_release_refuses_redirected_pip_environment(tmp_path, monkeypatch):
    _project(tmp_path)
    monkeypatch.setattr(release, "_git_state", lambda _root: {"commit": "a" * 40, "dirty": False, "epoch": 1})
    monkeypatch.setenv("PIP_TARGET", str(tmp_path / "target"))
    with pytest.raises(AKError) as error:
        release.build(_ctx(tmp_path), _args())
    assert error.value.code == "AK-RELEASE" and "PIP_TARGET" in error.value.why


def test_release_overwrite_restores_previous_artifacts_on_failure(tmp_path, monkeypatch):
    _project(tmp_path)
    monkeypatch.setattr(release, "_git_state", lambda _root: {"commit": "a" * 40, "dirty": False, "epoch": 1})
    monkeypatch.setattr(release, "_run_build", _fake_build)
    release.build(_ctx(tmp_path), _args())
    with pytest.raises(AKError):
        release.build(_ctx(tmp_path), _args())
    original = release.verify(_ctx(tmp_path), _args()).data
    replace = release.os.replace

    def fail_manifest(source, target):
        if Path(source).name == release.MANIFEST:
            raise OSError("simulated publish failure")
        return replace(source, target)

    monkeypatch.setattr(release.os, "replace", fail_manifest)
    with pytest.raises(OSError):
        release.build(_ctx(tmp_path), _args(overwrite=True))
    monkeypatch.setattr(release.os, "replace", replace)
    restored = release.verify(_ctx(tmp_path), _args()).data
    assert restored["artifacts"] == original["artifacts"]


def test_release_refuses_pip_config_override(tmp_path, monkeypatch):
    _project(tmp_path)
    monkeypatch.setenv("PIP_CONFIG_FILE", str(tmp_path / "pip.ini"))
    with pytest.raises(AKError) as error:
        release.build(_ctx(tmp_path), _args())
    assert error.value.code == "AK-RELEASE"


def test_release_rollback_stages_an_immutable_wheel_copy(tmp_path, monkeypatch):
    _project(tmp_path)
    monkeypatch.setattr(release, "_git_state", lambda _root: {"commit": "a" * 40, "dirty": False, "epoch": 1})
    monkeypatch.setattr(release, "_run_build", _fake_build)
    release.build(_ctx(tmp_path), _args())
    manifest = json.loads((tmp_path / "dist" / release.MANIFEST).read_text(encoding="utf-8"))
    item = next(item for item in manifest["artifacts"] if item["path"].endswith(".whl"))
    wheel = tmp_path / "dist" / item["path"]
    staged, directory = release._stage_wheel(wheel, item, VERSION)
    try:
        before = staged.read_bytes()
        wheel.write_bytes(b"replaced after staging")
        assert staged.read_bytes() == before
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_windows_rollback_helper_uses_process_handles_and_isolated_python(tmp_path, monkeypatch):
    captured = {}

    def popen(args, **kwargs):
        captured["args"], captured["kwargs"] = args, kwargs
        return SimpleNamespace(pid=123)

    monkeypatch.setattr(release.subprocess, "Popen", popen)
    release._schedule_windows_rollback(["python", "-m", "pip"], tmp_path / "status.json",
                                       tmp_path / "rollback.log", tmp_path / "staging")
    args = captured["args"]
    assert args[1:3] == ["-I", "-c"]
    compile(args[3], "<rollback-helper>", "exec")
    assert "OpenProcess" in args[3] and "os.kill" not in args[3]
    assert "from ctypes import wintypes" in args[3]
    assert "PIP_TARGET" not in captured["kwargs"]["env"]


def test_release_rollback_is_dry_run_by_default(tmp_path, monkeypatch):
    _project(tmp_path)
    monkeypatch.setattr(release, "_git_state", lambda _root: {"commit": "a" * 40, "dirty": False, "epoch": 1})
    monkeypatch.setattr(release, "_run_build", _fake_build)
    release.build(_ctx(tmp_path), _args())

    def forbidden(*_args, **_kwargs):
        raise AssertionError("pip must not run without --yes")

    monkeypatch.setattr(release.subprocess, "run", forbidden)
    result = release.rollback(_ctx(tmp_path), _args())
    assert result.data["dry_run"] is True and result.next.endswith("--yes")


def test_release_windows_console_rollback_is_deferred(tmp_path, monkeypatch):
    _project(tmp_path)
    monkeypatch.setattr(release, "_git_state", lambda _root: {"commit": "a" * 40, "dirty": False, "epoch": 1})
    monkeypatch.setattr(release, "_run_build", _fake_build)
    release.build(_ctx(tmp_path), _args())
    monkeypatch.setattr(release, "_running_console_script", lambda: True)
    calls = []

    def schedule(command, status_file, log_file, cleanup_dir):
        calls.append((command, status_file, log_file, cleanup_dir))
        return 4321

    monkeypatch.setattr(release, "_schedule_windows_rollback", schedule)
    result = release.rollback(_ctx(tmp_path), _args(yes=True))
    assert result.data["scheduled"] is True and result.data["installed"] is False
    assert result.data["helper_pid"] == 4321 and calls


def test_release_rollback_yes_runs_explicit_pip_install(tmp_path, monkeypatch):
    _project(tmp_path)
    monkeypatch.setattr(release, "_git_state", lambda _root: {"commit": "a" * 40, "dirty": False, "epoch": 1})
    monkeypatch.setattr(release, "_run_build", _fake_build)
    release.build(_ctx(tmp_path), _args())
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="installed", stderr="")

    monkeypatch.setattr(release.subprocess, "run", run)
    result = release.rollback(_ctx(tmp_path), _args(yes=True))
    assert result.data["installed"] is True and result.data["dry_run"] is False
    assert calls[0][1:7] == ["-I", "-m", "pip", "--isolated", "install", "--no-deps"]
