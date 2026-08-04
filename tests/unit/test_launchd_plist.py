"""Regression tests for the launchd plist renderer (F-01).

The `src/dreamy` rename left `render_plist` writing
`WorkingDirectory = <checkout>/scripts`, a directory that no longer exists.
launchd's posix_spawn fails outright on a non-existent cwd, so the scheduled
job could never start. Nothing caught it because `render_plist` had no test at
all — which is the actual defect these tests close.

Every test isolates the state directory into a tmp_path. A test that touched the
real `~/.local/share/dreamy` would both pollute a developer's machine and hide
the bug it is meant to catch, since the real directory usually exists.
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path

import pytest

from dreamy import launchd

REQUIRED_KEYS = {
    "Label",
    "ProgramArguments",
    "WorkingDirectory",
    "StartInterval",
    "RunAtLoad",
    "StandardOutPath",
    "StandardErrorPath",
}


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Point the module's state dir at tmp_path for the duration of a test."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(launchd, "DEFAULT_CONFIG_DIR", str(state))
    return state


# The resolved system interpreter: outside any checkout and outside every temp
# root, so the durability guard accepts it. Tests cannot pass `sys.executable`
# directly — under a development checkout that is `<repo>/.venv/bin/python`,
# which the guard refuses by design — and cannot use `tmp_path`, which pytest
# places under `/private/var/folders`, a temp root the guard also refuses.
# Verified: `_undurable_reason(DURABLE_INTERPRETER)` is None (see the premise
# assertion below), so no filesystem fixture is needed.
DURABLE_INTERPRETER = str(Path(sys.executable).resolve())


def test_durable_interpreter_premise():
    """Every render test below depends on this constant being accepted."""
    assert launchd._undurable_reason(DURABLE_INTERPRETER) is None


def _render(tmp_path, isolated_state, **kw):
    out = tmp_path / "LaunchAgents" / "test.plist"
    launchd.render_plist(
        interval_seconds=kw.get("interval_seconds", 21600),
        program_path=kw.get("program_path", DURABLE_INTERPRETER),
        plist_path=out,
    )
    return plistlib.loads(out.read_bytes()), out


def test_all_required_keys_present(tmp_path, isolated_state):
    """A dropped key is a silent functional regression.

    Losing StartInterval yields a job that never fires on a schedule; losing the
    log paths yields a job whose failures are invisible. Both look fine in a
    diff.
    """
    data, _ = _render(tmp_path, isolated_state)
    assert REQUIRED_KEYS <= set(data), f"missing keys: {sorted(REQUIRED_KEYS - set(data))}"


def test_working_directory_is_state_dir_not_scripts(tmp_path, isolated_state):
    """The F-01 regression itself."""
    data, _ = _render(tmp_path, isolated_state)
    assert data["WorkingDirectory"] == str(isolated_state)
    assert "scripts" not in data["WorkingDirectory"]


def test_working_directory_exists(tmp_path, isolated_state):
    """launchd cannot spawn into a directory that is not there."""
    data, _ = _render(tmp_path, isolated_state)
    assert Path(data["WorkingDirectory"]).is_dir()


def test_no_checkout_path_anywhere_in_plist(tmp_path, isolated_state):
    """PKG-002: nothing may pin the plist to a source checkout."""
    data, _ = _render(tmp_path, isolated_state)
    blob = str(data)
    for marker in ("Desktop/dreamer", "/src/dreamy", "site-packages"):
        assert marker not in blob, f"plist references {marker!r}: {blob}"


def test_program_arguments_shape(tmp_path, isolated_state):
    """argv must be a real array so a path with spaces is never re-split."""
    data, _ = _render(tmp_path, isolated_state)
    argv = data["ProgramArguments"]
    assert argv == [DURABLE_INTERPRETER, "-m", "dreamy", "run"]
    assert Path(argv[0]).exists(), "interpreter path does not exist"


def test_interval_is_honoured(tmp_path, isolated_state):
    data, _ = _render(tmp_path, isolated_state, interval_seconds=900)
    assert data["StartInterval"] == 900


@pytest.mark.parametrize("bad", [0, -1, -3600])
def test_rejects_non_positive_interval(tmp_path, isolated_state, bad):
    """A zero or negative StartInterval is rejected at the boundary."""
    with pytest.raises(ValueError):
        _render(tmp_path, isolated_state, interval_seconds=bad)


def test_render_creates_no_state_directories(tmp_path, monkeypatch):
    """`render_plist` is pure apart from the plist it is asked to write.

    State creation belongs to install(), the operator-invoked action. If render
    created it, calling render from a test or a dry run would materialise a real
    state tree as a side effect.
    """
    state = tmp_path / "not-created-yet"
    monkeypatch.setattr(launchd, "DEFAULT_CONFIG_DIR", str(state))
    out = tmp_path / "x.plist"
    launchd.render_plist(
        interval_seconds=3600, program_path=DURABLE_INTERPRETER, plist_path=out
    )
    assert out.is_file(), "plist should still be written"
    assert not state.exists(), "render_plist must not create the state directory"


def test_plist_parent_directory_is_created(tmp_path, isolated_state):
    """`~/Library/LaunchAgents` may not exist on a fresh machine."""
    out = tmp_path / "deep" / "nested" / "test.plist"
    launchd.render_plist(
        interval_seconds=3600, program_path=DURABLE_INTERPRETER, plist_path=out
    )
    assert out.is_file()


def test_install_signature_takes_no_working_dir():
    """The removed parameter is what let a checkout path into the plist.

    Both former callers passed `Path(__file__).resolve().parents[N]`, which
    resolves into site-packages under a wheel install.
    """
    import inspect

    params = list(inspect.signature(launchd.install).parameters)
    assert params == ["interval_seconds"], f"unexpected signature: {params}"


@pytest.mark.parametrize("bad", [True, False, 3600.7, "3600", None])
def test_render_plist_rejects_non_int_interval(tmp_path, isolated_state, bad):
    """Structural, not conventional.

    `bool` subclasses `int`, so True passed the old `<= 0` guard and rendered
    StartInterval=1. load_config rejects it upstream now, but render_plist is
    importable on its own and must defend itself.
    """
    with pytest.raises((TypeError, ValueError)):
        launchd.render_plist(
            interval_seconds=bad,
            program_path=DURABLE_INTERPRETER,
            plist_path=tmp_path / "x.plist",
        )


def _fake_venv(root: Path, marker: str = "pyproject.toml") -> Path:
    """Build a checkout whose `.venv/bin/python` is a SYMLINK, as venv does.

    The symlink is the point. `Path.resolve()` follows it out of the checkout
    to the system interpreter, so a guard written against `resolve()` reports
    "not a checkout" for the exact layout that ships the defect.
    """
    (root / marker).write_text("[project]\nname='x'\n")
    bindir = root / ".venv" / "bin"
    bindir.mkdir(parents=True)
    link = bindir / "python"
    link.symlink_to(Path(sys.executable).resolve())
    return link


def test_checkout_root_sees_through_venv_symlink(tmp_path):
    """PKG-002 regression: detection must be lexical, not resolved.

    Guarded with the real symlink shape because a `resolve()`-based walk
    passes every non-symlink test and still misses every real venv.
    """
    checkout = tmp_path / "repo"
    checkout.mkdir()
    link = _fake_venv(checkout)
    assert link.is_symlink(), "fixture must reproduce the real venv shape"
    assert link.resolve().is_relative_to(checkout) is False
    assert launchd._checkout_root(str(link)) == checkout


def test_checkout_root_detects_git_only_checkout(tmp_path):
    """A checkout marked by `.git` with no pyproject is still a checkout."""
    checkout = tmp_path / "repo"
    (checkout / ".git").mkdir(parents=True)
    link = _fake_venv(checkout, marker=".gitignore")
    assert launchd._checkout_root(str(link)) == checkout


def test_checkout_root_ignores_installed_interpreter(tmp_path):
    """A wheel-install interpreter has no checkout ancestor — install proceeds."""
    venv = tmp_path / "venvs" / "dreamy" / "bin"
    venv.mkdir(parents=True)
    interp = venv / "python"
    interp.symlink_to(Path(sys.executable).resolve())
    assert launchd._checkout_root(str(interp)) is None


def test_install_refuses_checkout_interpreter(tmp_path, isolated_state, monkeypatch):
    """`install()` must refuse before writing any persistent state.

    The plist outlives the shell: pinning it to a checkout breaks every future
    scheduled run once the checkout moves, with no error surfaced anywhere.
    """
    checkout = tmp_path / "repo"
    checkout.mkdir()
    link = _fake_venv(checkout)
    monkeypatch.setattr(launchd.sys, "executable", str(link))

    with pytest.raises(launchd.UndurableInterpreterError) as excinfo:
        launchd.install(21600)

    message = str(excinfo.value)
    assert str(checkout) in message, "refusal must name the offending checkout"
    assert "~/.venvs/dreamy" in message, "refusal must print the remediation"
    assert not (isolated_state / "logs").exists(), (
        "install() must refuse BEFORE creating state directories"
    )


def test_render_plist_refuses_undurable_interpreter(tmp_path):
    """The renderer defends itself; it does not trust `install()` to check.

    `render_plist` is importable and callable on its own, so a plist naming a
    doomed interpreter must be impossible to produce through any entry point.
    """
    checkout = tmp_path / "repo"
    checkout.mkdir()
    link = _fake_venv(checkout)
    out = tmp_path / "x.plist"

    with pytest.raises(launchd.UndurableInterpreterError):
        launchd.render_plist(
            interval_seconds=3600, program_path=str(link), plist_path=out
        )
    assert not out.exists(), "no plist may be written when the guard fires"


@pytest.mark.parametrize(
    "path",
    ["/tmp/v/bin/python", "/private/tmp/v/bin/python", "/var/folders/x9/T/v/bin/python"],
)
def test_temp_roots_are_undurable(path):
    """A temp-root interpreter dies on reboot, exactly like a moved checkout.

    Caught in review: an earlier proof of this guard used `/private/tmp`, which
    would itself have been deleted by periodic cleanup.
    """
    assert launchd._undurable_reason(path) is not None


def test_persistent_interpreter_is_durable():
    """A wheel install under a persistent path is accepted.

    Uses DURABLE_INTERPRETER, not a `tmp_path` venv: pytest places tmp_path
    under `/private/var/folders`, a temp root the guard refuses by design, so
    a tmp_path-based assertion here would encode a false premise and fail.
    """
    assert launchd._undurable_reason(DURABLE_INTERPRETER) is None


def test_install_refuses_editable_install_from_durable_interpreter(
    tmp_path, isolated_state, monkeypatch
):
    """The editable-install hole: durable interpreter, checkout-resident package.

    `~/.venvs/dreamy/bin/pip install -e <checkout>` produces an interpreter
    that passes every durability check while `dreamy` itself still imports out
    of the checkout. Checking only the interpreter ships a schedule that dies
    the moment the checkout moves.
    """
    monkeypatch.setattr(launchd.sys, "executable", DURABLE_INTERPRETER)
    assert launchd._undurable_reason(DURABLE_INTERPRETER) is None, "fixture premise"

    checkout = tmp_path / "repo"
    pkg = checkout / "src" / "dreamy"
    pkg.mkdir(parents=True)
    (checkout / "pyproject.toml").write_text("[project]\nname='dreamy'\n")
    monkeypatch.setattr(launchd, "__file__", str(pkg / "launchd.py"))

    with pytest.raises(launchd.UndurableInterpreterError) as excinfo:
        launchd.install(21600)
    assert "editable" in str(excinfo.value), "refusal must explain the -e case"
