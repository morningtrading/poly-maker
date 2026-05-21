"""bck_fill_model.py — match our resting paper orders against real trades.

Objective : for each real trade in history, decide whether one of our paper
            quotes would have been filled by that trade.
Rational  : the live PM_paper_fills uses an orderbook-drift heuristic that
            rarely fires for our quoting style. Here we have the actual trade
            stream, so we can use the *correct* matching rule: a real taker
            crossing our resting limit price is a fill.
Isolation : pure function. Depends on nothing.

Polymarket trade convention (verified empirically + via PMSCAN):
  - trade.side = "BUY"  → a real taker BOUGHT (hit the ask). If we had a
                          resting SELL at price <= trade.price, our sell fills.
  - trade.side = "SELL" → a real taker SOLD  (hit the bid). If we had a
                          resting BUY  at price >= trade.price, our buy fills.

This shadows: PM_paper_fills.check_fills() (replaces it with trade-driven matching).
"""
from __future__ import annotations

from typing import Optional


def fill_check(paper_bid_price: Optional[float],
               paper_ask_price: Optional[float],
               trade_side: str,
               trade_price: float) -> Optional[str]:
    """Return 'BUY' if our paper bid fills, 'SELL' if our paper ask fills,
    None otherwise.

    Tolerant matching: we treat the price boundary inclusively because
    Polymarket time-price priority lets the resting maker get filled at par.
    """
    side = (trade_side or "").upper()
    if side == "SELL" and paper_bid_price is not None and trade_price <= paper_bid_price:
        return "BUY"
    if side == "BUY" and paper_ask_price is not None and trade_price >= paper_ask_price:
        return "SELL"
    return None


def estimate_book(recent_trades: list[dict], window_s: int = 120) -> tuple[Optional[float], Optional[float]]:
    """Crude best_bid/best_ask estimate from recent trades.

    Without orderbook snapshots, we approximate using the most recent trades
    within `window_s` seconds. Last BUY trade ≈ best_ask, last SELL ≈ best_bid.
    Returns (best_bid, best_ask) or (None, None) if not enough data.

    Caveats acknowledged:
      - assumes minimal time has passed between trade and our quoting decision
      - in thin markets these estimates can be stale; downstream should treat
        them as approximate and skip quoting if spread looks crazy
    """
    if not recent_trades:
        return None, None
    last_ts = recent_trades[-1].get("ts", 0)
    cutoff = last_ts - window_s
    bids: list[float] = []
    asks: list[float] = []
    for t in reversed(recent_trades):
        if t.get("ts", 0) < cutoff:
            break
        side = (t.get("side") or "").upper()
        price = float(t.get("price") or 0)
        if side == "SELL":
            bids.append(price)
        elif side == "BUY":
            asks.append(price)
    best_bid = max(bids) if bids else None
    best_ask = min(asks) if asks else None
    # If the estimated spread inverts (bid > ask), the market clearly moved;
    # treat both as unknown to avoid bad fills.
    if best_bid is not None and best_ask is not None and best_bid >= best_ask:
        return None, None
    return best_bid, best_ask
