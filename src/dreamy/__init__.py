"""dreamy — read-only cross-harness coding-session reconciler."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

# PKG-003: the version is declared once, in pyproject.toml, and read back from
# installed metadata. A hard-coded literal here drifted from the packaging
# metadata (1.0.0 vs 2.0.0.dev0) precisely because it was a second source of
# truth. The fallback only applies when running from a source tree that was
# never installed.
try:
    __version__ = _version("dreamy")
except PackageNotFoundError:  # pragma: no cover - source tree without install
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
