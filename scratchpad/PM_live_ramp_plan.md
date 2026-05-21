# PM_live_ramp_plan — Path to live real-money trading

Draft created 2026-05-20. Conservative ramp; each task has a unique `live_NN` key.
Companion to `PM_preflight.py` and `PM_bot_control.py`.

Kill switch (pin this somewhere visible):
```
python -c "from PM_bot_control import stop; print(stop())"
```

---

## Phase A — On-chain prep (off-host)

- **live_01** — Fund `BROWSER_ADDRESS` with USDC on Polygon. Start = intended `max_total_exposure_usd`, not more.
- **live_02** — Approve USDC for the CTF Exchange contract by making ONE small manual trade on polymarket.com. This sets the on-chain allowance the bot relies on.
- **live_03** — Provision paid Polygon RPC (Alchemy/Infura/QuickNode). Set `POLYGON_RPC_URL` in `.env`. Public node throttles under merge load.

## Phase B — Risk sizing in `config/standard_config.yaml`

- **live_04** — Set `trade_size` to small starting value ($5–$10), down from current $50.
- **live_05** — Set `max_size` = 1×–2× `trade_size` for week 1.
- **live_06** — Tighten `circuit_breakers.max_total_exposure_usd` to ≈ wallet_balance × 0.5 (currently $5000 — too loose for first run).
- **live_07** — Lower `daily_max_stop_loss_loss_usd` from $25 → ~5% of bankroll.
- **live_08** — Review `cheap_multiplier_default=4` exposure math: at price 0.05 with $5 trade_size, 4× → $20 per cheap fill. Confirm acceptable or override per-market via `PM_set_multiplier.py`.
- **live_09** — Run `PM_select_market.py` and confirm expected market universe size + composition.

## Phase C — Code/safety verification (already implemented — verify, don't rewrite)

- **live_10** — Confirm circuit breakers gate the BUY path. `grep -n PM_circuit_breakers trading.py poly_data/trading_utils.py`.
- **live_11** — Confirm `DRY_RUN` gates orders, cancels, AND merges. `grep -rn DRY_RUN poly_data/ trading.py main.py`.
- **live_12** — Confirm SIGTERM closes websocket loops cleanly. `kill -TERM $(cat .pm_bot.pid)` in DRY_RUN, inspect log for clean shutdown.
- **live_13** — Confirm duplicate-launch refusal works. Start twice; second start should return `{"ok": false, "reason": "Bot already running..."}`.

## Phase D — Extended paper validation (DRY_RUN, real market data)

- **live_14** — Run bot 24h in DRY_RUN with the **intended live config** (post Phase B).
- **live_15** — Review `logs/PM_main_*.log` for `[DRY_RUN] Would post order` patterns — sanity-check sizes, prices, frequency.
- **live_16** — Inspect `PM_paper_fills.py` output for hypothetical PnL.
- **live_17** — Verify stop-loss + take-profit fired at least once in the paper run. `grep -n 'stop_loss\|tp_price' logs/PM_main_*.log`.
- **live_18** — Confirm zero unexpected exceptions: `grep -i 'traceback\|error' logs/PM_main_*.log`.

## Phase E — Pre-live final gate

- **live_19** — Backup SQLite DB: `cp polymb.db polymb.db.bak.preflive_$(date +%Y%m%d)`.
- **live_20** — Stop DRY_RUN bot: `python -c "from PM_bot_control import stop; print(stop())"`.
- **live_21** — Flip `DRY_RUN=false` in `.env`.
- **live_22** — Re-run `PM_preflight.py`. Require **0 FAIL, 0 WARN**.
- **live_23** — LIVE-RISK GATE: explicit `GO LIVE: yes` confirmation from user (per CLAUDE.md §6).

## Phase F — Launch + monitoring

- **live_24** — Launch via `python -c "from PM_bot_control import start; print(start())"`.
- **live_25** — Manually verify first 5–10 orders appear on polymarket.com under your wallet.
- **live_26** — Open `PM_dashboard.py` (streamlit) and confirm telemetry is live.
- **live_27** — Document the kill switch above somewhere visible (README or terminal note).
- **live_28** — Week 1 daily review: PnL, drawdown, stop-loss count, exposure peak. Use `PM_db_inspect.py`.
- **live_29** — If week 1 clean → ramp `trade_size` and `max_total_exposure_usd` incrementally. Max 2× per week.

---

## Risks (highest first)

1. **Unlimited allowance.** Polymarket UI typically grants unlimited; bot can theoretically spend full USDC balance. Mitigation: keep wallet balance = intended max exposure.
2. **`cheap_multiplier_default=4`** combined with cheap-side bias builds large position counts at low prices; one $0-resolution wipes that book.
3. **Public Polygon RPC throttling** can cause silent merge failures (positions accumulate without cleanup).
4. **Concurrent main.py** if `_scan_running_bots()` misses a process started outside project dir.
5. **DRY_RUN gating gaps** — if any code path bypasses the flag, real orders go out unexpectedly (live_11 covers).

## Open decisions (need user input)

- Starting wallet funding amount $?
- Starting `trade_size` $?
- Week-1 `max_total_exposure_usd` $?
- Phase B execution: do now, or after paper validation Phase D?
