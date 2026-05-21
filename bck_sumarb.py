"""bck_sumarb.py — backtest the YES + NO sum-arbitrage strategy.

Premise:
    In a binary market, YES_token and NO_token are claims on the same dollar.
    At resolution, exactly one pays $1.00 and the other pays $0.00. So
    YES_price + NO_price should equal $1.00 in equilibrium (minus fees).

    If we can BUY 1 YES at $A and 1 NO at $B with A + B < $1, we can MERGE
    them (Polymarket CTF redeem-on-CT operation) to receive $1 collateral —
    locking in (1 - A - B - merge_fee) of risk-free profit per share-pair.

This backtester answers:
    - How often does (YES_ask + NO_ask) drop below the breakeven threshold?
    - What's the total $ available across the 30-day window?
    - Is execution speed even relevant, or are opportunities small/rare?

Fill model (conservative):
    - We see a trade on YES at price P → estimate YES_best_ask ≈ P (taker bought)
    - Same for NO
    - When both estimates exist within `staleness_s` seconds AND their sum
      is below `max_sum` (e.g. 0.99), we simulate buying 1-share-pair at
      those asks and merging immediately.
    - Each opportunity is COUNTED ONCE per refresh cycle to avoid double-
      counting in a thin window of arbitrage.

Isolation : depends only on bck_data/trades.db and bck_data/markets.db
            (read-only) + stdlib. No bot imports.

Run:
    .venv/bin/python bck_sumarb.py
    .venv/bin/python bck_sumarb.py --max-sum 0.98 --merge-fee 0.005
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.resolve()
DATA_DIR = PROJECT_DIR / "bck_data"
TRADES_DB = DATA_DIR / "trades.db"
MARKETS_DB = DATA_DIR / "markets.db"
RESULTS_DIR = PROJECT_DIR / "bck_results"


# ---- sumarb_fn_01: data load ------------------------------------------------
def load_market_tokens() -> dict[str, set[str]]:
    """Return condition_id → set of asset_ids that traded. Inferred from
    trades.db (since markets.db only stores token1 reliably). A healthy
    binary market has exactly 2 token_ids."""
    conn = sqlite3.connect(f"file:{TRADES_DB}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT condition_id, asset_id FROM trades "
        "WHERE asset_id IS NOT NULL AND asset_id != '' "
        "GROUP BY condition_id, asset_id"
    ).fetchall()
    conn.close()
    out: dict[str, set[str]] = defaultdict(set)
    for cid, aid in rows:
        out[cid].add(aid)
    return out


def load_trades_ordered() -> list[tuple]:
    conn = sqlite3.connect(f"file:{TRADES_DB}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT ts, condition_id, asset_id, side, price, size "
        "FROM trades "
        "WHERE asset_id IS NOT NULL "
        "ORDER BY ts ASC"
    ).fetchall()
    conn.close()
    return rows


def load_market_questions() -> dict[str, str]:
    conn = sqlite3.connect(f"file:{MARKETS_DB}?mode=ro", uri=True)
    rows = conn.execute("SELECT condition_id, question FROM markets").fetchall()
    conn.close()
    return {cid: q for cid, q in rows}


# ---- sumarb_fn_02: replay ---------------------------------------------------
def run_sumarb(max_sum: float, merge_fee: float, staleness_s: int,
               min_edge: float) -> dict:
    """For each market, walk trades chronologically. Maintain a per-token
    estimated ask from the most recent observed BUY trade on that token.
    When both legs have a fresh-enough estimate AND sum < max_sum AND edge
    >= min_edge, count an opportunity and accumulate profit."""
    market_tokens = load_market_tokens()
    binary_markets = {cid: tuple(tokens) for cid, tokens in market_tokens.items()
                      if len(tokens) == 2}
    print(f"[sumarb] binary markets: {len(binary_markets)} (of {len(market_tokens)})")

    questions = load_market_questions()
    trades = load_trades_ordered()
    print(f"[sumarb] trades to scan: {len(trades):,}")

    # Per-condition-id state: {asset_id → (latest_ask_estimate, ts)}.
    # We treat a real BUY trade as evidence of the ask at that price.
    state: dict[str, dict[str, tuple[float, int]]] = defaultdict(dict)

    opportunities: list[dict] = []
    last_arb_ts: dict[str, int] = {}  # condition_id → last counted ts (rate-limit per market)

    for ts, cid, asset_id, side, price, size in trades:
        if cid not in binary_markets:
            continue
        s = (side or "").upper()
        if s != "BUY":
            continue  # we only use BUY trades as ask estimates
        state[cid][asset_id] = (float(price), ts)
        legs = state[cid]
        if len(legs) < 2:
            continue
        # Both asset_ids have an estimate. Check freshness + sum.
        (ts_a, ts_b) = (legs[list(legs.keys())[0]][1], legs[list(legs.keys())[1]][1])
        if abs(ts_a - ts_b) > staleness_s:
            continue
        prices = [p for p, _ in legs.values()]
        s_sum = sum(prices)
        if s_sum >= max_sum:
            continue
        edge = 1.0 - s_sum - merge_fee
        if edge < min_edge:
            continue
        # Rate-limit per market: don't count multiple times within staleness_s.
        if cid in last_arb_ts and (ts - last_arb_ts[cid]) < staleness_s:
            continue
        last_arb_ts[cid] = ts
        opportunities.append({
            "ts": ts, "cid": cid, "sum": s_sum, "edge": edge,
            "question": (questions.get(cid) or "")[:60],
        })

    return {
        "n_binary_markets": len(binary_markets),
        "n_trades_scanned": len(trades),
        "n_opportunities": len(opportunities),
        "total_edge_per_share": sum(o["edge"] for o in opportunities),
        "best_edge": max((o["edge"] for o in opportunities), default=0.0),
        "median_edge": sorted([o["edge"] for o in opportunities])[len(opportunities) // 2]
                         if opportunities else 0.0,
        "opportunities": opportunities,
    }


# ---- sumarb_fn_03: CLI ------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Backtest YES+NO sum arbitrage")
    ap.add_argument("--max-sum", type=float, default=0.99,
                    help="trigger arb only when YES_ask + NO_ask < this (default 0.99)")
    ap.add_argument("--merge-fee", type=float, default=0.005,
                    help="estimated $ fee per merge per share-pair (default 0.005)")
    ap.add_argument("--staleness-s", type=int, default=60,
                    help="max age in seconds between the two leg estimates (default 60)")
    ap.add_argument("--min-edge", type=float, default=0.005,
                    help="don't count if edge < this $ per share-pair (default 0.005)")
    args = ap.parse_args()

    if not TRADES_DB.exists():
        print(f"{TRADES_DB} not found — run bck_ingest first", file=sys.stderr)
        return 1

    print(f"[sumarb] params: max_sum={args.max_sum}  merge_fee=${args.merge_fee}  "
          f"staleness={args.staleness_s}s  min_edge=${args.min_edge}")
    print()

    res = run_sumarb(args.max_sum, args.merge_fee, args.staleness_s, args.min_edge)

    n_ops = res["n_opportunities"]
    print()
    print("═════════════════════ SUM-ARB BACKTEST RESULT ═════════════════════")
    print(f"  Binary markets:        {res['n_binary_markets']}")
    print(f"  Trades scanned:        {res['n_trades_scanned']:,}")
    print(f"  Arb opportunities:     {n_ops}")
    if n_ops:
        print(f"  Total edge captured:   ${res['total_edge_per_share']:.4f}  (assumes 1 share-pair per op)")
        print(f"  Median edge / op:      ${res['median_edge']:.4f}")
        print(f"  Best edge / op:        ${res['best_edge']:.4f}")
        print()
        print(f"  Top 10 opportunities:")
        top = sorted(res["opportunities"], key=lambda o: -o["edge"])[:10]
        for o in top:
            t = datetime.fromtimestamp(o["ts"], timezone.utc).strftime("%m-%d %H:%M")
            print(f"    {t}  sum=${o['sum']:.4f}  edge=${o['edge']:.4f}  {o['question']}")
    else:
        print("  No opportunities found at this threshold.")
        print(f"  Try loosening: --max-sum 0.995 --min-edge 0.001")
    print("════════════════════════════════════════════════════════════════════")

    # Persist
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"sumarb_{datetime.now():%Y%m%d_%H%M%S}.csv"
    import csv
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts_utc", "condition_id", "yes+no_sum", "edge_usd", "question"])
        for o in res["opportunities"]:
            w.writerow([
                datetime.fromtimestamp(o["ts"], timezone.utc).isoformat(),
                o["cid"], f'{o["sum"]:.4f}', f'{o["edge"]:.4f}', o["question"],
            ])
    print(f"  saved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
