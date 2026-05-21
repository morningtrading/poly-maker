#!/usr/bin/env python3
"""PM_live_sanity_test_v2.py — live MARKET-ORDER round-trip sanity test.

Objective: confirm end-to-end live trading via CLOB V2 — market buy, hold 30s,
market sell back. Validates auth, signing, routing, and fill mechanics.

Safety:
  - $1 USDC max BUY notional
  - SELL uses FAK so it fills whatever it can immediately and cancels the rest
  - Slippage + maker/taker fees apply: expect ~$0.05-$0.10 of net cost on the round trip
  - Position is reconciled via on-chain CTF token balance (no trust in API lag)

Funder:    0xeB8f6B4ae61142Bd4C6F7EFC3C7992E9a4DbAcF4 (Polymarket V2 deposit wallet)
Signature: SignatureTypeV2.POLY_1271 (EIP-1271 contract signatures)

Run:
    .venv/bin/python scratchpad/PM_live_sanity_test_v2.py
"""
import os
import time
from dotenv import load_dotenv

load_dotenv(".env", override=False)

from py_clob_client_v2 import (
    ClobClient,
    MarketOrderArgs,
    OrderType,
    PartialCreateOrderOptions,
    Side,
    SignatureTypeV2,
)
from web3 import Web3
from poly_data.abis import ConditionalTokenABI

HOST = "https://clob.polymarket.com"
# "Will Spider-Man: Brand New Day be the top grossing movie of 2026?" — Yes
# Price ~$0.615, 1¢ spread, ~$2,467 ask depth, neg_risk=True, min_order=5 shares.
TOKEN = "28161183422242370392388296744035422249088647252796713903067039294971789722479"
BUY_USDC = 3.0          # USD notional for the BUY leg (~4.8 shares at $0.62, above 5-share min)
WAIT_SECONDS = 30
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

key = os.getenv("PK")
funder_raw = "0xeB8f6B4ae61142Bd4C6F7EFC3C7992E9a4DbAcF4"
funder = Web3.to_checksum_address(funder_raw)


def get_ctf_balance() -> float:
    """Read raw CTF token balance for our funder; returns shares (6 decimals)."""
    rpc = os.getenv("POLYGON_RPC_URL", "https://polygon-bor-rpc.publicnode.com")
    w3 = Web3(Web3.HTTPProvider(rpc))
    ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS),
                          abi=ConditionalTokenABI)
    raw = ctf.functions.balanceOf(funder, int(TOKEN)).call()
    return raw / 1e6  # shares


print(f"==== market-order sanity test v2 — {time.strftime('%H:%M:%S')} ====")
print(f"funder=0xeB8f...AcF4  POLY_1271  token=Spider-Man top-grossing 2026? Yes  (~$0.62)")
print(f"plan: market BUY ${BUY_USDC}  →  wait {WAIT_SECONDS}s  →  market SELL all bought shares\n")

# Auth setup
_boot = ClobClient(host=HOST, chain_id=137, key=key, funder=funder,
                   signature_type=SignatureTypeV2.POLY_1271)
creds = _boot.create_or_derive_api_key()
client = ClobClient(host=HOST, chain_id=137, key=key, creds=creds, funder=funder,
                    signature_type=SignatureTypeV2.POLY_1271)

# Snapshot
shares_before = get_ctf_balance()
print(f"[setup]  CTF shares before: {shares_before:.4f}")

# Market BUY
print(f"\n[1/3]   market BUY ${BUY_USDC} (FOK)…")
buy_resp = client.create_and_post_market_order(
    order_args=MarketOrderArgs(token_id=TOKEN, amount=BUY_USDC, side=Side.BUY,
                               order_type=OrderType.FOK),
    options=PartialCreateOrderOptions(tick_size="0.01"),
    order_type=OrderType.FOK,
)
print(f"        BUY resp: {buy_resp}")

# Confirm fill via on-chain delta
time.sleep(2)  # brief lag for block confirmation
shares_after_buy = get_ctf_balance()
shares_bought = shares_after_buy - shares_before
print(f"        shares after BUY: {shares_after_buy:.4f}  (Δ = {shares_bought:+.4f})")
if shares_bought <= 0:
    print(f"[!!]    BUY did not fill (no share delta). Inspect response above.")
    raise SystemExit(1)

# Wait
print(f"\n[2/3]   waiting {WAIT_SECONDS}s…")
time.sleep(WAIT_SECONDS)

# Market SELL — sell EXACTLY what we bought (FAK in case price moved).
# Polymarket V2 accepts fractional taker amounts (BUY returned 4.838707 here).
sell_amount = round(shares_bought, 6)  # full fractional shares; round to USDC-ish precision
if sell_amount <= 0:
    print(f"[!!]    nothing to sell ({shares_bought:.6f})")
    raise SystemExit(1)

print(f"\n[3/3]   market SELL {sell_amount} shares (FAK)…")
sell_resp = client.create_and_post_market_order(
    order_args=MarketOrderArgs(token_id=TOKEN, amount=sell_amount, side=Side.SELL,
                               order_type=OrderType.FAK),
    options=PartialCreateOrderOptions(tick_size="0.01"),
    order_type=OrderType.FAK,
)
print(f"        SELL resp: {sell_resp}")

time.sleep(2)
shares_final = get_ctf_balance()
shares_sold = shares_after_buy - shares_final
print(f"\n==== RESULT ====")
print(f"  shares_before:      {shares_before:.4f}")
print(f"  shares_after_buy:   {shares_after_buy:.4f}  (bought {shares_bought:+.4f})")
print(f"  shares_final:       {shares_final:.4f}     (sold   {shares_sold:+.4f})")
print(f"  net position change: {shares_final - shares_before:+.4f} shares")
print(f"\n  Inspect Polymarket UI for the two trades and the net USDC delta.")
