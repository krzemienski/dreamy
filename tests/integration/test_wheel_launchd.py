"""Wheel-install verification of the launchd plist (F-01).

`tests/unit/test_launchd_plist.py` covers the renderer's logic and stays fast.
This file covers what unit tests structurally cannot: the reviewer found F-01 by
observing behaviour *under a wheel install*, where `Path(__file__).parents[N]`
resolves into `site-packages` rather than a repo. A source-tree test cannot
reproduce that, because in a source tree the wrong path happens to exist.

Marked `integration` and skipped unless explicitly opted in: it builds a wheel
and creates a venv, which is far too slow and side-effecting for the default
suite.

Run with:
    DREAMY_RUN_ACCEPTANCE=1 pytest tests/integration/test_wheel_launchd.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

_OPT_IN = os.environ.get("DREAMY_RUN_ACCEPTANCE") == "1"

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.skipif(
        not _OPT_IN,
        reason="builds a wheel and a venv; set DREAMY_RUN_ACCEPTANCE=1 to run",
    ),
    pytest.mark.skipif(
        shutil.which("uv") is None, reason="needs uv to build the wheel"
    ),
]


@pytest.fixture(scope="module")
def wheel_env(tmp_path_factory):
    """Build the current source into a wheel and install it into a clean venv.

    Built fresh rather than reusing a prebuilt artifact: a stale wheel would
    silently verify code that is no longer in the tree, which is exactly the
    class of mistake this test exists to catch.

    The venv is created under `~/.local/share/dreamy/acceptance/venvs/`, NOT
    under the pytest temp root. `render_plist` refuses temp-root interpreters
    (a plist naming one is dead at the next reboot), so a `/var/folders` venv
    could only be tested by weakening the guard or by passing a different
    interpreter — either of which would stop exercising the installed wheel,
    the whole point of this test. A unique per-run directory is created and
    removed here, and only if this fixture created it.
    """
    work = tmp_path_factory.mktemp("wheel-env")
    dist = work / "dist"
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dist)],
        cwd=REPO, check=True, capture_output=True, text=True,
    )
    wheels = sorted(dist.glob("*.whl"))
    assert wheels, "uv build produced no wheel"

    venv_root = Path.home() / ".local/share/dreamy/acceptance/venvs"
    venv_root.mkdir(parents=True, exist_ok=True)
    venv = venv_root / f"wheel-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    assert not venv.exists(), f"refusing to reuse an existing path: {venv}"
    created = False
    try:
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        created = True
        py = venv / "bin" / "python"
        env = dict(os.environ)
        # The global pip.conf sets `user = true`, which errors inside a venv.
        env["PIP_USER"] = "0"
        subprocess.run(
            [str(venv / "bin" / "pip"), "install", "--quiet", str(wheels[0])],
            check=True, env=env, capture_output=True, text=True,
        )
        yield py, work
    finally:
        # Only ever removes the unique directory this fixture made.
        if created:
            shutil.rmtree(venv, ignore_errors=True)


def _run_in_wheel(py: Path, work: Path, body: str) -> dict:
    """Execute `body` under the installed wheel, from a non-checkout cwd."""
    script = work / "probe.py"
    script.write_text(textwrap.dedent(body))
    cwd = work / "elsewhere"
    cwd.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)  # nothing may leak in from the checkout
    proc = subprocess.run(
        [str(py), str(script)],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"probe failed:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout)


def test_plist_from_installed_wheel_has_no_checkout_path(wheel_env):
    py, work = wheel_env
    result = _run_in_wheel(py, work, """
        import json, plistlib, sys, tempfile
        from pathlib import Path
        from dreamy import launchd

        state = Path(tempfile.mkdtemp(prefix="state-"))
        launchd.DEFAULT_CONFIG_DIR = str(state)
        out = Path(tempfile.mkdtemp(prefix="plist-")) / "LaunchAgents" / "t.plist"
        launchd.render_plist(
            interval_seconds=21600, program_path=sys.executable, plist_path=out
        )
        data = plistlib.loads(out.read_bytes())
        print(json.dumps({
            "module": launchd.__file__,
            "plist": {k: (str(v) if not isinstance(v, (list, int, bool)) else v)
                      for k, v in data.items()},
            "cwd_exists": Path(data["WorkingDirectory"]).is_dir(),
        }))
    """)

    assert "site-packages" in result["module"], "probe did not use the wheel"

    plist = result["plist"]
    blob = json.dumps(plist)
    assert "scripts" not in plist["WorkingDirectory"]
    assert "site-packages" not in plist["WorkingDirectory"]
    assert "Desktop/dreamer" not in blob, f"checkout path leaked: {blob}"
    assert result["cwd_exists"], "launchd cannot spawn into a missing directory"
    assert {
        "Label", "ProgramArguments", "WorkingDirectory",
        "StartInterval", "RunAtLoad", "StandardOutPath", "StandardErrorPath",
    } <= set(plist)


def test_module_form_resolves_under_wheel(wheel_env):
    """The plist runs `<python> -m dreamy run`; prove that entry point works."""
    py, work = wheel_env
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    cwd = work / "elsewhere"
    cwd.mkdir(exist_ok=True)
    proc = subprocess.run(
        [str(py), "-m", "dreamy", "--help"],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "dreamy" in proc.stdout
