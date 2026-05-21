"""bck_filter.py — apply selection_filters from the YAML to a market list.

Objective : at any point in time during a backtest, decide whether a market
            is in the bot's "eligible universe" per the same YAML the live bot
            uses. Re-implements PM_filter_markets.compute_survivors() so the
            backtester does not import the bot module.
Rational  : isolation contract — only the YAML is shared. Filter LOGIC is owned
            independently by both sides.
Isolation : reads only the YAML (via bck_config). Pure function.

This shadows: PM_filter_markets.compute_survivors().
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

OPS = {
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    ">":  lambda a, b: a > b,
    "<":  lambda a, b: a < b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def _derive_hours_to_resolution(end_iso: str | None, ref_ts: int) -> float | None:
    """Compute hours from `ref_ts` (unix seconds) to ISO end-date string."""
    if not end_iso:
        return None
    try:
        dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt.timestamp() - ref_ts) / 3600.0
    except Exception:
        return None


def market_passes(market: dict[str, Any],
                  filters: dict[str, dict],
                  ref_ts: int | None = None) -> tuple[bool, list[str]]:
    """Return (passes, list_of_failed_rules) for a single market dict.

    `market` should have keys matching the YAML's filter `column` fields:
    e.g. gm_reward_per_100, volatility_sum, 3_hour, spread, best_bid, best_ask,
    min_size, bid_reward_per_100, ask_reward_per_100, volume_24hr, end_date_iso.
    Mirrors compute_survivors() but operates on a single market.
    """
    if not filters:
        return True, []
    ref_ts = ref_ts if ref_ts is not None else int(datetime.now(timezone.utc).timestamp())

    # Derive hours_to_resolution at evaluation time so YAML can target it.
    if "end_date_iso" in market and "hours_to_resolution" not in market:
        market = dict(market)  # don't mutate caller
        market["hours_to_resolution"] = _derive_hours_to_resolution(
            market["end_date_iso"], ref_ts)

    failed: list[str] = []
    for name, rule in filters.items():
        col = rule.get("column")
        op_str = rule.get("op")
        value = rule.get("value")
        if col is None or op_str is None or value is None:
            continue
        if col not in market:
            failed.append(f"{name}: column '{col}' missing")
            continue
        op = OPS.get(op_str)
        if op is None:
            continue
        actual = market[col]
        try:
            actual_num = float(actual) if actual is not None else None
        except (TypeError, ValueError):
            failed.append(f"{name}: non-numeric value")
            continue
        if actual_num is None:
            failed.append(f"{name}: null value")
            continue
        if not op(actual_num, float(value)):
            failed.append(f"{name}({actual_num} {op_str} {value})")

    return len(failed) == 0, failed
