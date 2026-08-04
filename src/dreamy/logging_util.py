"""Structured topic logging: JSONL files + in-memory ring for live GUI."""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

from . import redact as _redact

TOPICS = (
    "ingest",
    "correlate",
    "analyze",
    "agent",
    "research",
    "teacher",
    "friends",
    "compiler",
    "report",
    "schedule",
    "system",
)

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}
_RING_MAX = 500

_lock = threading.Lock()
_output_dir: Path | None = None
_run_id: str | None = None
_min_level = _LEVELS["DEBUG"]
_ring: deque[dict] = deque(maxlen=_RING_MAX)


def configure_logging(output_dir: Path, run_id: str | None = None, level: str = "DEBUG") -> None:
    """Set the global logging destination. Call once at startup."""
    global _output_dir, _run_id, _min_level
    output_dir = Path(output_dir)
    with _lock:
        _output_dir = output_dir
        _run_id = run_id
        _min_level = _LEVELS.get(level.upper(), _LEVELS["DEBUG"])
    (output_dir / "logs").mkdir(parents=True, exist_ok=True)
    if run_id:
        (output_dir / "runs").mkdir(parents=True, exist_ok=True)


def _redact_fields(fields: dict) -> dict:
    return _redact.redact_dict(fields) if fields else fields


def _write(topic: str, level: str, msg: str, fields: dict) -> None:
    with _lock:
        if _LEVELS.get(level, 0) < _min_level:
            return
        output_dir = _output_dir
        run_id = _run_id

    now = time.time()
    event = {
        "ts_ms": int(now * 1000),
        "ts_iso": datetime.fromtimestamp(now, tz=UTC).isoformat(),
        "topic": topic,
        "level": level,
        "msg": _redact.redact(msg) if isinstance(msg, str) else msg,
        **_redact_fields(fields),
    }

    with _lock:
        _ring.append(event)

    if output_dir is None:
        return

    line = json.dumps(event, default=str) + "\n"
    with _lock:
        topic_path = output_dir / "logs" / f"{topic}.jsonl"
        with open(topic_path, "a", encoding="utf-8") as f:
            f.write(line)
        if run_id:
            run_path = output_dir / "runs" / f"{run_id}.jsonl"
            with open(run_path, "a", encoding="utf-8") as f:
                f.write(line)


class TopicLogger:
    """Logger bound to a single topic."""

    def __init__(self, topic: str):
        self.topic = topic

    def debug(self, msg: str, **fields) -> None:
        _write(self.topic, "DEBUG", msg, fields)

    def info(self, msg: str, **fields) -> None:
        _write(self.topic, "INFO", msg, fields)

    def warn(self, msg: str, **fields) -> None:
        _write(self.topic, "WARN", msg, fields)

    def error(self, msg: str, **fields) -> None:
        _write(self.topic, "ERROR", msg, fields)


_loggers: dict[str, TopicLogger] = {}


def get_logger(topic: str) -> TopicLogger:
    """Get (or create) the logger for a topic."""
    with _lock:
        logger = _loggers.get(topic)
        if logger is None:
            logger = TopicLogger(topic)
            _loggers[topic] = logger
        return logger


def get_recent_events(limit: int = 100, topic: str | None = None) -> list[dict]:
    """Return the most recent in-memory events, newest last."""
    with _lock:
        events = list(_ring)
    if topic is not None:
        events = [e for e in events if e["topic"] == topic]
    if limit is not None:
        events = events[-limit:]
    return events
