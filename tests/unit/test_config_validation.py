"""Config trust-boundary validation (N-03).

`Config.validate` range-checked its integer fields but never type-checked them.
Because `bool` subclasses `int`, `{"interval_seconds": true}` satisfied `> 0`,
reached the plist renderer as `int(True)`, and produced a launchd job scheduled
to fire **every second** — with no error and no warning. `3600.7` likewise
passed and silently truncated.

These tests exercise the real load path (`load_config` reading an actual JSON
file), not `Config.validate()` in isolation. Validating the dataclass directly
would miss the boundary that actually matters: the file a user edits.
"""

from __future__ import annotations

import json

import pytest

from dreamy.config import Config, load_config

# Every field declared `int` on the dataclass. Floats are excluded deliberately:
# spend_cap_usd and spend_warn_usd are declared Optional[float] and legitimately
# accept non-integers, so they are not part of this fix.
INT_FIELDS = [
    "interval_seconds",
    "lookback_days",
    "correlation_window_seconds",
    "retention_days",
]


def _write(tmp_path, field, value):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({field: value}))
    return p


@pytest.mark.parametrize("field", INT_FIELDS)
@pytest.mark.parametrize(
    "value,label",
    [(True, "bool-true"), (False, "bool-false")],
)
def test_bool_rejected(tmp_path, field, value, label):
    """`bool` is the dangerous case: it subclasses `int` and passes `> 0`."""
    with pytest.raises(ValueError, match=f"{field} must be an integer"):
        load_config(str(_write(tmp_path, field, value)))


@pytest.mark.parametrize("field", INT_FIELDS)
def test_float_rejected(tmp_path, field):
    """A float would silently truncate rather than fail loudly."""
    with pytest.raises(ValueError, match=f"{field} must be an integer"):
        load_config(str(_write(tmp_path, field, 3600.7)))


@pytest.mark.parametrize("field", INT_FIELDS)
def test_string_rejected(tmp_path, field):
    """A str must produce a clean validation error, never a TypeError.

    The type check gates the range check with `elif`, so a str never reaches
    the `<= 0` comparison.
    """
    with pytest.raises(ValueError, match=f"{field} must be an integer"):
        load_config(str(_write(tmp_path, field, "3600")))


@pytest.mark.parametrize("field", INT_FIELDS)
@pytest.mark.parametrize("value", [0, -1, -3600])
def test_non_positive_rejected(tmp_path, field, value):
    with pytest.raises(ValueError, match=f"{field} must be > 0"):
        load_config(str(_write(tmp_path, field, value)))


@pytest.mark.parametrize("field", INT_FIELDS)
def test_valid_int_accepted(tmp_path, field):
    cfg = load_config(str(_write(tmp_path, field, 3600)))
    assert getattr(cfg, field) == 3600


def test_defaults_are_valid():
    """The shipped defaults must pass the validator they are checked against."""
    assert Config().validate() == []


def test_error_message_names_the_actual_type():
    """A deterministic message that names the offending type, and no value.

    Config may carry sensitive values; validation errors surface in logs, so
    they report the type only.
    """
    errors = Config(interval_seconds=True).validate()  # type: ignore[arg-type]
    assert errors == ["interval_seconds must be an integer, got bool"]
