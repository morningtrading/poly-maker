"""bck_ingest.py — pull historical trade data from Polymarket public APIs.

Objective : populate bck_data/trades.db with every public trade on every
            selected market over the last N days. Pull-once, replay-many.
Rational  : the offline backtester needs ground-truth trade events to simulate
            fills. Polymarket's data-api/trades endpoint exposes this for free.
Isolation : reads only Polymarket public HTTPS endpoints + reads polymb.db
            for the current selected_markets list (read-only SELECT). Writes
            ONLY to bck_data/.

This shadows: nothing in the bot (the bot doesn't ingest historical trades).

Test:
    python bck_ingest.py --days 30
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

PROJECT_DIR = Path(__file__).parent.resolve()
DATA_DIR = PROJECT_DIR / "bck_data"
TRADES_DB = DATA_DIR / "trades.db"
MARKETS_DB = DATA_DIR / "markets.db"
BOT_DB = PROJECT_DIR / "polymb.db"  # read-only, just for selected_markets list

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

# ---- ingest_fn_01: schema ---------------------------------------------------
TRADES_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id TEXT NOT NULL,
    asset_id     TEXT,         -- token_id of the side traded
    outcome      TEXT,
    side         TEXT,         -- BUY / SELL (the taker side)
    price        REAL NOT NULL,
    size         REAL NOT NULL,
    ts           INTEGER NOT NULL,         -- unix seconds
    tx_hash      TEXT,
    fetched_at   INTEGER NOT NULL,
    UNIQUE(condition_id, ts, tx_hash, price, size, side, asset_id)
);
CREATE INDEX IF NOT EXISTS idx_trades_cond_ts ON trades(condition_id, ts);
CREATE INDEX IF NOT EXISTS idx_trades_asset_ts ON trades(asset_id, ts);
"""

MARKETS_SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    condition_id TEXT PRIMARY KEY,
    question     TEXT,
    slug         TEXT,
    token1       TEXT,
    token2       TEXT,
    answer1      TEXT,
    answer2      TEXT,
    tick_size    REAL,
    min_order_size REAL,
    neg_risk     INTEGER,
    end_date_iso TEXT,
    volume_24hr  REAL,
    snapshot_at  INTEGER NOT NULL
);
"""


# ---- ingest_fn_02: DB helpers -----------------------------------------------
def _open_db(path: Path, schema: str) -> sqlite3.Connection:
    """Open (creating dir + schema as needed). Returns a connection."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(schema)
    conn.commit()
    return conn


def selected_markets() -> list[tuple[str, str, str, str]]:
    """Read (question, token1, token2, condition_id) from the bot's polymb.db.
    READ-ONLY. We never write to polymb.db from the backtester."""
    if not BOT_DB.exists():
        raise FileNotFoundError(f"{BOT_DB} not found — bot's DB needed for market list")
    conn = sqlite3.connect(f"file:{BOT_DB}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT question, token1, token2, condition_id FROM selected_markets"
        ).fetchall()
    finally:
        conn.close()


def tradable_markets(min_vol_24h: float = 50.0,
                     max_markets_scanned: int = 5000) -> list[tuple[str, str, str, str]]:
    """Pull active, accepting-orders markets from gamma with vol >= min_vol_24h.

    Pagination notes (verified empirically):
      - Gamma caps page size at 100 regardless of the `limit` query param.
      - Markets are returned sorted by 24h volume descending. Once we see a
        full page where every result is below min_vol_24h, we can stop.
      - Safety bound: stop after scanning max_markets_scanned even if vol
        hasn't fallen below floor.

    Returns the same tuple shape as `selected_markets()` for caller compatibility.
    """
    out: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    offset = 0
    PAGE = 100
    scanned = 0
    consecutive_below_floor_pages = 0

    while scanned < max_markets_scanned:
        try:
            r = requests.get(
                f"{GAMMA_API}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": PAGE,
                    "offset": offset,
                },
                timeout=20,
            )
            if r.status_code != 200:
                break
            batch = r.json()
        except Exception:
            break
        if not batch:
            break

        page_below_floor = True
        for m in batch:
            scanned += 1
            cid = m.get("conditionId") or m.get("condition_id")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            if not m.get("acceptingOrders", True):
                continue
            vol = float(m.get("volume24hr") or 0)
            if vol < min_vol_24h:
                continue
            page_below_floor = False  # at least one passes — keep going

            tokens = m.get("clobTokenIds")
            t1 = t2 = ""
            if isinstance(tokens, str):
                try:
                    import json as _j
                    tlist = _j.loads(tokens)
                except Exception:
                    tlist = []
                if len(tlist) >= 2:
                    t1, t2 = tlist[0], tlist[1]
            elif isinstance(tokens, list) and len(tokens) >= 2:
                t1, t2 = tokens[0], tokens[1]
            q = (m.get("question") or "")[:200]
            out.append((q, t1, t2, cid))

        if len(batch) < PAGE:
            break  # last page
        offset += PAGE

        # Markets are vol-sorted desc; once we see ~3 pages with 0 passers we stop.
        if page_below_floor:
            consecutive_below_floor_pages += 1
            if consecutive_below_floor_pages >= 3:
                break
        else:
            consecutive_below_floor_pages = 0

    return out


