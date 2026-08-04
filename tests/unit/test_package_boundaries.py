"""Runtime code must not depend on directories absent after install [PKG-002].

`tools/` and `scripts/` are excluded from the wheel. An import of either from
`src/dreamy/` works in the checkout and raises `ModuleNotFoundError` on a
user's machine — the same class of defect as a CWD-relative default: invisible
in development, broken everywhere else.

AST-based rather than a text scan, so a mention inside a docstring or comment
(there are several, describing this very boundary) is not mistaken for a
dependency.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PACKAGE = REPO / "src" / "dreamy"
FORBIDDEN = {"tools", "scripts"}


def _module_files() -> list[Path]:
    return sorted(p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


def test_package_has_modules():
    """Guard the guard: an empty file list would make every check below vacuous."""
    assert len(_module_files()) > 20


@pytest.mark.parametrize("path", _module_files(), ids=lambda p: p.name)
def test_no_imports_from_excluded_dirs(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root not in FORBIDDEN, (
                    f"{path.relative_to(REPO)} imports '{alias.name}'; "
                    f"'{root}/' is not shipped in the wheel"
                )
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import, which cannot escape the package.
            if node.level == 0 and node.module:
                root = node.module.split(".")[0]
                assert root not in FORBIDDEN, (
                    f"{path.relative_to(REPO)} imports from '{node.module}'; "
                    f"'{root}/' is not shipped in the wheel"
                )


def test_excluded_dirs_are_absent_from_the_wheel_config():
    """`tools/` and `scripts/` must not be declared as packaged content.

    hatchling ships only what the wheel target names. This asserts the intent
    is recorded in `pyproject.toml`; `tests/integration/test_wheel_lock_shipping.py`
    asserts the built artifact actually matches.
    """
    import tomllib

    with (REPO / "pyproject.toml").open("rb") as fh:
        cfg = tomllib.load(fh)

    wheel = cfg.get("tool", {}).get("hatch", {}).get("build", {}).get("targets", {}).get("wheel", {})
    packages = wheel.get("packages", [])
    assert packages, "wheel target declares no packages"
    for entry in packages:
        assert not any(entry.strip("/").startswith(d) for d in FORBIDDEN), (
            f"wheel packages includes '{entry}', which must not ship"
        )
