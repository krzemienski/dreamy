"""The lock must survive the wheel and work from an installed package [PKG-002].

`tests/unit/test_skills_lock_packaging.py` resolves through `importlib` while
`src/` is importable under pytest, so it asserts on the SOURCE tree. That is
useful but insufficient: adding

    [tool.hatch.build.targets.wheel]
    exclude = ["src/dreamy/resources/skills.lock"]

produces a wheel with no lock in it, and those unit tests still pass 5/5. The
regression they exist to prevent walks straight through them.

This module closes that hole by building a real wheel, installing it into a
clean venv, and running the real console script from a directory containing
no `config/`. Verified against the injected exclude above: this fails, the
unit tests do not.

Marked `slow` — it builds and installs. Deselect with `-m "not slow"`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import venv
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.slow


def _env() -> dict[str, str]:
    """Inherit the real environment, overriding only what must be deterministic.

    Two failure modes motivate this. A hand-built `env={"PATH": ...}` drops
    HOME, and skill resolution is HOME-relative — `Path.home()` then falls back
    to the passwd entry, which happens to be right for the invoking account and
    silently wrong for any other. And a global `PIP_USER=1` makes every venv
    install fail with "Can not perform a '--user' install", which is an
    environment artifact rather than a packaging defect.
    """
    env = dict(os.environ)
    env["PIP_USER"] = "0"
    env.setdefault("HOME", str(Path.home()))
    return env


@pytest.fixture(scope="module")
def wheel(tmp_path_factory) -> Path:
    """Build a real wheel. A build failure is a FAILURE, never a skip.

    Skipping on a non-zero build turns the exact regression this module exists
    to catch into a green run. The only legitimate skip is the toolchain being
    absent altogether, which is checked before anything is attempted.
    """
    if shutil.which("pip") is None and not (Path(sys.prefix) / "bin" / "pip").exists():
        pytest.skip("pip unavailable; cannot build a wheel in this environment")

    out = tmp_path_factory.mktemp("wheel")
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", str(REPO), "--no-deps", "-w", str(out)],
        capture_output=True,
        text=True,
        env=_env(),
    )
    assert proc.returncode == 0, f"wheel build failed:\n{proc.stderr[-1500:]}"
    wheels = sorted(out.glob("*.whl"))
    assert wheels, "pip reported success but produced no wheel"
    return wheels[-1]


@pytest.fixture(scope="module")
def installed(tmp_path_factory, wheel: Path) -> Path:
    """A clean venv with the wheel installed. Returns the console-script path.

    An install failure is a FAILURE for the same reason as above.
    """
    env_dir = tmp_path_factory.mktemp("venv")
    venv.create(env_dir, with_pip=True)
    proc = subprocess.run(
        [str(env_dir / "bin" / "pip"), "install", str(wheel)],
        capture_output=True,
        text=True,
        env=_env(),
    )
    assert proc.returncode == 0, f"clean-venv install failed:\n{proc.stderr[-1500:]}"
    script = env_dir / "bin" / "dreamy"
    assert script.exists(), "wheel installed but console script 'dreamy' is absent"
    return script


def test_lock_is_inside_the_wheel(wheel: Path):
    """The distributed artifact must contain the lock.

    This is the assertion that catches a wheel-only exclude; the unit tests
    cannot, because they read the source tree.
    """
    names = zipfile.ZipFile(wheel).namelist()
    assert "dreamy/resources/skills.lock" in names, (
        f"lock missing from wheel; resources present: "
        f"{[n for n in names if '/resources/' in n]}"
    )


def test_wheel_lock_matches_repository(wheel: Path):
    shipped = json.loads(zipfile.ZipFile(wheel).read("dreamy/resources/skills.lock"))
    source = json.loads((REPO / "config" / "skills.lock").read_text())
    assert shipped == source


def test_installed_cli_verifies_from_foreign_cwd(installed: Path, tmp_path):
    """Run the installed console script from a directory with no `config/`.

    This is the real PKG-002 check: CWD-relative resolution would have found
    `config/skills.lock` from the checkout and passed for the wrong reason.
    """
    workdir = tmp_path / "elsewhere"
    workdir.mkdir()
    assert not (workdir / "config").exists()

    proc = subprocess.run(
        [str(installed), "skills", "verify"],
        cwd=workdir,
        capture_output=True,
        text=True,
        env=_env(),
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr[-400:]}"

    report = json.loads(proc.stdout)
    assert report["ok"] is True
    assert report["checked"] > 0, "exit 0 with nothing checked is a vacuous pass"


def test_installed_package_is_outside_the_checkout(installed: Path):
    """Guards against an editable install masquerading as a real one."""
    python = installed.parent / "python"
    proc = subprocess.run(
        [str(python), "-c", "import dreamy, pathlib; print(pathlib.Path(dreamy.__file__).parent)"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert str(REPO) not in proc.stdout.strip()
