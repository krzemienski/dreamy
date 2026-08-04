"""Validated dreamy configuration.

Persisted as JSON (``config.json``). TOML is accepted read-only so a
version-controlled ``config/dreamy.example.toml`` is a real, loadable file
rather than documentation prose: ``tomllib`` is stdlib on the supported
Python floor (>=3.11), so this costs no dependency. Writes stay JSON —
one on-disk write format, no round-trip ambiguity.
"""
from __future__ import annotations

import json
import os
import tempfile
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_CONFIG_DIR = "~/.local/share/dreamy"


def resolve_path(p: str) -> Path:
    """Expand ~ and return a Path."""
    return Path(p).expanduser()


def config_path(dir: str | None = None) -> Path:
    """Return the path to config.json inside the given (or default) directory."""
    base = resolve_path(dir or DEFAULT_CONFIG_DIR)
    return base / "config.json"


@dataclass
class Config:
    interval_seconds: int = 21600
    lookback_days: int = 30
    output_dir: str = DEFAULT_CONFIG_DIR
    # DESIGN.md §3.3 — T2 matches within ±N s, "N default 30s, configurable".
    # It was not configurable: run.py called correlate_records() with no window,
    # so the function default (then 120s) was the shipping value and no config
    # key could change it.
    correlation_window_seconds: int = 30
    analysis_workers: int = 4
    spend_cap_usd: float | None = None  # agent analysis disabled until positive cap is explicit
    spend_warn_usd: float | None = None  # soft warn only
    # I3: `spend_cap_usd` bounds ONE run only. At N scheduled runs/day that
    # composes into an unbounded cumulative total — nothing summed spend
    # ACROSS runs, so a $5 per-run cap with 4 runs/day permits ~$20/day with
    # no component able to observe or enforce the total. These two are
    # checked pre-flight in `run_pipeline` against `spend_ledger` (trailing
    # 24h / 30d), the same conservative posture as `spend_cap_usd`: null
    # means disabled, never "unlimited by accident".
    spend_cap_daily_usd: float | None = None
    spend_cap_monthly_usd: float | None = None
    retention_days: int = 90
    include_projects: list[str] = field(default_factory=list)
    exclude_projects: list[str] = field(default_factory=list)
    goals_paths: list[str] = field(default_factory=list)
    agent_model: str = "cc/claude-opus-5"
    ninerouter_base_url: str = "http://localhost:20128"
    claude_cli_path: str | None = None
    ninerouter_api_key_env: str = "NINEROUTER_API_KEY"
    agents_enabled: dict[str, bool] = field(default_factory=lambda: {
        "research": False, "teacher": False, "friends": False, "compiler": True,
    })
    log_level: str = "DEBUG"
    log_topics: list[str] = field(
        default_factory=lambda: [
            "ingest", "correlate", "analyze", "agent", "research",
            "teacher", "friends", "compiler", "report", "schedule",
        ]
    )
    emit_mode: str = "inline"  # inline|native
    skill_pin: bool = True
    source_paths: dict[str, str] = field(default_factory=dict)  # optional overrides per SOURCE_ID
    # `cli._chain_skill_names` has always read `cfg.prompt_chains` via
    # `getattr(cfg, "prompt_chains", None)`. No such field existed, so the
    # call returned None on every invocation and the override merge was
    # dead code: chains were documented as configurable but were not.
    # Declaring the field is what makes that line reachable.
    prompt_chains: dict[str, list[str]] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)

    def validate(self) -> list[str]:
        """Return a list of human-readable validation errors, empty if valid."""
        errors: list[str] = []
        # N-03: a range check alone is not a type check, and `bool` subclasses
        # `int`. `{"interval_seconds": true}` therefore passed `> 0`, reached
        # the plist as `int(True)`, and scheduled a launchd job to fire EVERY
        # SECOND. `3600.7` likewise passed and silently truncated to 3600.
        # `type(v) is int` rejects both -- `isinstance` would readmit bool.
        #
        # The type check gates the range check via elif, so a str never reaches
        # `<= 0` and raises TypeError instead of collecting a clean error.
        for name in (
            "interval_seconds",
            "lookback_days",
            "correlation_window_seconds",
            "analysis_workers",
            "retention_days",
        ):
            value = getattr(self, name)
            if type(value) is not int:
                errors.append(
                    f"{name} must be an integer, got {type(value).__name__}"
                )
            elif value <= 0:
                errors.append(f"{name} must be > 0")
        if self.spend_cap_usd is not None and self.spend_cap_usd <= 0:
            errors.append("spend_cap_usd must be positive or null (null disables agent analysis)")
        if self.spend_warn_usd is not None and self.spend_warn_usd < 0:
            errors.append("spend_warn_usd must be >= 0 or None")
        if self.spend_cap_daily_usd is not None and self.spend_cap_daily_usd <= 0:
            errors.append(
                "spend_cap_daily_usd must be positive or null (null disables the daily cumulative cap)"
            )
        if self.spend_cap_monthly_usd is not None and self.spend_cap_monthly_usd <= 0:
            errors.append(
                "spend_cap_monthly_usd must be positive or null (null disables the monthly cumulative cap)"
            )
        if self.emit_mode not in ("inline", "native"):
            errors.append("emit_mode must be 'inline' or 'native'")
        if not isinstance(self.agents_enabled, dict):
            errors.append("agents_enabled must be a dict")
        # A malformed chain silently yields zero skills to pin rather than
        # failing, so the shape is checked at the trust boundary instead.
        if not isinstance(self.prompt_chains, dict):
            errors.append("prompt_chains must be a table of name -> list of skill names")
        else:
            for name, members in self.prompt_chains.items():
                if not isinstance(members, list) or not all(
                    isinstance(m, str) for m in members
                ):
                    errors.append(
                        f"prompt_chains['{name}'] must be a list of skill-name strings"
                    )
        return errors


def _read_raw(p: Path) -> dict:
    """Parse a config file by suffix. TOML is read-only; JSON is the write format."""
    try:
        if p.suffix == ".toml":
            with p.open("rb") as fh:
                return tomllib.load(fh)
        return json.loads(p.read_text())
    except (json.JSONDecodeError, tomllib.TOMLDecodeError, OSError) as exc:
        raise ValueError(f"invalid dreamy config {p}: {exc}") from exc


def load_config(path: str | None = None) -> Config:
    """Load config from path (or default location). Missing file returns defaults."""
    p = config_path() if path is None else resolve_path(path)
    if not p.exists():
        return Config()
    raw = _read_raw(p)
    if not isinstance(raw, dict):
        raise ValueError(f"invalid dreamy config {p}: top level must be a table")
    known_fields = {f for f in Config.__dataclass_fields__}
    filtered = {k: v for k, v in raw.items() if k in known_fields}
    cfg = Config(**filtered)
    errors = cfg.validate()
    if errors:
        raise ValueError("invalid dreamy config: " + "; ".join(errors))
    return cfg


def save_config(cfg: Config, path: str | None = None) -> Path:
    """Atomically write cfg to path (or default location). Never writes secrets."""
    errors = cfg.validate()
    if errors:
        raise ValueError("invalid dreamy config: " + "; ".join(errors))
    p = config_path() if path is None else resolve_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = cfg.as_dict()
    fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp_name, p)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return p


# `is_spend_unlimited()` was removed 2026-07-31. It had zero call sites and
# unconditionally returned False, so its only possible effect was to mislead:
# the name asks a question the body never actually evaluates. Spend is capped
# per run by `agent_sdk._sum_run_cost` (WHERE run_id=?); there is no unlimited
# mode to interrogate.
