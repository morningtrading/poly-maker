#!/usr/bin/env python3
"""PM_reconcile.py — isolated read-only reconciliation: local view vs Polymarket.

Objective : detect drift between what the bot has done locally and what
            Polymarket actually shows. Catch orphan positions, dead markets,
            stalled bot, and network-latency anomalies — without ever touching
            bot state.
Rational  : early debugging showed the bot's view diverged from reality
            (Mariners 0.26-share orphan, 13/14 positions in dead pools). A
            scheduled out-of-band check would have caught both within minutes.
Dependencies (READ-ONLY usage of all of these):
    - requests          (data-api / CLOB / gamma HTTPS GETs)
    - web3              (on-chain CTF + USDC balances)
    - py_clob_client_v2 (CLOB open orders — same auth shape as the bot)
    - sqlite3 / dotenv  (read .env + polymb.db)
    - poly_data.abis    (constants only — ABI definitions)
Expected output : ANSI-coloured one-line-per-check summary to stdout +
                  append to logs/PM_reconcile_<date>.log + per-call latencies
                  to logs/PM_reconcile_latency.csv.
Isolation guarantees :
    - never writes to polymb.db tables used by the bot (selected_markets,
      all_markets, summary, etc.)
    - never writes to config/circuit_state.json or paper_state.json
    - never writes to .env or any YAML
    - the only files written are logs/PM_reconcile_*.{log,csv}
Test :
    .venv/bin/python PM_reconcile.py                # one-shot
    .venv/bin/python PM_reconcile.py --loop 300     # every 5 min
    .venv/bin/python PM_reconcile.py --check R1     # single check
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import requests
from dotenv import load_dotenv
from web3 import Web3

# Read-only constants only
from poly_data.abis import ConditionalTokenABI, erc20_abi

# ---- recon_cfg_01: paths + addresses (none of which we mutate) ---------------
PROJECT_DIR = Path(__file__).parent.resolve()
ENV_PATH = PROJECT_DIR / ".env"
DB_PATH = PROJECT_DIR / "polymb.db"
PID_FILE = PROJECT_DIR / ".pm_bot.pid"
LOG_DIR = PROJECT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
LATENCY_CSV = LOG_DIR / "PM_reconcile_latency.csv"
RECON_LOG = LOG_DIR / f"PM_reconcile_{datetime.now():%Y%m%d}.log"

CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
POLYMARKET_USD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"

# ---- recon_cfg_02: thresholds (TOLERANT — do not over-react) -----------------
# Below these we stay silent. Anything strictly larger gets a WARN line.
MIN_NOTIONAL_USD = 1.00              # dust floor — Polymarket can't act below $1
POSITION_VALUE_DRIFT_USD = 5.00      # |api_value - on_chain_estimate| tolerance
SHARE_COUNT_DRIFT = 5.0              # ignore fractional rounding
CASH_DROP_ALERT_USD = 10.00          # day-over-day drop threshold (R4 reuses)
HEARTBEAT_SILENCE_S = 300            # 5 min of no new log lines = stalled
DEAD_MARKET_HOURS_FLOOR = 48         # market resolving in <48h = warn-worthy
DEAD_MARKET_VOLUME_FLOOR = 200.0     # market with <$200/24h = warn-worthy

# Latency thresholds (R4)
LATENCY_BASELINE_WINDOW = 50         # rolling sample size
LATENCY_RECENT_WINDOW = 10           # short-term avg for drift detection
LATENCY_SPIKE_FACTOR = 5.0           # single call >5× baseline median → SPIKE
LATENCY_DRIFT_FACTOR = 2.0           # recent_median >2× baseline_median → DRIFT
LATENCY_MIN_BASELINE_SAMPLES = 10    # don't trip alerts until we have history

# DB / disk health thresholds (R5)
DB_SIZE_WARN_MB = 500                # polymb.db larger than this → WARN
LOGS_SIZE_WARN_MB = 1000             # logs/ directory larger than this → WARN
DISK_FREE_FAIL_MB = 1000             # less than this on the partition → FAIL
DB_INTEGRITY_OK = "ok"               # PRAGMA integrity_check expected string

# Bot resource health thresholds (R6)
BOT_RSS_WARN_MB = 1000               # main.py RSS larger than this → WARN
BOT_LOG_TRACEBACK_PER_HR_WARN = 100  # tracebacks in last hour exceeding this → WARN
BOT_LOG_LOOKBACK_LINES = 5000        # tail size for traceback counting

# ---- recon_cfg_03: ANSI colours ----------------------------------------------
C_RED = "\033[31m"
C_GRN = "\033[32m"
C_YLW = "\033[33m"
C_CYN = "\033[36m"
C_BLD = "\033[1m"
C_RST = "\033[0m"


# ---- recon_fn_01: structured logger + result table accumulator --------------
# Each check's final log_line() call also appends to RUN_RESULTS so we can
# print a nice OK/WARN/FAIL summary table at the end of every run (or cycle).
RUN_RESULTS: list[tuple[str, str, str]] = []  # (check, level, msg)


def _ts() -> str:
    """Local-time short timestamp for human-readable lines."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_line(level: str, check: str, msg: str, detail: str = "") -> None:
    """One line to stdout (coloured) + append to recon log file (plain) +
    record into RUN_RESULTS for the summary table.

    level ∈ {OK, WARN, FAIL, INFO}. WARN/FAIL coloured red/yellow.
    """
    colour = {"OK": C_GRN, "INFO": C_CYN, "WARN": C_YLW, "FAIL": C_RED}.get(level, C_RST)
    line = f"[{_ts()}] {check}: {level}  {msg}"
    print(f"{colour}{line}{C_RST}")
    with RECON_LOG.open("a") as f:
        f.write(line + "\n")
        if detail:
            for d in detail.splitlines():
                f.write(f"    {d}\n")
                print(f"    {d}")
    RUN_RESULTS.append((check, level, msg))


