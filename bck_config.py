"""bck_config.py — minimal YAML reader for the backtester.

Objective : ONE shared interface with the bot — `config/standard_config.yaml`.
            Read parameters, selection_filters, circuit_breakers from the same
            file the bot uses. If the YAML is OK for backtest, it's OK for live.
Rational  : isolation goal — backtester does NOT import any bot module. The
            YAML is the contract; everything else is re-implemented.
Isolation : reads ONLY `config/standard_config.yaml`. No bot internals.

This shadows: PM_config_loader.py (read-only subset).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

PROJECT_DIR = Path(__file__).parent.resolve()
DEFAULT_CONFIG = PROJECT_DIR / "config" / "standard_config.yaml"


def load(path: Path | str | None = None) -> dict[str, Any]:
    """Read the YAML config and return raw dict. Path defaults to the bot's
    config so backtest and bot stay aligned by default."""
    p = Path(path) if path else DEFAULT_CONFIG
    if not p.exists():
        raise FileNotFoundError(f"config not found: {p}")
    with p.open() as f:
        cfg = yaml.safe_load(f) or {}
    return cfg


def trading_defaults(cfg: dict | None = None) -> dict[str, float]:
    """Flatten the `parameters` section to {name: value} pairs."""
    cfg = cfg if cfg is not None else load()
    return {k: v.get("value") for k, v in (cfg.get("parameters") or {}).items()}


def selection_filters(cfg: dict | None = None) -> dict[str, dict]:
    """Return the YAML's selection_filters section verbatim — used by bck_filter."""
    cfg = cfg if cfg is not None else load()
    return cfg.get("selection_filters") or {}


def circuit_breaker_config(cfg: dict | None = None) -> dict[str, Any]:
    """Return the circuit_breakers section as {name: value}."""
    cfg = cfg if cfg is not None else load()
    return {k: v.get("value") for k, v in (cfg.get("circuit_breakers") or {}).items()}
