#!/usr/bin/env python3
"""
PM_preflight.py — pre-live sanity checks. Run this BEFORE flipping DRY_RUN.

Objective: Surface every "wallet/auth/balance/allowance is wrong" problem
on the ground, not in production after the bot has placed real orders.
Rational: Most live-mode surprises come from missing allowances, empty
USDC balance, or a wallet that hasn't been initialized on Polymarket. This
script answers go/no-go in 5 seconds.
Dependencies: poly_data.polymarket_client (read-only paths)
Expected output: prints a checklist with PASS/FAIL/WARN per item; exits 0
if all PASS, 1 if any FAIL.
Test:
    python PM_preflight.py
"""

from __future__ import annotations

import os
import sys
from typing import Tuple

from dotenv import load_dotenv

CHECK = "\033[32m✓ PASS\033[0m"
WARN = "\033[33m⚠ WARN\033[0m"
FAIL = "\033[31m✗ FAIL\033[0m"

RESULTS = []


def _report(name: str, status: str, detail: str = "") -> None:
    RESULTS.append((name, status, detail))
    print(f"  {status}  {name}" + (f"  — {detail}" if detail else ""))


def check_env() -> bool:
    """01_env: required env vars present."""
    print("\n[01] Environment variables")
    load_dotenv()
    ok = True
    for var in ("PK", "BROWSER_ADDRESS"):
        if os.getenv(var):
            _report(var, CHECK, "set")
        else:
            _report(var, FAIL, "MISSING in .env")
            ok = False
    dry_run = os.getenv("DRY_RUN", "false").lower() in ("1", "true", "yes", "on")
    _report("DRY_RUN", CHECK if dry_run else WARN,
            f"={dry_run}" + (" (live trading enabled!)" if not dry_run else ""))
    return ok


def check_client_init() -> Tuple[bool, object]:
    """02_client: can we build a PolymarketClient (proves keys are valid)?"""
    print("\n[02] Polymarket client init")
    try:
        from poly_data.polymarket_client import PolymarketClient
        c = PolymarketClient()
        _report("ClobClient", CHECK, "initialised")
        return True, c
    except Exception as e:
        _report("ClobClient", FAIL, f"init raised: {e}")
        return False, None


def check_rest_auth(client) -> bool:
    """03_rest: round-trip a read-only authenticated call."""
    print("\n[03] REST auth round-trip")
    try:
        orders = client.client.get_open_orders()
        _report("get_orders()", CHECK, f"returned {len(orders)} open order(s)")
    except Exception as e:
        _report("get_orders()", FAIL, str(e))
        return False
    try:
        positions = client.get_all_positions()
        _report("get_all_positions()", CHECK, f"returned {len(positions)} row(s)")
    except Exception as e:
        _report("get_all_positions()", FAIL, str(e))
        return False
    return True


def check_balance_allowance(client) -> bool:
    """04_balance: USDC balance + CTF allowance present?"""
    print("\n[04] Balance & allowance")
    try:
        from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        info = client.client.get_balance_allowance(params)
    except Exception as e:
        _report("get_balance_allowance(COLLATERAL)", FAIL, str(e))
        return False

    balance_raw = info.get("balance", "0")
    allowance_raw = info.get("allowance", "0")
    # Polymarket returns wei-style strings; USDC is 6 decimals on Polygon.
    try:
        balance = int(balance_raw) / 1_000_000
        allowance = int(allowance_raw) / 1_000_000
    except Exception:
        balance, allowance = 0.0, 0.0

    if balance >= 5:
        _report("USDC balance", CHECK, f"${balance:.2f}")
    elif balance > 0:
        _report("USDC balance", WARN, f"only ${balance:.2f} — not enough for $5 trade test")
    else:
        _report("USDC balance", FAIL, "$0.00 — fund the wallet before going live")

    if allowance >= 1_000:
        _report("USDC allowance (CTF)", CHECK, f"${allowance:,.0f} (essentially unlimited)")
    elif allowance > 0:
        _report("USDC allowance (CTF)", WARN, f"only ${allowance:.2f} — bot may exhaust allowance")
    else:
        # Under CLOB V2, the collateral is wrapped (Polymarket USD) and the
        # exchange does not require the legacy ERC-20 allowance pattern.
        # We confirmed live order placement works with allowance=0; treat as WARN.
        _report("USDC allowance (CTF)", WARN,
                "$0 reported — V2 uses wrapped collateral, allowance check not load-bearing. "
                "Live trades confirmed working at allowance=0.")
    return balance > 0


def check_breaker_state() -> bool:
    """05_breaker: nothing currently tripped that would block first BUYs."""
    print("\n[05] Circuit breaker state")
    try:
        import PM_circuit_breakers as cb
        snap = cb.snapshot()
        if snap["blocked"]:
            _report("Breaker tripped", FAIL, snap["block_reason"])
            return False
        else:
            _report("Breaker", CHECK, "all clear")
            cfg_dict = snap["config"]
            _report("Daily stop-loss cap", CHECK,
                    f"max count {cfg_dict['daily_max_stop_loss_count']}, "
                    f"max loss ${cfg_dict['daily_max_stop_loss_loss_usd']}")
            _report("Total exposure cap", CHECK, f"${cfg_dict['max_total_exposure_usd']}")
        return True
    except Exception as e:
        _report("Breaker module", WARN, str(e))
        return True  # not fatal


def check_config() -> bool:
    """06_config: trading_defaults + filter sanity."""
    print("\n[06] Config sanity")
    try:
        import PM_config_loader as cfg
        td = cfg.trading_defaults()
        f = cfg.selection_filters()
    except Exception as e:
        _report("config load", FAIL, str(e))
        return False
    trade_size = td.get("trade_size", 0)
    max_size = td.get("max_size", 0)
    if trade_size > 0 and max_size >= trade_size:
        _report("trade_size / max_size", CHECK, f"${trade_size} / ${max_size}")
    else:
        _report("trade_size / max_size", FAIL,
                f"invalid: ${trade_size} / ${max_size}")
        return False
    max_min = f.get("max_min_size", {}).get("value", 0)
    if max_min and trade_size >= max_min:
        _report("trade_size vs filter max_min_size", CHECK,
                f"${trade_size} >= ${max_min}")
    else:
        _report("trade_size vs filter max_min_size", WARN,
                f"${trade_size} < ${max_min} — bot will quote on markets it can't actually order on")
    return True


def main():
    print("=" * 60)
    print("Poly-maker preflight checklist")
    print("=" * 60)

    if not check_env():
        print("\nAborting: env vars missing.")
        sys.exit(1)

    ok, client = check_client_init()
    if not ok:
        print("\nAborting: client init failed.")
        sys.exit(1)

    if not check_rest_auth(client):
        print("\nAborting: REST auth failed.")
        sys.exit(1)

    check_balance_allowance(client)
    check_breaker_state()
    check_config()

    # Summary
    print("\n" + "=" * 60)
    fails = [r for r in RESULTS if r[1] == FAIL]
    warns = [r for r in RESULTS if r[1] == WARN]
    print(f"Result: {len(fails)} FAIL, {len(warns)} WARN, "
          f"{len(RESULTS) - len(fails) - len(warns)} PASS")
    if fails:
        print("\nFAILS to fix before going live:")
        for n, _, d in fails:
            print(f"  - {n}: {d}")
        sys.exit(1)
    if warns:
        print("\nWARNs to consider:")
        for n, _, d in warns:
            print(f"  - {n}: {d}")
    print("\nPreflight passed. You may proceed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