def market_universe(source: str, min_vol_24h: float = 50.0) -> list[tuple[str, str, str, str]]:
    """Dispatcher: pick the candidate-market list per CLI flag."""
    if source == "selected":
        return selected_markets()
    if source == "tradable":
        return tradable_markets(min_vol_24h=min_vol_24h)
    raise ValueError(f"unknown --source: {source}")


# ---- ingest_fn_03: trade fetcher --------------------------------------------
def fetch_trades_for_market(
    cond_id: str,
    since_ts: int,
    page_size: int = 500,
    max_pages: int = 200,
) -> list[dict]:
    """Pull all trades for a condition_id whose ts >= since_ts.

    Uses data-api/trades with cursor-based pagination. Stops early when the
    returned trades all predate since_ts, or when max_pages is hit.

    Returns a list of dicts with the raw API fields we care about.
    """
    out: list[dict] = []
    cursor = ""
    for _ in range(max_pages):
        url = f"{DATA_API}/trades"
        params = {
            "market": cond_id,
            "limit": page_size,
            "offset": len(out),
        }
        try:
            r = requests.get(url, params=params, timeout=20)
            if r.status_code != 200:
                # Some markets return 404 / 500 — skip them quietly
                break
            batch = r.json()
        except Exception:
            break
        if not batch:
            break
        out.extend(batch)
        # Stop early if every trade in the batch is older than since_ts
        try:
            youngest = max(int(t.get("timestamp", 0) or 0) for t in batch)
            oldest = min(int(t.get("timestamp", 0) or 0) for t in batch)
        except ValueError:
            break
        if oldest <= since_ts and youngest <= since_ts:
            break
        if len(batch) < page_size:
            break
    # Drop trades older than since_ts at this stage so caller doesn't store junk
    return [t for t in out if int(t.get("timestamp", 0) or 0) >= since_ts]


def insert_trades(conn: sqlite3.Connection, cond_id: str, trades: list[dict]) -> int:
    """Insert trades, ignoring duplicates (UNIQUE constraint catches dupes).
    Returns count of NEW rows inserted."""
    if not trades:
        return 0
    now = int(time.time())
    rows = []
    for t in trades:
        rows.append((
            cond_id,
            str(t.get("asset") or t.get("asset_id") or ""),
            t.get("outcome") or "",
            (t.get("side") or "").upper(),
            float(t.get("price") or 0),
            float(t.get("size") or 0),
            int(t.get("timestamp") or 0),
            t.get("transactionHash") or t.get("tx_hash") or "",
            now,
        ))
    before = conn.execute("SELECT COUNT(*) FROM trades WHERE condition_id=?",
                          (cond_id,)).fetchone()[0]
    conn.executemany(
        "INSERT OR IGNORE INTO trades "
        "(condition_id, asset_id, outcome, side, price, size, ts, tx_hash, fetched_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM trades WHERE condition_id=?",
                         (cond_id,)).fetchone()[0]
    return after - before


