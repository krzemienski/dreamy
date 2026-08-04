"""TUI package — cyberpunk dashboard backed by ReadStore + launchd."""
from __future__ import annotations

from .app import DreamyApp

__all__ = ["DreamyApp", "launch"]


def launch(db_path, output_dir) -> None:
    from ..read import ReadStore

    rs = ReadStore(db_path, output_dir=output_dir)
    DreamyApp(read_store=rs, output_dir=output_dir).run()
