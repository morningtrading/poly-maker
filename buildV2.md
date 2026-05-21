# Build V2 — Polymarket profitable strategies research + next-step prompts

> Generated 2026-05-21 at end of session.
> Context: V1 bot stopped after backtester proved MM/mean-reversion at $10 caps
> is structurally unprofitable. This document captures research for V2 direction.

Based on web research across academic papers, news coverage, and practitioner
blogs, here's what actually makes money on Polymarket in 2026, scored against
the current setup (backtester + v2 SDK + ~$278 wallet).

---

## What's documented as profitable

| Strategy | Documented profit | Capital needed | Speed | Feasible for us? |
|---|---|---|---|---|
| **Crypto-spot lag arbitrage** (BTC/ETH/SOL 15-min markets) | $313 → $414K / month for one bot | $300+ | **Sub-100ms required** (73% of profits go to fastest) | ⚠ Maybe — depends on RPC + latency |
| **ML ensemble (news + social signals)** | $2.2M in 2 months (Igor Mikerin profile) | Variable | Seconds-minutes | ⚠ Big eng lift; needs LLM + news pipeline |
| **Cross-platform arbitrage** (Polymarket ↔ Kalshi) | $39.5M cumulative across all participants since 2024 | $500+ | Seconds | ✅ Feasible — both have APIs |
| **Liquidity rewards harvesting at scale** | $30-80/day on $10k (down from $200-300 in 2024) | **$10k+ required** for >$1k orders that qualify | Slow | ❌ Need ~$10k capital |
| **Information edge** (Théo's neighbor-polling) | $80M on Trump 2024 | Whale-scale | Days/weeks | ❌ One-off; not systematic |
| **Sports market makers** (post-Feb 2026 maker rebate pilot) | "Double income stream" | $1k+ | Minutes | ⚠ Adverse selection risk on game outcomes |
| **Correlation/triangle arbitrage** (e.g., "team wins championship" ≥ "team wins semi") | Small but persistent | Any | Seconds | ✅ Feasible offline analysis |

**Key reality check from the research:** average arbitrage opportunity now lasts
**2.7 seconds**. Easy money is gone. Anything that worked in 2024 mostly doesn't
in 2026.

---

## Ranking for OUR setup (small capital, no HFT infra)

| Rank | Strategy | Why it fits |
|---|---|---|
| **1** | **Cross-platform arb (Polymarket ↔ Kalshi)** | Doesn't need sub-100ms; ~1-4% edges; we already have v2 SDK |
| **2** | **Correlation/combinatorial arb** | Mathematical edge, no speed race, can run from polymb.db |
| **3** | **ML signal trading** (small scale) | Big edge potential, but big build cost |
| **4** | **Crypto-spot lag arb** | Highest theoretical edge, but **speed-bound** — need <100ms infra we don't have |
| **5** | Avoid liquidity rewards | Requires $10k+ capital we don't have |

---

## Concrete prompts for next sessions

These are copy-pastable into a fresh Claude Code session. Each is scoped to one
strategy and references existing tooling (bck_*, v2 SDK, polymb.db).

### Prompt 1 — Cross-platform Polymarket↔Kalshi arbitrage scout

```
I want to build a cross-platform arbitrage scout between Polymarket and Kalshi
following the pattern in https://github.com/ImMike/polymarket-arbitrage.

Constraints:
- Reuse our existing bck_ingest.py pattern (read-only, isolated, bck_* prefix)
- Output: a new file bck_xplat_arb.py that finds correlated markets
  (matching by event/title fuzzy similarity) and flags when their implied
  probabilities diverge by > $0.02 after fees
- Don't trade yet — just report. We'll wire trading after validation.
- Kalshi API auth needed; ask me for credentials before fetching
- Output the top 20 divergences daily as a CSV in bck_results/

Start by proposing the schema for matching markets across platforms,
then ask me to confirm before pulling data.
```

### Prompt 2 — Correlation/triangle arbitrage on Polymarket alone

```
I want to find combinatorial arbitrage opportunities within Polymarket itself.
The canonical example: if "Team A wins championship" is at 60¢, then
"Team A wins semi-final" must be at least 60¢ (you can't win the championship
without winning the semi).

Build bck_combo_arb.py that:
1. Reads our existing bck_data/markets.db
2. Identifies markets in the same event tree (via Polymarket's `events` API
   that groups related markets)
3. For each tree, computes the logical implication constraints
4. Reports inconsistencies > $0.01 after merge fees ($0.005 estimated)

Reference: arxiv 2508.03474 (Unravelling the Probabilistic Forest).
```

### Prompt 3 — Crypto-spot lag arbitrage exploration

```
The most profitable Polymarket bots in 2026 exploit price lag between
Polymarket BTC/ETH/SOL 15-min "will X go up by Y%" markets and confirmed
spot prices on Binance/Coinbase.

I want to evaluate if WE can play this. Build bck_spot_lag.py that:
1. Ingests 30 days of trades from all BTC/ETH/SOL Polymarket markets
2. Pairs them with historical Binance spot prices via their public API
3. For each Polymarket trade, computes: what was the Binance spot price
   30s before / 30s after the trade?
4. Reports: how often is Polymarket >100ms behind spot, and by how much?

The output tells us if our infrastructure (Alchemy RPC, US-east latency
to Polymarket) is fast enough to compete. If average gap < 100ms,
we abandon this. If the typical lag is > 1s, we have an edge.
```

### Prompt 4 — ML signal generator from public data

```
Build a lightweight ML signal generator for Polymarket markets:

1. New file bck_ml_signal.py
2. Inputs: market title + last 24h trade history + (optional) recent news headlines
3. Output: an "edge score" — bot's estimate of fair price vs current market price
4. Approach: start with simple features (volume momentum, price acceleration,
   spread), add a Logistic Regression or LightGBM target = "did the price
   converge to 0 or 1 within 30 days?"
5. Train on our existing 1.7M trade dataset
6. Walk-forward validation: train on first 20 days, test on last 10

Goal: prove ML can predict resolution outcomes better than current market
prices. Threshold for "useful": > 55% accuracy on holdout closed markets.
```

### Prompt 5 — Specialized vertical bot (pick one domain)

```
Most profitable Polymarket strategies specialize. I want to pick ONE
vertical and build a focused bot:

A) Sports markets (with maker rebate pilot in effect since Feb 2026)
B) Crypto-price markets (BTC/ETH/SOL 1-day / 1-week)
C) Economic event markets (Fed, inflation, GDP)
D) Recurring weather markets (mean-reverting by physics)

For the vertical I pick:
1. Identify ~10-20 markets that fit the niche over the last 30 days
2. Compute IF a simple specialized strategy (e.g., "buy NO when YES > 0.95
   for crypto markets resolving in <6h") would have been profitable
3. Use our bck_replay.py as the engine; just feed it a filtered universe
4. Report PnL + win rate per vertical
```

### Prompt 6 — Reality-check the academic literature

```
Polymarket strategies in academic literature claim X but practitioners
report Y. Help me reconcile:

1. Read arxiv.org/abs/2508.03474 ("Unravelling the Probabilistic Forest")
   and extract: which arbitrage types do they find profitable, what's the
   median edge size, what % of opportunities last < 1 second?
2. Compare to our backtest findings (60+ combos, all unprofitable)
3. Propose: which academic finding is most actionable for a $278-wallet bot?

Then write a 1-page memo: "What we should and shouldn't bother trying."
```

### Prompt 7 — Resume bot operations under a different hypothesis

```
We stopped the live bot after backtest showed mean-reversion MM doesn't work
at $10 caps. Before designing a new strategy, run an experiment:

1. Resume the bot in DRY_RUN
2. Lower the TP threshold to 1% (was 2-3% in our sweeps)
3. Tighten the stop to 5% (was -10 to -15)
4. Reduce the universe to ONLY the most liquid 10 markets
5. Run 24h paper, compare to live data we collected earlier

Hypothesis being tested: if we operate ONLY in the very most liquid markets
with very tight stops, can we get to 50%+ win rate AND non-negative PnL?

Use bck_replay.py first to predict the result before going live.
```

---

## Opinionated recommendation

If you only do ONE thing: **Prompt 1 (Polymarket ↔ Kalshi arb)**.

Reasoning:
- Documented profitable space ($39.5M cumulative since 2024)
- Doesn't need sub-100ms speed — opportunities last seconds-to-minutes
- Reuses our existing v2 SDK + bck_ingest pattern
- Failure mode is bounded: just doesn't find anything → no money lost
- Could be ready to test in 1-2 days

If you want a longer-term play: **Prompt 4 (ML signal generator)**. Highest
ceiling but biggest build cost.

---

## Context from V1 that should inform V2

Things V1 built that V2 should reuse:
- `bck_ingest.py` — Polymarket public-API ingest pattern (read-only, isolated)
- `bck_replay.py` — event-driven replay engine (can drive any strategy)
- `bck_filter.py` — YAML-driven selection_filters
- `bck_sweep.py` — parameter grid search harness
- `PM_reconcile.py` — health-check framework (R1..R6)
- `PMSCAN/show_PM_status.sh` — human-readable PnL view
- `PM_menupm.sh` — single ops entrypoint with kill switch
- v2 SDK migration in `poly_data/polymarket_client.py` — `SignatureTypeV2.POLY_1271`
- $10/market hard cap pattern in `standard_config.yaml`
- Bot-internal isolation contract — all `bck_*` files never import bot code

V1 strategy findings that V2 should respect:
- 80% win rate is achievable on Polymarket mean reversion BUT loss size kills
  asymmetric TP/SL configs. → V2 needs symmetric or trend-aligned PnL profile.
- Universe filtering (volume, time-to-resolution) is NOT the lever — strategy
  structure is. → V2 should look at NEW edge sources, not tighter universe.
- Maker rewards aren't viable below $1k order sizes. → V2 either gives up on
  rewards OR scales capital first.
- Adverse selection on resting maker bids is the underlying killer in MM
  approaches. → V2 should either avoid resting bids (be a taker) OR add a
  signal layer that withdraws bids when an informed trader appears.

---

## Sources

- [Best Polymarket Strategies in 2026 — TradingVPS](https://tradingvps.io/best-polymarket-trading-strategies-in-2026/)
- [Beyond Simple Arbitrage: 4 Polymarket Strategies Bots Actually Profit From in 2026](https://medium.com/illumination/beyond-simple-arbitrage-4-polymarket-strategies-bots-actually-profit-from-in-2026-ddacc92c5b4f)
- [Arbitrage Bots Dominate Polymarket With Millions in Profits as Humans Fall Behind — Yahoo Finance](https://finance.yahoo.com/news/arbitrage-bots-dominate-polymarket-millions-100000888.html)
- [Polymarket Liquidity Rewards — official docs](https://docs.polymarket.com/polymarket-learn/trading/liquidity-rewards)
- [Polymarket Maker Rebates Program — official docs](https://docs.polymarket.com/market-makers/maker-rebates)
- [Polymarket Strategies: 2026 Guide for Profitable Trading — cryptonews](https://cryptonews.com/cryptocurrency/polymarket-strategies/)
- [Prediction Market Arbitrage: Cross-Market, Cross-Platform — Polyguana](https://polyguana.com/learn/polymarket-arbitrage)
- [Polymarket-Kalshi arbitrage bot — GitHub (MrFadiAi)](https://github.com/MrFadiAi/Polymarket-bot)
- [Polymarket/Kalshi 10K+ market arbitrage bot — GitHub (ImMike)](https://github.com/ImMike/polymarket-arbitrage)
- [Unravelling the Probabilistic Forest: Arbitrage in Prediction Markets — arXiv 2508.03474](https://arxiv.org/pdf/2508.03474)
- [How a French "whale" made $80M on Trump 2024 — CBS News](https://www.cbsnews.com/news/french-whale-made-over-80-million-on-polymarket-betting-on-trump-election-win-60-minutes/)
- [Building a Polymarket BTC 15-Min Trading Bot with NautilusTrader — Medium](https://medium.com/@aulegabriel381/the-ultimate-guide-building-a-polymarket-btc-15-minute-trading-bot-with-nautilustrader-ef04eb5edfcb)
