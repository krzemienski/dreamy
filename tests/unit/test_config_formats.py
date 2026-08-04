"""TOML config loading and prompt-chain overrides.

Two defects motivate these tests.

1. The spec requires a version-controlled ``config/dreamy.example.toml``, but
   ``load_config`` only parsed JSON. An example file the loader cannot read is
   decoration, so TOML is now accepted on read.

2. ``cli._chain_skill_names`` has always merged ``cfg.prompt_chains`` via
   ``getattr(cfg, "prompt_chains", None) or {}``. No such field existed on
   ``Config``, so the expression evaluated to ``None`` on every call and the
   merge was unreachable. ``test_chain_override_reaches_compiler`` fails
   against the pre-fix dataclass, which is what makes it a regression test
   rather than a restatement of the implementation.
"""

from __future__ import annotations

import json

import pytest

from dreamy.cli import _chain_skill_names
from dreamy.config import Config, load_config


def _write(tmp_path, name: str, text: str):
    p = tmp_path / name
    p.write_text(text)
    return str(p)


def test_toml_config_loads(tmp_path):
    p = _write(tmp_path, "dreamy.toml", "interval_seconds = 3600\nlookback_days = 14\n")
    cfg = load_config(p)
    assert (cfg.interval_seconds, cfg.lookback_days) == (3600, 14)


def test_json_config_still_loads(tmp_path):
    """TOML support must not regress the shipping write format."""
    p = _write(tmp_path, "config.json", json.dumps({"interval_seconds": 7200}))
    assert load_config(p).interval_seconds == 7200


def test_toml_validation_rejects_bool(tmp_path):
    """Boundary checks apply to TOML, not only JSON."""
    p = _write(tmp_path, "dreamy.toml", "interval_seconds = true\n")
    with pytest.raises(ValueError, match="interval_seconds must be an integer"):
        load_config(p)


def test_malformed_toml_rejected(tmp_path):
    p = _write(tmp_path, "dreamy.toml", "interval_seconds = = 3\n")
    with pytest.raises(ValueError, match="invalid dreamy config"):
        load_config(p)


def test_unknown_keys_ignored(tmp_path):
    """Forward compatibility: an unknown key must not crash an older build."""
    p = _write(tmp_path, "dreamy.toml", 'interval_seconds = 60\nfuture_key = "x"\n')
    assert load_config(p).interval_seconds == 60


def test_shipped_example_toml_is_loadable():
    """The version-controlled example must satisfy the real validator.

    This is the check that keeps ``config/dreamy.example.toml`` honest: if the
    example drifts from the dataclass it fails here rather than on a user's
    first run.
    """
    from pathlib import Path

    example = Path(__file__).resolve().parents[2] / "config" / "dreamy.example.toml"
    assert example.exists(), f"missing shipped example: {example}"
    cfg = load_config(str(example))
    assert cfg.validate() == []


def test_chain_override_reaches_compiler(tmp_path):
    """The override must change what the compiler will pin.

    Asserting only that the field parses would hold even while the merge stayed
    dead, so this asserts the downstream effect and pins the baseline: the
    custom skill must be absent without the override and present with it.
    """
    assert "my-custom-skill" not in _chain_skill_names(Config())

    p = _write(
        tmp_path,
        "dreamy.toml",
        '[prompt_chains]\nresumption = ["task-architect", "my-custom-skill"]\n',
    )
    cfg = load_config(p)
    names = _chain_skill_names(cfg)
    assert "my-custom-skill" in names
    # Overriding one chain must not drop the defaults of the others.
    assert "functional-validation" in names


def test_malformed_chain_rejected(tmp_path):
    """A bad chain shape yields zero pinned skills silently; fail at the boundary."""
    p = _write(tmp_path, "dreamy.toml", '[prompt_chains]\nresumption = "not-a-list"\n')
    with pytest.raises(ValueError, match="must be a list of skill-name strings"):
        load_config(p)


def test_shipped_prompt_chains_example_is_loadable():
    """The chains example must be a real config, not documentation prose.

    It is shipped as a standalone file usable via `dreamy --config`, so it has
    to satisfy the production validator and actually change what the compiler
    would pin.
    """
    from pathlib import Path

    example = (
        Path(__file__).resolve().parents[2] / "config" / "prompt-chains.example.toml"
    )
    assert example.exists(), f"missing shipped example: {example}"

    cfg = load_config(str(example))
    assert cfg.validate() == []
    names = _chain_skill_names(cfg)
    # The example demonstrates extending a default rather than replacing it,
    # so both the appended skill and the untouched chains must be present.
    assert "my-team-conventions" in names
    assert "functional-validation" in names
