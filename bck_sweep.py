"""bck_sweep.py — parameter grid search over backtest replay.

Objective : try many combinations of TP/SL/etc. against the same ingested
            trade history, surface which config WOULD have made money.
Rational  : if the bot's win rate is structurally low, the question is
            whether some other TP/SL combination would have been profitable.
            Grid sweep answers that empirically.
Isolation : depends only on bck_config + bck_replay. No bot imports.

Sweep dimensions (CLI flags):
    --tp        list of take_profit_threshold % values
    --sl        list of stop_loss_threshold % values (negative)
    --spread    list of spread_threshold values (stop-loss gate)
    --volume    list of min_volume_24hr floor values

Example:
    .venv/bin/python bck_sweep.py --tp 1,2,3,5 --sl -5,-10,-15
    .venv/bin/python bck_sweep.py --tp 2 --sl -10 --volume 200,500,1000,2000

Output: per-combo summary table, sorted by realized PnL desc.
"""
from __future__ import annotations

import argparse
import copy
import csv
import itertools
import sys
import time
from datetime import datetime
from pathlib import Path

import bck_config
import bck_replay

PROJECT_DIR = Path(__file__).parent.resolve()
RESULTS_DIR = PROJECT_DIR / "bck_results"

# ANSI colour codes (auto-skipped if non-TTY via _colour)
C_GRN = "\033[32m"; C_RED = "\033[31m"; C_YLW = "\033[33m"
C_BLD = "\033[1m"; C_DIM = "\033[2m"; C_RST = "\033[0m"


def _colour(s: str, code: str) -> str:
    return f"{code}{s}{C_RST}" if sys.stdout.isatty() else s


def _parse_list(s: str, cast=float) -> list:
    if not s:
        return []
    return [cast(x.strip()) for x in s.split(",") if x.strip()]


def _set_yaml_value(cfg: dict, section: str, key: str, value) -> None:
    """Mutate cfg[section][key]['value'] = value (matches YAML shape)."""
    if section not in cfg:
        cfg[section] = {}
    if key not in cfg[section]:
        cfg[section][key] = {}
    cfg[section][key]["value"] = value


def run_grid(tp_list, sl_list, spread_list, volume_list) -> list[dict]:
    """Cartesian product of provided lists. Each unspecified dimension stays at
    its YAML default (we pass through the original config unchanged for it)."""
    base_cfg = bck_config.load()

    def fallback(lst, section, key, cast=float):
        return lst if lst else [cast(base_cfg.get(section, {}).get(key, {}).get("value", 0))]

    tp_list     = fallback(tp_list, "parameters", "take_profit_threshold")
    sl_list     = fallback(sl_list, "parameters", "stop_loss_threshold")
    spread_list = fallback(spread_list, "parameters", "spread_threshold")
    volume_list = fallback(volume_list, "selection_filters", "min_volume_24hr")

    combos = list(itertools.product(tp_list, sl_list, spread_list, volume_list))
    print(f"[bck_sweep] grid size: {len(combos)} combo(s)")
    print(f"[bck_sweep] TP: {tp_list}")
    print(f"[bck_sweep] SL: {sl_list}")
    print(f"[bck_sweep] spread: {spread_list}")
    print(f"[bck_sweep] vol floor: {volume_list}")
    print()

    results: list[dict] = []
    for i, (tp, sl, sp, vol) in enumerate(combos, 1):
        t0 = time.perf_counter()
        cfg = copy.deepcopy(base_cfg)
        _set_yaml_value(cfg, "parameters", "take_profit_threshold", tp)
        _set_yaml_value(cfg, "parameters", "stop_loss_threshold", sl)
        _set_yaml_value(cfg, "parameters", "spread_threshold", sp)
        _set_yaml_value(cfg, "selection_filters", "min_volume_24hr", vol)
        # Inject a 'column' field for the filter rule if missing (so re-built rule still applies).
        try:
            cfg["selection_filters"]["min_volume_24hr"].setdefault("column", "volume_24hr")
            cfg["selection_filters"]["min_volume_24hr"].setdefault("op", ">=")
        except Exception:
            pass

        stats = bck_replay.run_replay(yaml_cfg=cfg, quiet=True)
        elapsed = time.perf_counter() - t0
        if "error" in stats:
            print(f"  [{i:>2}/{len(combos)}] ERR: {stats['error']}")
            continue
        results.append({
            "tp": tp, "sl": sl, "spread": sp, "volume": vol,
            "fills": stats["n_fills"],
            "closed": stats["wins"] + stats["losses"],
            "wins": stats["wins"], "losses": stats["losses"],
            "win_rate": stats["win_rate"],
            "stop_losses": stats["n_stop_loss"],
            "realized_pnl": stats["realized_pnl"],
            "elapsed_s": elapsed,
        })
        pnl = stats["realized_pnl"]
        colour = C_GRN if pnl > 0 else (C_RED if pnl < -1 else C_YLW)
        print(f"  [{i:>2}/{len(combos)}] tp={tp:>4.1f}%  sl={sl*100:>5.1f}%  "
              f"spread<={sp:.3f}  vol>=${vol:>5.0f}  "
              f"→ {_colour(f'${pnl:+8.2f}', colour)}  "
              f"({stats['wins']}W/{stats['losses']}L = "
              f"{stats['win_rate']:.0f}%)  {elapsed:.1f}s")
    return results