def _figlet_banner(text: str) -> Optional[str]:
    """Render `text` using the system figlet binary. Returns None if figlet
    is not installed (caller falls back to plain text)."""
    try:
        import subprocess
        r = subprocess.run(["figlet", "-f", "standard", text],
                           capture_output=True, text=True, timeout=2)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.rstrip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _overall_status() -> str:
    """Compute headline status from RUN_RESULTS: FAIL > WARN > OK."""
    levels = {lvl for _, lvl, _ in RUN_RESULTS}
    if "FAIL" in levels:
        return "FAIL"
    if "WARN" in levels:
        return "WARNING"
    return "BOT OK"


def print_summary() -> None:
    """Big figlet headline + pretty per-check table; written to stdout + recon log."""
    if not RUN_RESULTS:
        return

    # Headline banner — visual at-a-glance state
    headline = _overall_status()
    banner = _figlet_banner(headline)
    banner_colour = {"BOT OK": C_GRN, "WARNING": C_YLW, "FAIL": C_RED}.get(headline, C_RST)
    if banner:
        print(f"\n{banner_colour}{C_BLD}{banner}{C_RST}")
        with RECON_LOG.open("a") as f:
            f.write("\n" + banner + "\n")
    else:
        # Fallback if figlet not installed — bold plain text still readable
        fallback = f"\n  ====== {headline} ======\n"
        print(f"{banner_colour}{C_BLD}{fallback}{C_RST}")
        with RECON_LOG.open("a") as f:
            f.write(fallback + "\n")

    # Column widths
    w_chk = max(len(r[0]) for r in RUN_RESULTS)
    w_chk = max(w_chk, len("Check"))
    w_lvl = 6
    border = "─" * (w_chk + w_lvl + 60 + 6)

    counts = {"OK": 0, "WARN": 0, "FAIL": 0, "INFO": 0}
    lines = [border, f"{'Check':<{w_chk}} │ {'Status':<{w_lvl}} │ Detail", border]
    for chk, lvl, msg in RUN_RESULTS:
        counts[lvl] = counts.get(lvl, 0) + 1
        colour = {"OK": C_GRN, "INFO": C_CYN, "WARN": C_YLW, "FAIL": C_RED}.get(lvl, C_RST)
        msg_short = msg if len(msg) <= 60 else msg[:57] + "..."
        # We write the coloured status only to stdout; the file gets plain text
        lines.append(f"{chk:<{w_chk}} │ {colour}{lvl:<{w_lvl}}{C_RST} │ {msg_short}")
    summary = (f"{C_BLD}TOTAL:{C_RST} "
               f"{C_GRN}{counts['OK']} OK{C_RST}, "
               f"{C_YLW}{counts['WARN']} WARN{C_RST}, "
               f"{C_RED}{counts['FAIL']} FAIL{C_RST}"
               f"{', ' + str(counts['INFO']) + ' INFO' if counts['INFO'] else ''}")
    lines.append(border)
    lines.append(summary)
    lines.append(border)
    print("\n" + "\n".join(lines))

    # Plain text mirror for the file
    plain_lines = ["", border.replace("│", "|")]
    plain_lines.append(f"{'Check':<{w_chk}} | {'Status':<{w_lvl}} | Detail")
    plain_lines.append(border.replace("│", "|"))
    for chk, lvl, msg in RUN_RESULTS:
        msg_short = msg if len(msg) <= 60 else msg[:57] + "..."
        plain_lines.append(f"{chk:<{w_chk}} | {lvl:<{w_lvl}} | {msg_short}")
    plain_lines.append(border.replace("│", "|"))
    plain_lines.append(
        f"TOTAL: {counts['OK']} OK, {counts['WARN']} WARN, {counts['FAIL']} FAIL"
        + (f", {counts['INFO']} INFO" if counts['INFO'] else ""))
    plain_lines.append(border.replace("│", "|"))
    with RECON_LOG.open("a") as f:
        f.write("\n".join(plain_lines) + "\n")