# ---- ingest_fn_04: market metadata snapshot ---------------------------------
def snapshot_market_metadata(conn: sqlite3.Connection, cond_id: str, token1: str) -> bool:
    """Pull the market's current gamma metadata and store a snapshot row."""
    if not token1:
        return False
    try:
        r = requests.get(
            f"{GAMMA_API}/markets",
            params={"clob_token_ids": token1},
            timeout=10,
        )
        if r.status_code != 200:
            return False
        items = r.json()
        if not items:
            return False
        m = items[0]
    except Exception:
        return False

    conn.execute(
        "INSERT OR REPLACE INTO markets "
        "(condition_id, question, slug, token1, token2, answer1, answer2, "
        " tick_size, min_order_size, neg_risk, end_date_iso, volume_24hr, snapshot_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            cond_id,
            (m.get("question") or "")[:300],
            m.get("slug"),
            token1,
            None,  # token2 — gamma doesn't reliably return both; fill from bot DB instead
            None, None,
            float(m.get("orderMinTickSize") or 0.01),
            float(m.get("orderPriceMinTickSize") or 0),
            1 if m.get("negRisk") else 0,
            m.get("endDate"),
            float(m.get("volume24hr") or 0),
            int(time.time()),
        ),
    )
    conn.commit()
    return True


# ---- ingest_fn_05: orchestration --------------------------------------------
def run_ingest(days: int, source: str = "selected",
               min_vol_24h: float = 50.0, dry_run: bool = False) -> None:
    """Top-level: pick the candidate-market list per --source, fetch trades."""
    since_ts = int(time.time()) - days * 86400
    print(f"[bck_ingest] source: {source}"
          + (f" (min_vol_24h=${min_vol_24h:.0f})" if source == "tradable" else ""))
    markets = market_universe(source, min_vol_24h=min_vol_24h)
    print(f"[bck_ingest] target window: last {days} days  (since {datetime.fromtimestamp(since_ts, timezone.utc).isoformat()})")
    print(f"[bck_ingest] markets to ingest: {len(markets)}")
    if not markets:
        print("[bck_ingest] no markets in selected_markets — nothing to do")
        return

    if dry_run:
        print("[bck_ingest] --dry-run: would fetch but won't write anything")
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    trades_conn = _open_db(TRADES_DB, TRADES_SCHEMA)
    markets_conn = _open_db(MARKETS_DB, MARKETS_SCHEMA)

    totals = {"new_trades": 0, "skipped": 0, "errors": 0}
    for idx, (question, token1, _token2, cond_id) in enumerate(markets, 1):
        try:
            trades = fetch_trades_for_market(cond_id, since_ts)
        except Exception as e:
            totals["errors"] += 1
            print(f"  [{idx:2}/{len(markets)}] ERR  {question[:55]:55}  {e}")
            continue
        if not trades:
            totals["skipped"] += 1
            print(f"  [{idx:2}/{len(markets)}] (0)  {question[:55]:55}  no trades in window")
        else:
            new_n = insert_trades(trades_conn, cond_id, trades)
            totals["new_trades"] += new_n
            print(f"  [{idx:2}/{len(markets)}] +{new_n:4}  {question[:55]:55}  ({len(trades)} fetched)")
        snapshot_market_metadata(markets_conn, cond_id, token1)

    trades_conn.close()
    markets_conn.close()
    print()
    print(f"[bck_ingest] done: +{totals['new_trades']} new trades stored, "
          f"{totals['skipped']} markets had no recent trades, "
          f"{totals['errors']} errors")


# ---- ingest_fn_06: CLI ------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Pull Polymarket trade history for backtesting")
    ap.add_argument("--days", type=int, default=30,
                    help="how many days of history to fetch (default: 30)")
    ap.add_argument("--source", choices=["selected", "tradable"], default="selected",
                    help="market universe to ingest. selected = bot's current 24; "
                         "tradable = active+acceptingOrders+vol>=min_vol_24h on gamma")
    ap.add_argument("--min-vol-24h", type=float, default=50.0,
                    help="for --source tradable: 24h volume floor in $ (default 50)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be fetched without writing")
    args = ap.parse_args()
    if args.days < 1 or args.days > 365:
        print("--days must be 1..365", file=sys.stderr)
        return 2
    run_ingest(args.days, source=args.source,
               min_vol_24h=args.min_vol_24h, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
