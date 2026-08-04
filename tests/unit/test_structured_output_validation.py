"""The stdlib structured-output validator must actually validate.

`pyproject.toml` declares `dependencies = []`. `jsonschema` is only ever
present transitively (via `mcp`), so the hand-rolled walker is the DEFAULT
production path, not a rare degradation — and it has to hold on its own.

An earlier version walked one level while its docstring claimed recursion. It
accepted a nested type violation, a bad array element, and a boolean where an
integer was required. The last is the instructive one: the guard existed but
sat *below* an `isinstance(value, (int,))` check, and `isinstance(True, int)`
is True in Python, so it could never be reached.

Every test here calls `_walk_schema` directly rather than `_validate_structured`.
That is deliberate: this development environment HAS `jsonschema` transitively,
so going through the public entry point would silently exercise the fast path
and prove nothing about the fallback.
"""

from __future__ import annotations

import pytest

from dreamy.agent_sdk import _validate_structured, _walk_schema

SCHEMA = {
    "type": "object",
    "properties": {
        "n": {"type": "integer"},
        "s": {"type": "string"},
        "sev": {"type": "string", "enum": ["low", "high"]},
        "nested": {
            "type": "object",
            "properties": {"deep": {"type": "integer"}},
            "required": ["deep"],
        },
        "arr": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["n", "s"],
}


def _valid() -> dict:
    return {"n": 1, "s": "x", "sev": "low", "nested": {"deep": 2}, "arr": [1, 2]}


def test_valid_object_is_accepted() -> None:
    """The control. Without it every rejection below could be spurious."""
    assert _walk_schema(_valid(), SCHEMA, path="$") is None


@pytest.mark.parametrize(
    ("label", "obj", "expect_in_message"),
    [
        ("bool where integer required", {"n": True, "s": "x"}, "boolean"),
        ("bool inside typed array", {"n": 1, "s": "x", "arr": [True]}, "boolean"),
        ("missing required key", {"n": 1}, "required"),
        (
            "nested type violation",
            {"n": 1, "s": "x", "nested": {"deep": "no"}},
            "nested.deep",
        ),
        (
            "nested missing required",
            {"n": 1, "s": "x", "nested": {}},
            "required",
        ),
        ("array item violation", {"n": 1, "s": "x", "arr": ["no"]}, "arr[0]"),
        ("enum violation", {"n": 1, "s": "x", "sev": "medium"}, "not one of"),
        ("top-level wrong type", [], "expected object"),
    ],
)
def test_malformed_is_rejected(label, obj, expect_in_message) -> None:
    error = _walk_schema(obj, SCHEMA, path="$")
    assert error is not None, f"{label} was accepted"
    assert expect_in_message in error, f"{label}: unhelpful message {error!r}"


def test_undeclared_keys_allowed_unless_forbidden() -> None:
    """Silence on `additionalProperties` means permitted, per JSON Schema.

    Rejecting here would make the fallback STRICTER than `jsonschema`, which
    would fail valid model output whenever the real library is absent.
    """
    obj = {"n": 1, "s": "x", "undeclared": 1}
    assert _walk_schema(obj, SCHEMA, path="$") is None

    strict = {**SCHEMA, "additionalProperties": False}
    error = _walk_schema(obj, strict, path="$")
    assert error is not None and "undeclared" in error


def test_unknown_keywords_are_ignored_not_failed() -> None:
    """An unrecognised keyword must not manufacture a rejection."""
    schema = {"type": "object", "properties": {"n": {"type": "integer", "minimum": 5}}}
    # `minimum` is unimplemented; a conforming-typed value must still pass
    # rather than being rejected by a rule this walker cannot evaluate.
    assert _walk_schema({"n": 1}, schema, path="$") is None


def test_error_paths_locate_the_failure() -> None:
    """A bare 'invalid' is not actionable when output is deeply nested."""
    error = _walk_schema({"n": 1, "s": "x", "nested": {"deep": "no"}}, SCHEMA, path="$")
    assert error is not None
    assert error.startswith("$.nested.deep:"), error


def test_public_entry_point_rejects_non_dict() -> None:
    """`_validate_structured` guards the type before either backend runs."""
    assert "expected dict" in (_validate_structured(["not", "a", "dict"], SCHEMA) or "")


def test_agrees_with_jsonschema_where_available() -> None:
    """Differential check against the real implementation.

    Skipped rather than vendored: the point is that the fallback does not
    DIVERGE from `jsonschema`, which can only be checked where both exist.
    """
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(SCHEMA)

    values = [1, True, "x", 1.5, None, [1], ["x"], [True], {"deep": 1}, {"deep": "x"}, {}, "low", "medium"]
    cases = [
        {"n": 1, "s": "x", key: value}
        for key in ("n", "s", "sev", "nested", "arr")
        for value in values
    ]
    cases += [{"n": 1}, {"s": "x"}, {}, {"n": 1, "s": "x"}]

    disagreements = []
    for obj in cases:
        mine = _walk_schema(obj, SCHEMA, path="$")
        try:
            validator.validate(obj)
            reference = None
        except jsonschema.ValidationError as exc:
            reference = str(exc).splitlines()[0]
        if (mine is not None) != (reference is not None):
            disagreements.append((obj, mine, reference))

    assert not disagreements, f"{len(disagreements)} disagreements: {disagreements[:3]}"
