# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""M-19: an AI that only reads llms.txt can install AgentKit and run one verify (eval/llms_check.py)."""
import json
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from eval import llms_check as L  # noqa: E402


def test_llms_install_section_is_mechanically_followable():
    text = (ROOT / "llms.txt").read_text()
    alts, follow = L.split_install(L.install_commands(text))
    assert alts[0].startswith("pip install " + L.GIT_URL)
    assert any(a.startswith("pipx install") for a in alts) and any(a.startswith("uv tool install") for a in alts)
    assert follow and follow[0] == "agentkit doctor"
    assert L.path_hint(text) == "$HOME/.local/bin:$PATH"
    assert L.verify_example(text).startswith('agentkit verify --check "')
    # every alternative installs the same package from the documented URL
    assert all(L.GIT_URL in a for a in alts)


def _online() -> bool:
    try:
        socket.create_connection(("pypi.org", 443), timeout=3).close()
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _online(), reason="needs network to fetch dependencies from PyPI")
def test_fresh_venv_install_from_llms_txt_then_doctor_and_verify():
    p = subprocess.run([sys.executable, "-m", "eval.llms_check", "--src", str(ROOT)], cwd=str(ROOT),
                       capture_output=True, text=True, timeout=600)
    res = json.loads(p.stdout.strip().splitlines()[-1])
    assert res["ok"], res
    assert [s["cmd"].split()[1] for s in res["steps"] if s["cmd"].startswith("agentkit")] == ["doctor", "verify"]


@pytest.mark.skipif(not _online() or not shutil.which("uv") and not shutil.which("pipx"),
                    reason="bare-system scenario needs network and uv or pipx")
def test_bare_system_python_without_pip_falls_through_to_pipx_or_uv():
    p = subprocess.run([sys.executable, "-m", "eval.llms_check", "--src", str(ROOT), "--scenario", "bare"], cwd=str(ROOT),
                       capture_output=True, text=True, timeout=600)
    res = json.loads(p.stdout.strip().splitlines()[-1])
    assert res["ok"], res
    assert res["chosen"].split()[0] in ("pipx", "uv")
