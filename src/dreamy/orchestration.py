"""Bounded deterministic execution for independent pipeline units."""
from __future__ import annotations

import time
import traceback
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Generic, TypeVar

UnitT = TypeVar("UnitT")
ResultT = TypeVar("ResultT")

_MAX_MESSAGES = 16
_MAX_MESSAGE_CHARS = 4_000


@dataclass(frozen=True)
class UnitOutput(Generic[ResultT]):
    """A worker's computed value plus non-fatal diagnostics."""

    result: ResultT
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResultEnvelope(Generic[ResultT]):
    """The complete, immutable outcome of one independent unit."""

    unit_id: str
    started_monotonic: float
    ended_monotonic: float
    result: ResultT | None
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


def _bounded_messages(messages: Iterable[object]) -> tuple[str, ...]:
    bounded: list[str] = []
    for message in messages:
        if len(bounded) >= _MAX_MESSAGES:
            break
        text = str(message)
        if len(text) > _MAX_MESSAGE_CHARS:
            text = text[: _MAX_MESSAGE_CHARS - 3] + "..."
        bounded.append(text)
    return tuple(bounded)


def _execute_unit(
    unit_id: str,
    unit: UnitT,
    worker: Callable[[UnitT], UnitOutput[ResultT]],
) -> ResultEnvelope[ResultT]:
    started = time.monotonic()
    try:
        output = worker(unit)
    except Exception:  # noqa: BLE001 — one failed unit must not cancel siblings
        return ResultEnvelope(
            unit_id=unit_id,
            started_monotonic=started,
            ended_monotonic=time.monotonic(),
            result=None,
            errors=_bounded_messages((traceback.format_exc(),)),
        )
    return ResultEnvelope(
        unit_id=unit_id,
        started_monotonic=started,
        ended_monotonic=time.monotonic(),
        result=output.result,
        warnings=_bounded_messages(output.warnings),
    )


def map_units(
    units: Iterable[UnitT],
    *,
    unit_id: Callable[[UnitT], str],
    worker: Callable[[UnitT], UnitOutput[ResultT]],
    max_workers: int,
) -> list[ResultEnvelope[ResultT]]:
    """Compute independent units and return outcomes sorted by unit id.

    Worker count one deliberately bypasses the executor so it remains the exact
    sequential reference mode. Parallel completion order never reaches callers.
    """
    if type(max_workers) is not int or max_workers <= 0:
        raise ValueError("max_workers must be a positive integer")

    identified = [(unit_id(unit), unit) for unit in units]
    ids = [identifier for identifier, _ in identified]
    if len(ids) != len(set(ids)):
        raise ValueError("unit ids must be unique")
    if not identified:
        return []

    if max_workers == 1:
        envelopes = [
            _execute_unit(identifier, unit, worker)
            for identifier, unit in identified
        ]
    else:
        worker_count = min(max_workers, len(identified))
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="dreamy-analysis",
        ) as executor:
            futures = [
                executor.submit(_execute_unit, identifier, unit, worker)
                for identifier, unit in identified
            ]
            envelopes = [future.result() for future in futures]

    return sorted(envelopes, key=lambda envelope: envelope.unit_id)
