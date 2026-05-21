"""bck_circuit.py — circuit-breaker state machine for the backtester.

Objective : enforce the same risk caps the live bot does — total exposure cap,
            daily stop-loss caps, per-market 24h stop-loss pause. Pure
            in-memory state, never touches the live bot's circuit_state.json.
Isolation : depends only on bck_config (YAML reader).

This shadows: PM_circuit_breakers.py (in-memory equivalent).
"""
from __future__ import annotations

from collections import defaultdict


class CircuitBreaker:
    """Tracks daily counters + per-market 24h windows. Single-process, in-memory.

    Daily counters reset when `roll_day()` is called with a new UTC date
    (caller decides when). Per-market events expire after 24h via prune().
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.enabled = cfg.get("enabled", True)
        self.daily_max_count = cfg.get("daily_max_stop_loss_count", 999999)
        self.daily_max_loss = cfg.get("daily_max_stop_loss_loss_usd", 1e9)
        self.per_market_max_24h = cfg.get("per_market_max_stop_loss_24h", 999999)
        self.max_total_exposure = cfg.get("max_total_exposure_usd", 1e9)
        self.daily_min_realized_pnl = cfg.get("daily_min_realized_pnl_usd", -1e9)

        # Mutable state
        self.day_key: str | None = None
        self.stop_loss_count_today = 0
        self.stop_loss_loss_today = 0.0
        self.realized_pnl_today = 0.0
        self.per_market_24h: dict[str, list[tuple[int, float]]] = defaultdict(list)
        # market_key -> [(ts, loss_usd), ...]  pruned to last 24h on read

    def roll_day(self, new_day_key: str) -> None:
        """Reset daily counters when crossing UTC midnight (caller-controlled)."""
        if self.day_key != new_day_key:
            self.day_key = new_day_key
            self.stop_loss_count_today = 0
            self.stop_loss_loss_today = 0.0
            self.realized_pnl_today = 0.0

    def record_realized(self, market_key: str, amount_usd: float, ts: int) -> None:
        """Track every realized PnL event. Negative = loss."""
        if not self.enabled:
            return
        self.realized_pnl_today += amount_usd
        if amount_usd < -0.001:  # treat as stop-loss
            self.stop_loss_count_today += 1
            self.stop_loss_loss_today += -amount_usd
            self.per_market_24h[market_key].append((ts, -amount_usd))

    def _prune_per_market(self, market_key: str, now_ts: int) -> None:
        cutoff = now_ts - 86400
        events = [e for e in self.per_market_24h.get(market_key, []) if e[0] >= cutoff]
        self.per_market_24h[market_key] = events

    def should_block_buy(self, market_key: str, now_ts: int,
                          current_total_exposure_usd: float) -> tuple[bool, str]:
        """Mirror of PM_circuit_breakers.should_block_buy(). Returns (blocked, reason)."""
        if not self.enabled:
            return False, ""
        if self.stop_loss_count_today >= self.daily_max_count:
            return True, f"daily stop-loss count {self.stop_loss_count_today} >= cap {self.daily_max_count}"
        if self.stop_loss_loss_today >= self.daily_max_loss:
            return True, f"daily stop-loss loss ${self.stop_loss_loss_today:.2f} >= cap ${self.daily_max_loss}"
        if self.realized_pnl_today <= self.daily_min_realized_pnl:
            return True, f"daily realized PnL ${self.realized_pnl_today:.2f} <= floor ${self.daily_min_realized_pnl}"
        if current_total_exposure_usd >= self.max_total_exposure:
            return True, f"total exposure ${current_total_exposure_usd:.2f} >= cap ${self.max_total_exposure}"
        self._prune_per_market(market_key, now_ts)
        recent = len(self.per_market_24h.get(market_key, []))
        if recent >= self.per_market_max_24h:
            return True, f"per-market 24h stop count {recent} >= cap {self.per_market_max_24h}"
        return False, ""
