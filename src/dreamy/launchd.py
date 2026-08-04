"""Launchd helper for the dreamy reconciliation scheduler.

All plist writes are atomic (tempfile + os.replace). Schedule mutation
requires explicit confirmation at the CALLER layer — this module is the
primitive, not the policy.
"""
from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .config import DEFAULT_CONFIG_DIR, resolve_path

LABEL = "com.nick.dreamy"


class UndurableInterpreterError(RuntimeError):
    """`install()` refused: the interpreter path would not survive.

    Distinct from RuntimeError so the CLI can present the remediation text
    without also swallowing unrelated runtime failures raised deeper in the
    install path (a launchctl fault, a permission error) as if they were the
    same operator-actionable condition.
    """


def _uid() -> str:
    return str(os.getuid())


def _run(args, timeout: int = 10) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError as exc:
        return 127, "", str(exc)


def is_installed() -> bool:
    rc, _, _ = _run(["launchctl", "print", f"gui/{_uid()}/{LABEL}"])
    return rc == 0


def print_state() -> str:
    """Return raw `launchctl print gui/$UID/<label>` output. Empty on failure."""

    rc, out, err = _run(["launchctl", "print", f"gui/{_uid()}/{LABEL}"])
    return out if rc == 0 else err


def parse_state(raw: str = "") -> dict:
    """Parse the fields VG-5 asserts on out of REAL `launchctl print` output.

    Field names verified against live output on this machine:
        state = running · run interval = 21600 seconds · runs = 1
        last exit code = (never exited) · properties = runatload | inferred program
    """
    text = raw if raw else print_state()
    if not text:
        return {}

    def _field(pattern: str) -> str | None:
        m = re.search(pattern, text, re.MULTILINE)
        return m.group(1).strip() if m else None

    interval = _field(r"^\s*run interval\s*=\s*(\d+)")
    runs = _field(r"^\s*runs\s*=\s*(\d+)")
    props = _field(r"^\s*properties\s*=\s*(.+)$") or ""
    return {
        "state": _field(r"^\s*state\s*=\s*(\S+)"),
        "job_state": _field(r"^\s*job state\s*=\s*(\S+)"),
        "interval_seconds": int(interval) if interval else None,
        "runs": int(runs) if runs else None,
        "last_exit_code": _field(r"^\s*last exit code\s*=\s*(.+)$"),
        "run_at_load": "runatload" in props.lower(),
        "pid": _field(r"^\s*pid\s*=\s*(\d+)"),
        "plist_path": _field(r"^\s*path\s*=\s*(.+)$"),
    }


def _last_run_started_ms() -> int | None:
    """Newest run start recorded in the state DB, or None. Read-only."""
    db = resolve_path(DEFAULT_CONFIG_DIR) / "state.db"
    if not db.exists():
        return None
    try:
        import sqlite3

        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT MAX(started_ms) FROM runs").fetchone()
        finally:
            con.close()
    except Exception:  # noqa: BLE001 — schedule display never breaks a run
        return None
    return int(row[0]) if row and row[0] else None


