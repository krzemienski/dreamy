"""Connector registry — all six source connectors."""
from __future__ import annotations

from ..protocol import Connector
from .claude import ClaudeConnector
from .codex import CodexConnector
from .omp_pi import OmpConnector, PiConnector
from .opencode import OpenCodeConnector
from .router import RouterConnector

ALL_CONNECTORS = [
    OmpConnector,
    PiConnector,
    ClaudeConnector,
    CodexConnector,
    OpenCodeConnector,
    RouterConnector,
]


def make_connectors() -> list[Connector]:
    """Instantiate every registered concrete connector."""
    return [
        OmpConnector(),
        PiConnector(),
        ClaudeConnector(),
        CodexConnector(),
        OpenCodeConnector(),
        RouterConnector(),
    ]