def render_table(results: list[dict]) -> None:
    """Pretty-print a summary table, sorted by realized PnL desc."""
    if not results:
        print("(no results)")
        return
    results.sort(key=lambda r: -r["realized_pnl"])
    print()
    print(_colour("══════════════════════════════ SWEEP RESULTS (sorted by PnL) ══════════════════════════════", C_BLD))
    print(f"{'TP %':>5}  {'SL %':>6}  {'spread':>7}  {'vol≥$':>7}  "
          f"{'fills':>6}  {'closed':>6}  {'wins':>5}  {'loss':>4}  "
          f"{'win%':>5}  {'stops':>5}  {'PnL':>9}")
    print("─" * 90)
    for r in results:
        pnl = r["realized_pnl"]
        colour = C_GRN if pnl > 0 else (C_RED if pnl < -1 else C_YLW)
        print(
            f"{r['tp']:>5.1f}  {r['sl']*100:>5.1f}%  "
            f"{r['spread']:>7.3f}  {r['volume']:>7.0f}  "
            f"{r['fills']:>6}  {r['closed']:>6}  "
            f"{r['wins']:>5}  {r['losses']:>4}  "
            f"{r['win_rate']:>4.0f}%  {r['stop_losses']:>5}  "
            f"{_colour(f'${pnl:+8.2f}', colour)}"
        )
    print()

    # Best / worst summary
    best = results[0]
    worst = results[-1]
    print(_colour(f"  BEST:  TP={best['tp']}%  SL={best['sl']*100:.0f}%  "
                  f"spread≤{best['spread']:.3f}  vol≥${best['volume']:.0f}  "
                  f"→  ${best['realized_pnl']:+.2f}  ({best['win_rate']:.0f}% on {best['closed']} trades)",
                  C_GRN if best['realized_pnl'] > 0 else C_YLW))
    print(_colour(f"  WORST: TP={worst['tp']}%  SL={worst['sl']*100:.0f}%  "
                  f"spread≤{worst['spread']:.3f}  vol≥${worst['volume']:.0f}  "
                  f"→  ${worst['realized_pnl']:+.2f}  ({worst['win_rate']:.0f}% on {worst['closed']} trades)",
                  C_RED if worst['realized_pnl'] < 0 else C_YLW))


def main() -> int:
    ap = argparse.ArgumentParser(description="Parameter sweep over backtest replay")
    ap.add_argument("--tp", default="", help="take_profit_threshold % values, comma-sep")
    ap.add_argument("--sl", default="", help="stop_loss_threshold % values (negative)")
    ap.add_argument("--spread", default="", help="spread_threshold values")
    ap.add_argument("--volume", default="", help="min_volume_24hr floor values ($)")
    args = ap.parse_args()

    tp_list = _parse_list(args.tp)
    sl_list = [v / 100.0 for v in _parse_list(args.sl)]  # CLI in %, internal as ratio
    spread_list = _parse_list(args.spread)
    volume_list = _parse_list(args.volume)

    if not any([tp_list, sl_list, spread_list, volume_list]):
        # Default sweep: TP × SL grid
        tp_list = [1.0, 2.0, 3.0, 5.0]
        sl_list = [-0.05, -0.10, -0.15, -0.20]
        print("[bck_sweep] no flags given — using default TP×SL grid")

    t0 = time.perf_counter()
    results = run_grid(tp_list, sl_list, spread_list, volume_list)
    elapsed = time.perf_counter() - t0
    print(f"\n[bck_sweep] total elapsed: {elapsed:.1f}s")

    render_table(results)

    # Persist
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"sweep_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with out.open("w", newline="") as f:
        if results:
            w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
            w.writeheader()
            w.writerows(results)
    print(f"  saved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
