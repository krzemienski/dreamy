"""`python -m dreamy` shim.

PKG-002 requires that production execution not depend on the source checkout.
The console entry point declared in pyproject.toml is
``dreamy = "dreamy.cli:main"``. This module exists so the module form
(`python -m dreamy`) and the installed-script form (`dreamy`) dispatch through
exactly one implementation, rather than drifting into two argument parsers.
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
