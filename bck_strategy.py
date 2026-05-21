"""bck_strategy.py — entry/TP/SL/risk-off logic, isolated re-impl of the bot.

Objective : take the same `parameters` block from the YAML the live bot uses,
            and produce the same intent at each tick: do we want to quote, and
            at what price/size?
Rational  : isolation contract. Strategy LOGIC is owned independently by
            backtester and bot — only YAML values are shared.
Isolation : depends only on bck_config (YAML reader). Pure functions, no I/O.

This shadows: trading.py's per-token quoting logic (entry gates + TP + SL +
risk-off). Order of checks matches the live bot's perform_trade() loop.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ---- strategy_dc_01: dataclass for a paper position ------------------------
@dataclass
class Position:
    size: float = 0.0          # shares (positive = long)
    avg_price: float = 0.0     # weighted-average cost in USDC/share
    opened_ts: int = 0
    last_stop_ts: int = 0      # for risk-off pause computation
    last_stop_release_ts: int = 0  # when we can re-enter after stop


@dataclass
class Intent:
    """What the strategy WANTS to do this tick. None means 'do nothing'."""
    action: str                # 'BUY' or 'SELL'
    price: float
    size: float                # shares
    reason: str                # for tracing


# ---- strategy_fn_01: clip price to bot's global bounds ---------------------
def _within_price_bounds(price: float, params: dict) -> bool:
    lo = float(params.get("min_order_price", 0.05))
    hi = float(params.get("max_order_price", 0.95))
    return lo <= price <= hi


# ---- strategy_fn_02: compute quote intent for one token --------------------
def quote_intent(
    pos: Position,
    best_bid: Optional[float],
    best_ask: Optional[float],
    market_min_size_shares: float,
    tick_size: float,
    params: dict,
    now_ts: int,
    can_buy: bool = True,
) -> Optional[Intent]:
    """Decide what the bot wants to quote for THIS token at THIS tick.

    Mirror of trading.py's per-token block, simplified for backtest:
      - if no position and can_buy: post a BUY one tick above best_bid
        (must respect price bounds + min-share constraint)
      - if position > 0: post a SELL at avgPrice * (1 + tp%) or above ask
      - if in risk-off pause: nothing
    """
    if best_bid is None or best_ask is None:
        return None
    if best_ask <= best_bid:
        return None  # crossed/locked book; skip
    trade_size = float(params.get("trade_size", 10))  # shares
    max_size = float(params.get("max_size", 10))      # shares (per-market cap)

    # Risk-off pause (after a stop-loss): wait sleep_period hours before re-entering.
    sleep_h = float(params.get("sleep_period", 24))
    if pos.size <= 1e-9 and pos.last_stop_release_ts > now_ts:
        return None

    # ----- SELL branch (we have inventory) -----
    if pos.size > 1e-9:
        # 1) Stop-loss exit: if position is down >= stop_loss_threshold AND the
        #    book is still tight, post a MARKETABLE sell at best_bid so it fills
        #    immediately. Matches the live bot's stop-out path.
        sl_thresh = float(params.get("stop_loss_threshold", -0.1))   # negative ratio
        spread_thresh = float(params.get("spread_threshold", 0.05))
        if pos.avg_price > 0:
            pnl_pct = (best_bid - pos.avg_price) / pos.avg_price
            spread = best_ask - best_bid
            if pnl_pct <= sl_thresh and spread <= spread_thresh:
                # marketable: sell at the bid (we hit the bid as a taker)
                exit_price = best_bid
                exit_price = round(exit_price / tick_size) * tick_size
                if not _within_price_bounds(exit_price, params):
                    return None
                return Intent("SELL", exit_price, pos.size, reason="stop_loss_exit")

        # 2) Normal take-profit: post at max(tp_price, best_ask).
        tp_pct = float(params.get("take_profit_threshold", 2.0))
        tp_price = pos.avg_price * (1.0 + tp_pct / 100.0)
        target = max(tp_price, best_ask)
        target = round(target / tick_size) * tick_size
        if not _within_price_bounds(target, params):
            return None
        if pos.size < market_min_size_shares:
            return None  # can't post sub-min resting order
        return Intent("SELL", target, pos.size, reason="take_profit_or_ask")

    # ----- BUY branch (entering) -----
    if not can_buy:
        return None
    # Place bid at best_bid + 1 tick (top-of-book join with improvement).
    target = best_bid + tick_size
    target = round(target / tick_size) * tick_size
    if not _within_price_bounds(target, params):
        return None
    size = min(trade_size, max_size - pos.size)
    if size < market_min_size_shares:
        return None
    return Intent("BUY", target, size, reason="entry")


# ---- strategy_fn_03: apply a fill to a Position ----------------------------
def apply_buy_fill(pos: Position, price: float, size: float, ts: int) -> None:
    """Mutate `pos` in place: weighted-avg cost on buys."""
    new_size = pos.size + size
    if new_size > 1e-9:
        pos.avg_price = (pos.avg_price * pos.size + price * size) / new_size
    pos.size = new_size
    if pos.opened_ts == 0:
        pos.opened_ts = ts


def apply_sell_fill(pos: Position, price: float, size: float, ts: int,
                    params: dict) -> tuple[float, bool]:
    """Mutate `pos` in place. Returns (realized_pnl, was_stop_loss).

    Stop-loss flag is True iff the fill closed at a loss > stop_loss_threshold
    relative to average cost. Used by circuit-breaker bookkeeping.
    """
    pnl = (price - pos.avg_price) * size
    pos.size -= size
    if pos.size < 1e-9:
        pos.size = 0.0

    sl_thresh = float(params.get("stop_loss_threshold", -0.1))  # negative ratio
    is_sl = (pnl < 0) and (
        pnl / max(pos.avg_price * size, 1e-9) <= sl_thresh
    )

    if is_sl:
        pos.last_stop_ts = ts
        sleep_h = float(params.get("sleep_period", 24))
        pos.last_stop_release_ts = ts + int(sleep_h * 3600)
        # Wipe avg_price when we exit fully so re-entry isn't biased by old cost.
        if pos.size == 0:
            pos.avg_price = 0.0
    elif pos.size == 0:
        pos.avg_price = 0.0
    return pnl, is_sl