def next_run_after(interval_seconds: int) -> int | None:
    """Next run time in epoch-ms, derived from LIVE launchctl state.

    launchctl publishes no absolute next-fire time for a StartInterval job, so
    it is reconstructed from the interval launchd ACTUALLY holds — not the
    configured one; they diverge whenever a plist is edited without a reload —
    anchored on the last observed run. Returns None when the job is not
    loaded, which callers render as "not scheduled" rather than inventing a
    time.
    """
    state = parse_state()
    if not state or not state.get("state"):
        return None
    live_interval = state.get("interval_seconds") or interval_seconds
    if not live_interval or live_interval <= 0:
        return None
    now_ms = int(time.time() * 1000)
    anchor_ms = _last_run_started_ms()
    step = live_interval * 1000
    if anchor_ms is None or anchor_ms > now_ms:
        return now_ms + step
    return anchor_ms + ((now_ms - anchor_ms) // step + 1) * step


def render_plist(interval_seconds: int, program_path: str, plist_path: Path) -> Path:
    # PKG-002: the scheduled job must not depend on a source checkout.
    #
    # This previously wrote `<working_dir>/scripts` as WorkingDirectory -- the
    # pre-rename package location. That directory no longer exists, so launchd's
    # posix_spawn failed with FileNotFoundError before the job could start, and
    # nothing caught it because render_plist had no test.
    #
    # The `working_dir` parameter was removed rather than defaulted. Accepting a
    # caller-supplied cwd is what let a checkout path into the plist in the
    # first place, and pointing a scheduled job at a managed target repository
    # would be worse than pointing it at a missing directory. The state
    # directory is the only correct cwd: it is where the run reads and writes,
    # and it is identical under a wheel install and a source checkout.
    #
    # State-directory creation belongs to install(), the operator-invoked action
    # permitted to create Dreamy's own state. This function only writes the
    # plist it was asked to write, so it stays safe to call from a test or a
    # dry-run without materialising a real state tree as a side effect.
    # `type(...) is not int` rather than isinstance: bool subclasses int, so
    # True would pass `<= 0` and render StartInterval=1 -- a job firing every
    # second. load_config rejects that upstream now, but this function is
    # importable on its own, so the guarantee is made structural here rather
    # than relying on every caller being gated.
    if type(interval_seconds) is not int:
        raise TypeError(
            f"interval_seconds must be an int, got {type(interval_seconds).__name__}"
        )
    # The durability guard lives HERE, not only in install(), for the same
    # reason the interval type-check does: render_plist is importable and
    # callable on its own, so the guarantee is structural rather than a
    # property of one caller remembering to check first. A plist naming an
    # interpreter that will not exist next week is malformed output no matter
    # who asked for it.
    #
    # There is deliberately NO override, at any layer. An escape hatch that
    # ships a schedule already known to break is not a feature; the correct
    # fix (install under a durable path) is one command and is printed in the
    # refusal text itself.
    reason = _undurable_reason(program_path)
    if reason is not None:
        raise UndurableInterpreterError(
            f"refusing to render a plist naming a non-durable interpreter: {reason}"
        )
    if interval_seconds <= 0:
        raise ValueError(f"interval_seconds must be positive, got {interval_seconds}")
    state_dir = resolve_path(DEFAULT_CONFIG_DIR)
    plist = {
        "Label": LABEL,
        # Each argv element is a separate array entry, so a path containing
        # spaces needs no quoting and is never re-split by launchd.
        # `-m dreamy` resolves through the interpreter's own import path, which
        # works from site-packages with no cwd-relative assumption.
        "ProgramArguments": [program_path, "-m", "dreamy", "run"],
        "WorkingDirectory": str(state_dir),
        "StartInterval": int(interval_seconds),
        "RunAtLoad": True,
        "StandardOutPath": str(state_dir / "logs" / "launchd.out.log"),
        "StandardErrorPath": str(state_dir / "logs" / "launchd.err.log"),
    }
    plist_path = Path(plist_path)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(plist_path.parent), prefix=".dreamy-", suffix=".plist.tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            plistlib.dump(plist, f)
        os.replace(tmp_name, plist_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return plist_path


# Temp roots whose contents the OS is free to delete between boots. An
# interpreter here is exactly as fragile as one in a checkout: the plist
# survives, the interpreter does not, and the next scheduled run fails to
# spawn with nothing surfaced anywhere.
def _ephemeral_roots() -> list[Path]:
    roots = {
        Path(tempfile.gettempdir()),
        Path("/tmp"),
        Path("/var/folders"),
        Path("/private/tmp"),
        Path("/private/var/folders"),
    }
    out = []
    for r in roots:
        out.append(r)
        try:
            out.append(r.resolve())
        except (OSError, RuntimeError):
            pass
    return out


def _undurable_reason(program_path: str) -> str | None:
    """Explain why *program_path* must not be written into a plist, or None.

    PKG-002 is a DURABILITY invariant, not a checkout-specific one. A plist is
    persistent state that outlives the shell that wrote it, so the interpreter
    it names must still exist at that literal path weeks later. Two shapes
    fail that, both observed on this machine:

    * a source checkout (`<repo>/.venv/bin/python`, what an editable install
      produces) — breaks the moment the repo is moved, renamed, or deleted;
    * an OS temp root (`/tmp/...`, `/var/folders/...`) — deleted on reboot or
      by periodic cleanup, so the schedule silently dies.

    Detection is LEXICAL (`absolute()`, never `resolve()`), and that choice is
    the whole guard. A venv's `bin/python` is a symlink to the system
    interpreter, so `resolve()` reports
    `/Library/Frameworks/.../bin/python3.11` — outside both a checkout and a
    temp root — and every check passes while the defect ships. Measured here:
    resolve() -> None, absolute() -> the checkout. launchd spawns the literal
    string in the plist, so the literal string is what must be durable.
    """
    try:
        here = Path(program_path).absolute()
    except (OSError, RuntimeError):
        return None

    for root in _ephemeral_roots():
        if here == root or root in here.parents:
            return (
                f"{program_path} lives under the temporary directory {root}, whose "
                "contents the OS deletes on reboot or during periodic cleanup"
            )

    for parent in here.parents:
        if (parent / "pyproject.toml").exists() or (parent / ".git").exists():
            return (
                f"{program_path} lives inside the source checkout {parent}; moving, "
                "renaming, or deleting that checkout breaks every future run"
            )
    return None


def _checkout_root(program_path: str) -> Path | None:
    """Return the source checkout the interpreter lives in, or None.

    Retained as the narrow checkout-only predicate. `install()` gates on
    `_undurable_reason`, which is strictly broader.
    """
    try:
        here = Path(program_path).absolute()
    except (OSError, RuntimeError):
        return None
    for parent in here.parents:
        if (parent / "pyproject.toml").exists() or (parent / ".git").exists():
            return parent
    return None



def install(interval_seconds: int) -> tuple[Path, str]:
    """Write the plist and `launchctl bootstrap` it. Returns (plist_path, stdout).

    Takes no working directory: the scheduled job always runs from the Dreamy
    state directory. Callers previously passed a checkout-derived path
    (`Path(__file__).parents[N]`), which resolved into site-packages under a
    wheel install and named a deleted directory after the src/ rename.
    """
    # No environment override. See render_plist's note: an escape hatch that
    # ships a schedule already known to break is not a feature.
    #
    # BOTH the interpreter and the package location are checked. An editable
    # install (`~/.venvs/dreamy/bin/pip install -e <checkout>`) yields a
    # durable interpreter while `dreamy` itself still resolves into the
    # checkout, so checking the interpreter alone would pass and still ship a
    # schedule that dies when the checkout moves.
    reason = _undurable_reason(sys.executable)
    if reason is None:
        package_reason = _undurable_reason(str(Path(__file__).parent))
        if package_reason is not None:
            reason = (
                f"the dreamy package itself is loaded from a non-durable location "
                f"({package_reason}), which an editable install produces even when "
                f"the interpreter path is durable"
            )
    if reason is not None:
        raise UndurableInterpreterError(
            "refusing to install a scheduled job pinned to a non-durable interpreter: "
            f"{reason}.\n"
            "The plist outlives this shell, so launchd would fail to spawn with no "
            "visible error the next time the job fires.\n"
            "Install dreamy under a persistent path first, e.g.:\n"
            "  python3 -m venv ~/.venvs/dreamy\n"
            "  ~/.venvs/dreamy/bin/pip install <path-to-checkout>\n"
            "  ~/.venvs/dreamy/bin/dreamy install\n"
            "(a non-editable install — `-e` leaves the package in the checkout)"
        )
    state_dir = resolve_path(DEFAULT_CONFIG_DIR)
    # Directory creation lives here, not in render_plist: install() is the
    # operator-invoked action allowed to create Dreamy's own state, and a
    # function named render_* must not touch the filesystem beyond the plist it
    # is asked to write.
    (state_dir / "logs").mkdir(parents=True, exist_ok=True)
    plist_path = render_plist(
        interval_seconds=interval_seconds,
        program_path=sys.executable,
        plist_path=Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist",
    )
    if is_installed():
        _run(["launchctl", "bootout", f"gui/{_uid()}/{LABEL}"])
    rc, out, err = _run(["launchctl", "bootstrap", f"gui/{_uid()}", str(plist_path)])
    captured = out if rc == 0 else (err or out)
    return plist_path, captured


def uninstall() -> tuple[bool, str]:
    """`launchctl bootout` if loaded. Returns (loaded, stdout_or_stderr)."""

    rc, out, err = _run(["launchctl", "bootout", f"gui/{_uid()}/{LABEL}"])
    if rc == 0:
        plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        if plist.exists():
            plist.unlink()
        return True, out
    plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    if plist.exists():
        try:
            plist.unlink()
        except OSError:
            pass
    return False, err or out
