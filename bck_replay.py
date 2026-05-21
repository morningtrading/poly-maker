"""bck_replay.py — event-driven replay over historical Polymarket trades.

Objective : feed real trades (from bck_data/trades.db) into the bot's
            strategy/circuit/filter logic in chronological order, simulate
            fills, and accumulate realized PnL.
Rational  : the whole point of the backtester.
Isolation : depends only on bck_config, bck_filter, bck_strategy, bck_circuit,
            bck_fill_model. Imports NOTHING from poly_data / PM_* / global_state.

Pipeline:
    1. Load trades + market metadata from bck_data/*.db
    2. Apply selection_filters per current YAML to determine which markets
       are in the tradeable universe (filter applied per-market once for now;
       future improvement: re-filter at every YAML-style cadence)
    3. Walk the global merged trade stream in time order:
         a. update best_bid/best_ask estimate from recent trades on this token
         b. check fill against our paper resting order
         c. on fill, update position + paper PnL + circuit-breaker counters
         d. recompute the strategy's quote intent for this token
            (replaces any prior resting order — matches the bot's cancel/post pattern)
    4. Emit summary stats + per-market PnL table + fills CSV.

Test:
    .venv/bin/python bck_replay.py
    .venv/bin/python bck_replay.py --start 2026-05-01 --end 2026-05-21
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import bck_config
import bck_filter
import bck_strategy as strat
from bck_circuit import CircuitBreaker
from bck_fill_model import estimate_book, fill_check

PROJECT_DIR = Path(__file__).parent.resolve()
DATA_DIR = PROJECT_DIR / "bck_data"
TRADES_DB = DATA_DIR / "trades.db"
MARKETS_DB = DATA_DIR / "markets.db"
RESULTS_DIR = PROJECT_DIR / "bck_results"

# Window of recent trades kept per-token for book estimation
RECENT_TRADE_WINDOW = 50


# ---- replay_fn_01: load market metadata ------------------------------------
def load_markets() -> dict[str, dict]:
    """Return condition_id → market dict, with keys the filter expects."""
    if not MARKETS_DB.exists():
        raise FileNotFoundError(f"{MARKETS_DB} not found — run bck_ingest first")
    conn = sqlite3.connect(f"file:{MARKETS_DB}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT condition_id, question, token1, token2, tick_size, "
        "       min_order_size, neg_risk, end_date_iso, volume_24hr "
        "FROM markets"
    ).fetchall()
    conn.close()
    out: dict[str, dict] = {}
    for cid, q, t1, t2, tick, mos, nr, end_iso, vol in rows:
        out[cid] = {
            "condition_id": cid, "question": q or "",
            "token1": t1, "token2": t2,
            "tick_size": float(tick or 0.01),
            "min_order_size": float(mos or 5),
            "neg_risk": bool(nr),
            "end_date_iso": end_iso,
            "volume_24hr": float(vol or 0),
        }
    return out


# ---- replay_fn_02: load trades in time order -------------------------------
def load_trades(start_ts: Optional[int] = None,
                end_ts: Optional[int] = None,
                limit: Optional[int] = None) -> list[tuple]:
    """Return all trades sorted by ts ascending. Optionally bound by time."""
    if not TRADES_DB.exists():
        raise FileNotFoundError(f"{TRADES_DB} not found — run bck_ingest first")
    conn = sqlite3.connect(f"file:{TRADES_DB}?mode=ro", uri=True)
    where = []
    args: list = []
    if start_ts is not None:
        where.append("ts >= ?")
        args.append(start_ts)
    if end_ts is not None:
        where.append("ts <= ?")
        args.append(end_ts)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    limit_sql = f" LIMIT {int(limit)}" if limit else ""
    sql = ("SELECT ts, condition_id, asset_id, outcome, side, price, size "
           "FROM trades" + where_sql + " ORDER BY ts ASC" + limit_sql)
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows


# ---- replay_fn_03: pre-filter the universe ---------------------------------
def filter_universe(markets: dict[str, dict], yaml_cfg: dict,
                    ref_ts: int) -> set[str]:
    """Apply selection_filters from YAML to mark which condition_ids are tradeable.

    Note: this is a snapshot filter — we apply it once with `ref_ts`. A more
    realistic backtest would re-filter every N hours, but for v1 a snapshot is
    enough to validate the approach.
    """
    filters = bck_config.selection_filters(yaml_cfg)
    survivors: set[str] = set()
    for cid, m in markets.items():
        # bck_filter expects flat key/value access; map our market dict.
        m_for_filter = {
            "gm_reward_per_100":   m.get("gm_reward_per_100", 1.0),
            "volatility_sum":      m.get("volatility_sum", 0),
            "3_hour":              m.get("3_hour", 0),
            "spread":              m.get("spread", 0),
            "best_bid":            m.get("best_bid", 0.5),
            "best_ask":            m.get("best_ask", 0.5),
            "min_size":            m.get("min_size", 0),
            "bid_reward_per_100":  m.get("bid_reward_per_100", 1.0),
            "ask_reward_per_100":  m.get("ask_reward_per_100", 1.0),
            "volume_24hr":         m["volume_24hr"],
            "end_date_iso":        m["end_date_iso"],
        }
        ok, _failed = bck_filter.market_passes(m_for_filter, filters, ref_ts)
        if ok:
            survivors.add(cid)
    return survivors


# ---- replay_fn_04: the main event loop -------------------------------------
def run_replay(yaml_path: Optional[str] = None,
               yaml_cfg: Optional[dict] = None,
               start_ts: Optional[int] = None,
               end_ts: Optional[int] = None,
               quiet: bool = False) -> dict:
    """Walk all trades chronologically, simulate paper fills, return stats.

    Provide EITHER `yaml_path` (file path) OR `yaml_cfg` (already-parsed dict).
    `yaml_cfg` is useful for parameter sweeps where the caller mutates the
    config in memory across runs.
    """
    cfg = yaml_cfg if yaml_cfg is not None else bck_config.load(yaml_path)
    params = bck_config.trading_defaults(cfg)
    breaker = CircuitBreaker(bck_config.circuit_breaker_config(cfg))

    markets = load_markets()
    trades = load_trades(start_ts=start_ts, end_ts=end_ts)
    if not trades:
        return {"error": "no trades in selected window"}

    # Filter universe using the LATEST trade's timestamp as the snapshot.
    # WHY latest, not first: hours_to_resolution is computed from end_date_iso
    # minus ref_ts; using the first trade (which is up to 30 days ago) would
    # reject every market whose endDate is too far in the FUTURE-from-then.
    # The intent of the filter is "would these markets be eligible NOW?" — so
    # snapshot at the end of the window. (A proper time-varying filter is a
    # future improvement; for v1 a snapshot is good enough.)
    ref_ts = trades[-1][0]  # last ts
    eligible_cids = filter_universe(markets, cfg, ref_ts)
    if not quiet:
        print(f"[bck_replay] {len(eligible_cids)} of {len(markets)} markets pass filters")

    # Per-token state: positions, paper orders, recent-trade window for book est.
    positions: dict[str, strat.Position] = defaultdict(strat.Position)
    paper_bid: dict[str, Optional[tuple[float, float]]] = defaultdict(lambda: None)  # (price, size)
    paper_ask: dict[str, Optional[tuple[float, float]]] = defaultdict(lambda: None)
    recent: dict[str, deque] = defaultdict(lambda: deque(maxlen=RECENT_TRADE_WINDOW))

    fills: list[dict] = []
    total_exposure_usd = 0.0
    current_day_key = None

    skipped_filter = skipped_thin = 0
    progress_step = max(1, len(trades) // 20)

    for i, (ts, cid, asset_id, outcome, side, price, size) in enumerate(trades):
        if not quiet and i % progress_step == 0 and i > 0:
            print(f"[bck_replay] ...{i}/{len(trades)} trades processed", flush=True)
        # Skip markets not in eligible universe
        if cid not in eligible_cids:
            skipped_filter += 1
            continue
        m = markets.get(cid)
        if not m:
            continue
        token = str(asset_id) if asset_id else None
        if not token:
            continue
        tick = m["tick_size"] or 0.01
        market_min = m["min_order_size"] or 5

        # Daily rollover (UTC) for circuit breaker
        day_key = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
        if day_key != current_day_key:
            breaker.roll_day(day_key)
            current_day_key = day_key

        # 1) Update recent-trade window BEFORE checking fills.
        recent[token].append({"ts": ts, "side": side, "price": price})

        # 2) Check our resting paper orders against this real trade.
        pos = positions[token]
        bid = paper_bid.get(token)
        ask = paper_ask.get(token)
        bid_price = bid[0] if bid else None
        ask_price = ask[0] if ask else None
        result = fill_check(bid_price, ask_price, side, price)
        if result == "BUY" and bid is not None:
            fill_size = min(bid[1], size)
            fill_price = bid[0]
            strat.apply_buy_fill(pos, fill_price, fill_size, ts)
            total_exposure_usd += fill_size * fill_price
            fills.append({
                "ts": ts, "cid": cid, "asset": token, "side": "BUY",
                "price": fill_price, "size": fill_size, "pnl": 0.0,
                "is_sl": False, "question": m["question"][:60],
            })
            paper_bid[token] = None  # consumed
            if bid[1] > size:  # partial: leave residual on the book
                paper_bid[token] = (bid[0], bid[1] - size)
        elif result == "SELL" and ask is not None:
            fill_size = min(ask[1], size)
            fill_price = ask[0]
            pnl, is_sl = strat.apply_sell_fill(pos, fill_price, fill_size, ts, params)
            # exposure decreases by sold notional at avg cost (not at fill price)
            total_exposure_usd -= fill_size * (pos.avg_price if pos.size > 0 else fill_price)
            total_exposure_usd = max(0.0, total_exposure_usd)
            breaker.record_realized(cid, pnl, ts)
            fills.append({
                "ts": ts, "cid": cid, "asset": token, "side": "SELL",
                "price": fill_price, "size": fill_size, "pnl": pnl,
                "is_sl": is_sl, "question": m["question"][:60],
            })
            paper_ask[token] = None
            if ask[1] > size:
                paper_ask[token] = (ask[0], ask[1] - size)

        # 3) Recompute quote intent for this token.
        best_bid, best_ask = estimate_book(list(recent[token]), window_s=300)
        blocked, _reason = breaker.should_block_buy(cid, ts, total_exposure_usd)
        intent = strat.quote_intent(
            pos=pos,
            best_bid=best_bid, best_ask=best_ask,
            market_min_size_shares=market_min,
            tick_size=tick,
            params=params,
            now_ts=ts,
            can_buy=not blocked,
        )
        if intent is None:
            # Strategy doesn't want to quote — clear any stale orders.
            if pos.size <= 1e-9:
                paper_bid[token] = None
            paper_ask[token] = None if pos.size <= 1e-9 else paper_ask[token]
            continue

        if intent.action == "BUY":
            paper_bid[token] = (intent.price, intent.size)
            paper_ask[token] = None
        else:  # SELL
            paper_ask[token] = (intent.price, intent.size)

    # ---- Summary stats ----
    wins = [f for f in fills if f["side"] == "SELL" and f["pnl"] > 0.005]
    losses = [f for f in fills if f["side"] == "SELL" and f["pnl"] < -0.005]
    realized = sum(f["pnl"] for f in fills if f["side"] == "SELL")
    n_closed = len(wins) + len(losses)
    return {
        "n_trades_input": len(trades),
        "n_trades_skipped_filter": skipped_filter,
        "n_fills": len(fills),
        "n_buy_fills": sum(1 for f in fills if f["side"] == "BUY"),
        "n_sell_fills": sum(1 for f in fills if f["side"] == "SELL"),
        "n_stop_loss": sum(1 for f in fills if f["is_sl"]),
        "realized_pnl": realized,
        "win_rate": (len(wins) / n_closed * 100) if n_closed else 0.0,
        "wins": len(wins),
        "losses": len(losses),
        "ts_first": trades[0][0],
        "ts_last": trades[-1][0],
        "fills_data": fills,  # caller can write CSV
    }


# ---- replay_fn_05: CLI -----------------------------------------------------
def _parse_date(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())


def main() -> int:
    ap = argparse.ArgumentParser(description="Backtest replay engine")
    ap.add_argument("--config", default=None, help="path to YAML (default: bot's)")
    ap.add_argument("--start", default=None, help="ISO date e.g. 2026-05-01")
    ap.add_argument("--end", default=None, help="ISO date e.g. 2026-05-21")
    args = ap.parse_args()

    start_ts = _parse_date(args.start)
    end_ts = _parse_date(args.end)

    print(f"[bck_replay] running…")
    stats = run_replay(yaml_path=args.config, start_ts=start_ts, end_ts=end_ts)
    if "error" in stats:
        print(stats["error"], file=sys.stderr)
        return 1

    # ---- Print summary ----
    t_first = datetime.fromtimestamp(stats["ts_first"], timezone.utc).strftime("%Y-%m-%d %H:%M")
    t_last = datetime.fromtimestamp(stats["ts_last"], timezone.utc).strftime("%Y-%m-%d %H:%M")
    print()
    print(f"═══════════════════════ BACKTEST RESULT ═══════════════════════")
    print(f"  Window:           {t_first} → {t_last} UTC")
    print(f"  Trades scanned:   {stats['n_trades_input']:,}")
    print(f"  Outside filter:   {stats['n_trades_skipped_filter']:,}")
    print(f"  Paper fills:      {stats['n_fills']}  ({stats['n_buy_fills']} BUYs, {stats['n_sell_fills']} SELLs)")
    print(f"  Closed trades:    {stats['wins'] + stats['losses']}  ({stats['wins']}W / {stats['losses']}L)")
    print(f"  Win rate:         {stats['win_rate']:.1f}%")
    print(f"  Stop-losses:      {stats['n_stop_loss']}")
    print(f"  Realized PnL:     ${stats['realized_pnl']:+.4f}")
    print(f"════════════════════════════════════════════════════════════════")

    # ---- Persist fills CSV ----
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_csv = RESULTS_DIR / f"replay_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with out_csv.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts_utc", "condition_id", "asset_id", "side", "price",
                    "size", "pnl", "is_sl", "question"])
        for fr in stats["fills_data"]:
            w.writerow([
                datetime.fromtimestamp(fr["ts"], timezone.utc).isoformat(),
                fr["cid"], fr["asset"], fr["side"],
                f'{fr["price"]:.4f}', f'{fr["size"]:.4f}',
                f'{fr["pnl"]:+.4f}', fr["is_sl"], fr["question"],
            ])
    print(f"  fills written to: {out_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