# ---- recon_fn_02: timed network call helper ---------------------------------
def timed(endpoint_label: str, fn: Callable[[], Any]) -> tuple[Any, float]:
    """Run fn(), return (result, elapsed_ms). Appends a row to LATENCY_CSV.

    Why: every reconcile call's latency is recorded so R4 can spot drift later.
    """
    t0 = time.perf_counter()
    result = fn()
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    _append_latency(endpoint_label, elapsed_ms)
    return result, elapsed_ms


def _append_latency(endpoint: str, ms: float) -> None:
    """Append one row to logs/PM_reconcile_latency.csv (created if missing)."""
    new_file = not LATENCY_CSV.exists()
    with LATENCY_CSV.open("a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["ts_iso", "endpoint", "ms"])
        w.writerow([datetime.now(timezone.utc).isoformat(), endpoint, f"{ms:.2f}"])


# ---- recon_fn_03: clients (lazy-initialised, isolated) ----------------------
class Clients:
    """Holds Polymarket + web3 clients used by the checks. Initialised lazily."""

    def __init__(self):
        load_dotenv(ENV_PATH, override=False)
        self.funder_addr = Web3.to_checksum_address(os.getenv("BROWSER_ADDRESS"))
        self.funder_lo = self.funder_addr.lower()
        self.pk = os.getenv("PK")
        rpc_url = os.getenv("POLYGON_RPC_URL", "https://polygon-bor-rpc.publicnode.com")
        self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
        self.ctf = self.w3.eth.contract(
            address=Web3.to_checksum_address(CTF_ADDRESS),
            abi=ConditionalTokenABI,
        )
        self.poly_usd = self.w3.eth.contract(
            address=Web3.to_checksum_address(POLYMARKET_USD),
            abi=erc20_abi,
        )
        self._clob = None  # init on first use

    def clob(self):
        """Return an authenticated CLOB V2 client (cached)."""
        if self._clob is not None:
            return self._clob
        from py_clob_client_v2 import ClobClient, SignatureTypeV2
        boot = ClobClient(host="https://clob.polymarket.com", chain_id=137,
                          key=self.pk, funder=self.funder_addr,
                          signature_type=SignatureTypeV2.POLY_1271)
        creds = boot.create_or_derive_api_key()
        self._clob = ClobClient(host="https://clob.polymarket.com", chain_id=137,
                                key=self.pk, creds=creds, funder=self.funder_addr,
                                signature_type=SignatureTypeV2.POLY_1271)
        return self._clob


# ---- recon_fn_04: helpers used by checks ------------------------------------
def fetch_all_positions(c: Clients) -> list[dict]:
    """data-api with sizeThreshold=0 → ALL positions including dust."""
    r, _ = timed("data-api/positions", lambda: requests.get(
        "https://data-api.polymarket.com/positions",
        params={"user": c.funder_lo, "limit": 500, "sizeThreshold": 0},
        timeout=15))
    return r.json() if r.status_code == 200 else []


def fetch_selected_markets() -> dict[str, dict]:
    """Return {question: {token1, token2, condition_id, ...}} from polymb.db."""
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(
            "SELECT question, token1, token2, condition_id FROM selected_markets")
        return {q: {"token1": t1, "token2": t2, "condition_id": cid}
                for q, t1, t2, cid in cur.fetchall()}
    finally:
        conn.close()


def fetch_gamma_market(c: Clients, token_id: str) -> Optional[dict]:
    """Get one market's metadata from gamma. Returns None on failure."""
    r, _ = timed("gamma/markets", lambda: requests.get(
        "https://gamma-api.polymarket.com/markets",
        params={"clob_token_ids": token_id}, timeout=10))
    if r.status_code != 200:
        return None
    j = r.json()
    return j[0] if j else None


# ============================================================================
# CHECK R1: orphan positions (Polymarket has positions in markets we don't trade)
# ============================================================================
def check_r1_orphans(c: Clients) -> None:
    """Compare data-api positions (incl. dust) to selected_markets.

    Tolerances:
      - position notional < $1 → silent (Mariners-class dust is acknowledged
        and ignored; can't be sold via Polymarket marketable orders anyway)
      - position in a market that IS in selected_markets → silent (the bot
        could legitimately have opened it; on-chain check is a deeper R1b
        we don't run here to stay simple)
    """
    positions = fetch_all_positions(c)
    selected = fetch_selected_markets()
    selected_tokens = set()
    for v in selected.values():
        if v["token1"]: selected_tokens.add(str(v["token1"]))
        if v["token2"]: selected_tokens.add(str(v["token2"]))

    orphans = []
    for p in positions:
        notional = float(p.get("currentValue") or 0)
        if notional < MIN_NOTIONAL_USD:
            continue  # ignore dust
        asset = str(p.get("asset", ""))
        if asset in selected_tokens:
            continue  # legitimate bot position
        orphans.append({
            "title": (p.get("title") or "")[:60],
            "outcome": p.get("outcome"),
            "size": float(p.get("size") or 0),
            "value": notional,
            "end_date": (p.get("endDate") or "")[:10],
        })

    if not orphans:
        log_line("OK", "R1 orphan_positions", f"0 orphans (scanned {len(positions)})")
        return
    detail = "\n".join(
        f"- ${o['value']:.2f}  {o['outcome']:4} {o['size']:.2f}sh  "
        f"end={o['end_date']}  {o['title']}"
        for o in orphans)
    log_line("WARN", "R1 orphan_positions",
             f"{len(orphans)} orphans totalling ${sum(o['value'] for o in orphans):.2f}",
             detail)


# ============================================================================
# CHECK R2: dead markets in selected_markets
# ============================================================================
def check_r2_dead_markets(c: Clients) -> None:
    """For each selected market, ask gamma if it's still tradeable.

    Tolerances:
      - we only WARN, never modify selected_markets
      - silent if all checks pass
      - skip markets whose token1 is missing
    """
    selected = fetch_selected_markets()
    now = datetime.now(timezone.utc)
    dead = []
    checked = 0
    for q, v in selected.items():
        if not v["token1"]:
            continue
        m = fetch_gamma_market(c, v["token1"])
        if m is None:
            continue
        checked += 1
        problems = []
        if m.get("closed"): problems.append("closed")
        if m.get("archived"): problems.append("archived")
        if not m.get("acceptingOrders", True): problems.append("not accepting_orders")
        end_iso = m.get("endDate") or m.get("end_date_iso")
        if end_iso:
            try:
                end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
                hrs = (end - now).total_seconds() / 3600
                if hrs < DEAD_MARKET_HOURS_FLOOR:
                    problems.append(f"resolves in {hrs:.0f}h")
            except Exception:
                pass
        vol = float(m.get("volume24hr") or 0)
        if vol < DEAD_MARKET_VOLUME_FLOOR:
            problems.append(f"vol24=${vol:.0f}")
        if problems:
            dead.append({"q": q[:60], "issues": problems})

    if not dead:
        log_line("OK", "R2 dead_markets", f"all {checked} selected markets healthy")
        return
    detail = "\n".join(f"- {d['q']}  →  {', '.join(d['issues'])}" for d in dead)
    detail += "\nSuggestion: re-run PM_filter_markets.py"
    log_line("WARN", "R2 dead_markets",
             f"{len(dead)}/{checked} selected markets degraded",
             detail)


# ============================================================================
# CHECK R3: bot heartbeat
# ============================================================================
def check_r3_heartbeat() -> None:
    """Check PID alive + log file growing.

    Tolerances:
      - 5 min silence threshold (bot loop is ~30s, 10× margin)
      - missing PID file → INFO not WARN (bot may not be expected to run)
    """
    if not PID_FILE.exists():
        log_line("INFO", "R3 heartbeat", "no .pm_bot.pid (bot not expected to run)")
        return
    try:
        pid = int(PID_FILE.read_text().strip())
    except Exception:
        log_line("WARN", "R3 heartbeat", "could not parse .pm_bot.pid")
        return

    proc_alive = Path(f"/proc/{pid}").exists()
    if not proc_alive:
        log_line("WARN", "R3 heartbeat",
                 f"PID {pid} in .pm_bot.pid but process not running (stale pid file)")
        return

    # find latest PM_main_*.log and check mtime
    logs = sorted(LOG_DIR.glob("PM_main_*.log"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    if not logs:
        log_line("WARN", "R3 heartbeat", f"PID {pid} alive but no PM_main_*.log found")
        return
    age_s = time.time() - logs[0].stat().st_mtime
    if age_s > HEARTBEAT_SILENCE_S:
        log_line("WARN", "R3 heartbeat",
                 f"PID {pid} alive but log silent for {age_s:.0f}s (>{HEARTBEAT_SILENCE_S}s)",
                 f"latest log: {logs[0].name}")
        return
    log_line("OK", "R3 heartbeat",
             f"PID {pid} alive, log fresh ({age_s:.0f}s ago)")


# ============================================================================
# CHECK R4: network latency drift / spike detection
# ============================================================================
def _read_latency_history() -> dict[str, list[float]]:
    """Read latency CSV and bucket by endpoint."""
    history: dict[str, list[float]] = {}
    if not LATENCY_CSV.exists():
        return history
    with LATENCY_CSV.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            ep = row["endpoint"]
            try:
                history.setdefault(ep, []).append(float(row["ms"]))
            except Exception:
                continue
    return history


def check_r4_latency() -> None:
    """Analyse the latency CSV: per endpoint, compare recent vs baseline.

    Tolerances:
      - need at least LATENCY_MIN_BASELINE_SAMPLES to trip
      - SPIKE if last call > 5× baseline median
      - DRIFT if median of last 10 > 2× median of last 50
    """
    history = _read_latency_history()
    if not history:
        log_line("INFO", "R4 latency", "no latency history yet (run at least once)")
        return

    anomalies = []
    summary = []
    for endpoint, samples in history.items():
        baseline = samples[-LATENCY_BASELINE_WINDOW:]
        if len(baseline) < LATENCY_MIN_BASELINE_SAMPLES:
            summary.append(f"{endpoint}: {len(baseline)} samples (warming up)")
            continue
        base_med = statistics.median(baseline)
        last_ms = samples[-1]
        recent = samples[-LATENCY_RECENT_WINDOW:] if len(samples) >= LATENCY_RECENT_WINDOW else samples
        recent_med = statistics.median(recent)
        summary.append(
            f"{endpoint}: last={last_ms:.0f}ms  recent_med={recent_med:.0f}ms  "
            f"base_med={base_med:.0f}ms  (n={len(baseline)})")
        if last_ms > base_med * LATENCY_SPIKE_FACTOR:
            anomalies.append(
                f"SPIKE  {endpoint}: last call {last_ms:.0f}ms vs baseline {base_med:.0f}ms "
                f"({last_ms/base_med:.1f}×)")
        if recent_med > base_med * LATENCY_DRIFT_FACTOR:
            anomalies.append(
                f"DRIFT  {endpoint}: recent_med {recent_med:.0f}ms vs baseline {base_med:.0f}ms "
                f"({recent_med/base_med:.1f}×)")

    if anomalies:
        log_line("WARN", "R4 latency",
                 f"{len(anomalies)} anomaly/anomalies across {len(history)} endpoints",
                 "\n".join(anomalies + [""] + summary))
    else:
        log_line("OK", "R4 latency",
                 f"all {len(history)} endpoints within tolerance",
                 "\n".join(summary))


# ============================================================================
# CHECK R5: DB & disk health
# ============================================================================
def _dir_size_mb(path: Path) -> float:
    """Recursive total size of all files under path, in MB."""
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except Exception:
            continue
    return total / (1024 * 1024)


def check_r5_db_disk() -> None:
    """polymb.db size + integrity, logs/ size, disk free space.

    Tolerances:
      - absolute thresholds only (no day-over-day delta — keep state-free)
      - integrity_check failures are FAIL, not WARN (silent corruption is bad)
    """
    issues = []
    info = []

    # 1. polymb.db size + integrity
    if DB_PATH.exists():
        db_mb = DB_PATH.stat().st_size / (1024 * 1024)
        info.append(f"polymb.db: {db_mb:.1f} MB")
        if db_mb > DB_SIZE_WARN_MB:
            issues.append(("WARN", f"polymb.db is {db_mb:.0f} MB (> {DB_SIZE_WARN_MB} MB)"))

        # integrity_check
        try:
            conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            try:
                result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                conn.close()
            if result != DB_INTEGRITY_OK:
                issues.append(("FAIL", f"polymb.db integrity_check returned: {result[:200]}"))
            else:
                info.append("integrity_check: ok")
        except Exception as e:
            issues.append(("WARN", f"integrity_check failed to run: {e}"))

        # row counts on key tables (info only — drops/spikes alert later if we add deltas)
        try:
            conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            for table in ("all_markets", "selected_markets"):
                try:
                    n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    info.append(f"{table}: {n} rows")
                except Exception:
                    info.append(f"{table}: missing")
            conn.close()
        except Exception:
            pass
    else:
        issues.append(("WARN", f"polymb.db not found at {DB_PATH}"))

    # 2. logs/ directory size
    if LOG_DIR.exists():
        logs_mb = _dir_size_mb(LOG_DIR)
        info.append(f"logs/: {logs_mb:.1f} MB")
        if logs_mb > LOGS_SIZE_WARN_MB:
            issues.append(("WARN",
                f"logs/ is {logs_mb:.0f} MB (> {LOGS_SIZE_WARN_MB} MB) — rotation overdue"))

    # 3. disk free space on the partition holding the project
    try:
        import shutil
        free_bytes = shutil.disk_usage(PROJECT_DIR).free
        free_mb = free_bytes / (1024 * 1024)
        info.append(f"disk free: {free_mb:.0f} MB")
        if free_mb < DISK_FREE_FAIL_MB:
            issues.append(("FAIL", f"disk free is only {free_mb:.0f} MB (< {DISK_FREE_FAIL_MB} MB)"))
    except Exception as e:
        info.append(f"disk free: unable to check ({e})")

    detail = "; ".join(info)
    if not issues:
        log_line("OK", "R5 db_disk", "all healthy", detail)
        return
    # Promote level: FAIL beats WARN
    worst = "FAIL" if any(lv == "FAIL" for lv, _ in issues) else "WARN"
    msg = " | ".join(m for _, m in issues)
    log_line(worst, "R5 db_disk", msg, detail)


# ============================================================================
# CHECK R6: bot resource health (RSS + traceback rate in current log)
# ============================================================================
def _bot_process():
    """Resolve the bot's psutil.Process via the PID file. Return None if absent/dead."""
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
    except Exception:
        return None
    try:
        import psutil
        proc = psutil.Process(pid)
        if not proc.is_running():
            return None
        return proc
    except Exception:
        return None


def _count_recent_tracebacks(log_path: Path, lookback_lines: int) -> int:
    """Count 'Traceback (most recent call last):' in the last N lines of log_path."""
    if not log_path.exists():
        return 0
    try:
        # Tail efficiently — read last N lines
        with log_path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(size, max(64_000, lookback_lines * 200))
            f.seek(size - chunk, 0)
            tail = f.read().decode("utf-8", errors="ignore")
        lines = tail.splitlines()[-lookback_lines:]
        return sum(1 for line in lines if "Traceback (most recent call last):" in line)
    except Exception:
        return 0


def check_r6_bot_resources() -> None:
    """RSS + log error rate of the bot process.

    Tolerances:
      - silent if no bot is running (treat as not-applicable INFO)
      - RSS only triggers if > BOT_RSS_WARN_MB (default 1 GB)
      - traceback alert at BOT_LOG_TRACEBACK_PER_HR_WARN (default 100/hr)
        based on rough count from the last BOT_LOG_LOOKBACK_LINES log lines
    """
    proc = _bot_process()
    if proc is None:
        log_line("INFO", "R6 bot_resources", "bot not running (skip)")
        return

    issues = []
    info = []

    try:
        mem_mb = proc.memory_info().rss / (1024 * 1024)
        info.append(f"RSS={mem_mb:.0f} MB")
        if mem_mb > BOT_RSS_WARN_MB:
            issues.append(("WARN", f"RSS {mem_mb:.0f} MB > {BOT_RSS_WARN_MB} MB"))
    except Exception as e:
        info.append(f"RSS: unavailable ({e})")

    try:
        cpu = proc.cpu_percent(interval=0.2)
        info.append(f"CPU={cpu:.0f}%")
    except Exception:
        pass

    try:
        uptime_s = time.time() - proc.create_time()
        info.append(f"uptime={uptime_s:.0f}s")
    except Exception:
        uptime_s = None

    # traceback rate in the latest bot log
    logs = sorted(LOG_DIR.glob("PM_main_*.log"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    if logs:
        tb_count = _count_recent_tracebacks(logs[0], BOT_LOG_LOOKBACK_LINES)
        info.append(f"tracebacks in last {BOT_LOG_LOOKBACK_LINES} log lines: {tb_count}")
        # rough estimate: if log is busy, BOT_LOG_LOOKBACK_LINES ~= last few minutes;
        # if quiet, last day. Use proxied "per-hour" via uptime scaling if uptime known
        # and < 1h, else compare directly.
        # Simpler: alert above the absolute count threshold.
        if tb_count > BOT_LOG_TRACEBACK_PER_HR_WARN:
            issues.append(("WARN",
                f"{tb_count} tracebacks in tail (> {BOT_LOG_TRACEBACK_PER_HR_WARN})"))

    detail = "; ".join(info)
    if not issues:
        log_line("OK", "R6 bot_resources", "bot healthy", detail)
        return
    msg = " | ".join(m for _, m in issues)
    log_line("WARN", "R6 bot_resources", msg, detail)


# ---- recon_fn_main: CLI ------------------------------------------------------
def run_all(clients: Clients) -> None:
    check_r1_orphans(clients)
    check_r2_dead_markets(clients)
    check_r3_heartbeat()
    check_r4_latency()
    check_r5_db_disk()
    check_r6_bot_resources()


def main() -> None:
    ap = argparse.ArgumentParser(description="PM_reconcile — local vs Polymarket checks")
    ap.add_argument("--check", choices=["R1", "R2", "R3", "R4", "R5", "R6", "all"], default="all")
    ap.add_argument("--loop", type=int, metavar="SECONDS",
                    help="Run continuously every N seconds (60–3600)")
    args = ap.parse_args()

    if args.loop:
        if not (60 <= args.loop <= 3600):
            sys.exit("--loop must be 60..3600")
        print(f"{C_CYN}reconcile loop every {args.loop}s "
              f"(Ctrl-C to stop, log: {RECON_LOG}){C_RST}")

    cycle = 0
    while True:
        cycle += 1
        RUN_RESULTS.clear()  # fresh table each cycle
        # Cycle header — makes each cycle visually distinct in loop mode.
        # Without it, cycle N's table runs into cycle N+1's log lines and
        # it's unclear which cycle the table belongs to.
        header = f"═══ Cycle {cycle} @ {_ts()} ═══"
        print(f"\n{C_BLD}{C_CYN}{header}{C_RST}")
        with RECON_LOG.open("a") as f:
            f.write(f"\n{header}\n")
        clients = Clients()  # fresh per cycle so cached creds don't go stale
        try:
            if args.check == "all":
                run_all(clients)
            elif args.check == "R1":
                check_r1_orphans(clients)
            elif args.check == "R2":
                check_r2_dead_markets(clients)
            elif args.check == "R3":
                check_r3_heartbeat()
            elif args.check == "R4":
                check_r4_latency()
            elif args.check == "R5":
                check_r5_db_disk()
            elif args.check == "R6":
                check_r6_bot_resources()
        except KeyboardInterrupt:
            print("\nstopped")
            break
        except Exception as e:
            log_line("FAIL", "reconcile", f"unexpected error: {type(e).__name__}: {e}")

        print_summary()

        if not args.loop:
            break
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
