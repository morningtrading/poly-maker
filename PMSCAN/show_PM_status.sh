#!/usr/bin/env bash
# show_PM_status.sh — Polymarket public account snapshot
# Usage: ./show_PM_status.sh [username|0xwallet] [hours]
#   default user:  morningtrading
#   hours:         optional time window for fills/stats (decimals OK, e.g. 0.5)
#
# Examples:
#   ./show_PM_status.sh                  # all activity for @morningtrading
#   ./show_PM_status.sh morningtrading 4 # only the last 4 hours
#   ./show_PM_status.sh 0xabc...  24     # last 24 hours for a wallet address
#
# Honors NO_COLOR=1 to disable ANSI colors. Output to non-TTY also disables color.

set -euo pipefail

ARG="${1:-morningtrading}"
WINDOW_HOURS="${2:-}"   # empty => no time cap
if [[ -n "$WINDOW_HOURS" && ! "$WINDOW_HOURS" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
    echo "second argument must be a positive number of hours; got: $WINDOW_HOURS" >&2
    exit 2
fi
UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# --- Resolve proxy wallet ---------------------------------------------------
if [[ "$ARG" =~ ^0x[0-9a-fA-F]{40}$ ]]; then
    WALLET="${ARG,,}"
    HANDLE="$ARG"
else
    HANDLE="${ARG#@}"
    curl -sfL -A "$UA" "https://polymarket.com/@${HANDLE}" -o "$TMP/profile.html"
    WALLET=$(grep -oE '"proxyWallet":"0x[0-9a-fA-F]+"' "$TMP/profile.html" \
             | head -1 | grep -oE '0x[0-9a-fA-F]+' | tr 'A-F' 'a-f')
    if [[ -z "$WALLET" ]]; then
        echo "could not find proxyWallet for @$HANDLE" >&2
        exit 1
    fi
fi

echo "Polymarket account:  @${HANDLE}"
echo "Proxy wallet:        ${WALLET}"
echo

# --- Fetch public data ------------------------------------------------------
curl -sfL "https://data-api.polymarket.com/positions?user=${WALLET}&sizeThreshold=0&limit=500&sortBy=CURRENT&sortDirection=DESC" \
     -o "$TMP/positions.json"
curl -sfL "https://data-api.polymarket.com/activity?user=${WALLET}&limit=500&type=TRADE" \
     -o "$TMP/activity.json"
curl -sfL "https://data-api.polymarket.com/activity?user=${WALLET}&limit=500&type=REDEEM" \
     -o "$TMP/redeems.json"

# --- Render -----------------------------------------------------------------
# TTY check MUST happen here, in the script's own shell — not inside $(...),
# which runs in a subshell whose stdout is a pipe, making `[[ -t 1 ]]` always
# return false.
if [[ ( -t 1 || -n "${FORCE_COLOR:-}" ) && -z "${NO_COLOR:-}" ]]; then
    COLOR=1
else
    COLOR=0
fi
export COLOR WINDOW_HOURS

POS="$TMP/positions.json" ACT="$TMP/activity.json" RED="$TMP/redeems.json" \
python3 <<'PY'
import json, os, sys, time, datetime

USE_COLOR = os.environ.get("COLOR") == "1"
def esc(c): return c if USE_COLOR else ""
R, G, Y, B, D, N = (esc("\033[31m"), esc("\033[32m"), esc("\033[33m"),
                    esc("\033[1m"),  esc("\033[2m"),  esc("\033[0m"))
LINK = esc("\033[4;36m")   # underline + cyan, applied to hyperlinked cells

NOW = time.time()

def pad(s, w):
    s = str(s)
    return (s[:w] if len(s) > w else s).ljust(w)

def hyperlink(s, url):
    """OSC 8 escape — clickable in iTerm2, GNOME Terminal, kitty, WezTerm,
    Windows Terminal, etc. Plain terminals just see the text."""
    if not USE_COLOR or not url:
        return s
    return f"\033]8;;{url}\033\\{s}\033]8;;\033\\"

def market_url(item):
    slug = item.get("eventSlug") or item.get("slug")
    return f"https://polymarket.com/event/{slug}" if slug else None

# Unicode box-drawing characters for a cleaner table.
BOX = {"h": "─", "v": "│",
       "tl": "┌", "tm": "┬", "tr": "┐",
       "ml": "├", "mm": "┼", "mr": "┤",
       "bl": "└", "bm": "┴", "br": "┘"}

def _padalign(text, w, align):
    text = str(text)
    if len(text) > w: text = text[:w]
    return text.rjust(w) if align == "R" else (text.center(w) if align == "C" else text.ljust(w))

def _render_cell(value, w, align):
    """Cell value can be a string, (text, color), or (text, color, url)."""
    if isinstance(value, tuple):
        if len(value) == 2:
            text, color = value; url = None
        else:
            text, color, url = value
        padded = _padalign(text, w, align)
        out = f"{color}{padded}{N}" if color else padded
        return hyperlink(out, url)
    return _padalign(value, w, align)

def table(header, rows, widths, align=None):
    """Render a Unicode-bordered table.
       align: list of 'L'/'R'/'C' per column; default all 'L'."""
    if align is None:
        align = ["L"] * len(widths)
    v   = f"{D}{BOX['v']}{N}"
    def hbar(L, M, R):
        return f"{D}{L}{M.join(BOX['h'] * (w + 2) for w in widths)}{R}{N}"
    def render_row(cells, is_header=False):
        out = []
        for i, c in enumerate(cells):
            if is_header:
                out.append(f"{B}{_padalign(c, widths[i], align[i])}{N}")
            else:
                out.append(_render_cell(c, widths[i], align[i]))
        return v + v.join(" " + c + " " for c in out) + v
    print(hbar(BOX["tl"], BOX["tm"], BOX["tr"]))
    print(render_row(header, is_header=True))
    print(hbar(BOX["ml"], BOX["mm"], BOX["mr"]))
    for r in rows:
        print(render_row(r))
    print(hbar(BOX["bl"], BOX["bm"], BOX["br"]))

def fmt_age(sec):
    if sec is None or sec < 0: return "—"
    m = int(sec // 60)
    if m < 1:   return "<1m"
    if m < 60:  return f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:  return f"{h}h{m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d{h:02d}h"

def parse_iso(s):
    if not s: return None
    s = str(s).replace("Z", "+00:00")
    for fmt in (s, s + "T00:00:00+00:00"):
        try:
            return datetime.datetime.fromisoformat(fmt)
        except (ValueError, TypeError):
            continue
    return None

def fmt_settles(end_iso):
    """Time until market endDate. 'past Xh' if already past scheduled end."""
    dt = parse_iso(end_iso)
    if dt is None: return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    sec = dt.timestamp() - NOW
    return fmt_age(sec) if sec >= 0 else f"past {fmt_age(-sec)}"

def pnl_color(v):
    if v is None: return ""
    if v >  0.005: return G
    if v < -0.005: return R
    return D

def trunc(s, n):
    s = str(s)
    return s if len(s) <= n else s[: n - 3] + "..."

# ---- Load --------------------------------------------------------------
positions = json.load(open(os.environ["POS"]))
activity  = json.load(open(os.environ["ACT"]))
redeems   = json.load(open(os.environ["RED"]))

# Categorize positions:
#   - "active":    market still trading, you hold tradable shares
#   - "unsettled": redeemable=true — market resolved, winnings pending claim
#   - "lost":      market resolved (curPrice ~ 0) and not redeemable — losing shares
def categorize(p):
    if p.get("redeemable"): return "unsettled"
    if p.get("curPrice", 0) <= 0.001 and p.get("size", 0) > 0: return "lost"
    return "active"
for p in positions: p["_cat"] = categorize(p)
active_pos    = [p for p in positions if p["_cat"] == "active"]
unsettled_pos = [p for p in positions if p["_cat"] == "unsettled"]
lost_pos      = [p for p in positions if p["_cat"] == "lost"]

# ---- Walk activity chronologically; weighted-avg cost per asset --------
# Polymarket's activity API returns newest-first. Many fills can share the
# same UNIX-second timestamp, so we trust API order (index N-1 = oldest)
# rather than re-sorting by timestamp — otherwise the running cum jumbles
# within same-second clusters.
#
# A "Ticket" (#PMTRxxxx) tags every fill belonging to one round-trip on an
# outcome: from the BUY that opens a position from zero through the SELL(s)
# that bring it back to zero. Polymarket has no native equivalent — closest
# concepts are transactionHash (per fill) and orderHash (per order), neither
# of which groups BUYs and SELLs together.
state        = {}             # asset -> {'size','avg','ticket'}
first_buy_ts = {}             # asset -> earliest BUY timestamp
N_act = len(activity)
events = [None] * N_act       # parallel to activity, in API order (newest first)
cum = 0.0
ticket_seq = 0

# Walk oldest -> newest. We do two passes per event so the ticket assigned
# to the trade aligns with the chronological event index.
ticket_for_event = [None] * N_act
for i in range(N_act - 1, -1, -1):  # chronological
    ev = activity[i]
    a  = ev["asset"]
    s  = state.setdefault(a, {"size": 0.0, "avg": 0.0, "ticket": None})
    if s["ticket"] is None:
        ticket_seq += 1
        s["ticket"] = f"#PMTR{ticket_seq:04d}"
    ticket_for_event[i] = s["ticket"]

    realized = None
    if ev["side"] == "BUY":
        first_buy_ts.setdefault(a, ev["timestamp"])
        new_size = s["size"] + ev["size"]
        if new_size > 1e-9:
            s["avg"] = (s["size"] * s["avg"] + ev["size"] * ev["price"]) / new_size
        s["size"] = new_size
    else:  # SELL
        realized = (ev["price"] - s["avg"]) * ev["size"]
        s["size"] -= ev["size"]
        if s["size"] < 1e-9:
            s["size"] = 0.0
        cum += realized

    events[i] = {**ev, "realized": realized, "cum_pnl": cum, "ticket": s["ticket"]}

    # Position closed -> next BUY on this asset will start a fresh ticket.
    if s["size"] == 0:
        s["ticket"] = None

total_realized = cum
# state[asset]['ticket'] now holds the live (open) ticket for any position
# whose size is still > 0, otherwise None.

# ---- Win/loss streak stats (chronological, oldest -> newest) -----------
def compute_stats(evlist):
    """evlist is in API order (newest-first); we walk it chronologically."""
    results = [e for e in reversed(evlist) if e["realized"] is not None]
    wins   = [r for r in results if r["realized"] >  0.005]
    losses = [r for r in results if r["realized"] < -0.005]
    n_closed = len(wins) + len(losses)
    realized = sum(r["realized"] for r in results)
    mw = ml = 0; cur_k = None; cur_l = 0
    for r in results:
        k = "W" if r["realized"] > 0.005 else ("L" if r["realized"] < -0.005 else None)
        if k is None: continue
        cur_l = cur_l + 1 if k == cur_k else 1
        cur_k = k
        if k == "W": mw = max(mw, cur_l)
        else:        ml = max(ml, cur_l)
    return {
        "realized":     realized,
        "n_closed":     n_closed,
        "wins":         wins,
        "losses":       losses,
        "win_rate":     (len(wins) / n_closed * 100) if n_closed else 0.0,
        "max_win":      mw,
        "max_loss":     ml,
        "cur_streak":   (cur_k, cur_l) if cur_k else (None, 0),
        "largest_win":  max(wins,   key=lambda r: r["realized"], default=None),
        "largest_loss": min(losses, key=lambda r: r["realized"], default=None),
    }

lifetime = compute_stats(events)

# ---- Positions tables --------------------------------------------------
def render_position_block(title, group):
    print(f"{B}=== {title} ==={N}")
    if not group:
        print("(none)")
        return 0.0, 0.0  # (sum currentValue, sum cashPnl, sum initialValue)... handled below
    group_sorted = sorted(group, key=lambda p: -p["currentValue"])
    rows = []
    tot_val = tot_pnl = tot_init = 0.0
    for p in group_sorted:
        opened = first_buy_ts.get(p["asset"])
        age = fmt_age(NOW - opened) if opened else "—"
        ticket = (state.get(p["asset"]) or {}).get("ticket") or "—"
        pnl_c = pnl_color(p["cashPnl"])
        url = market_url(p)
        rows.append((
            trunc(p["title"], 38),
            p["outcome"],
            f"{p['size']:.1f}",
            f"{p['avgPrice']:.3f}",
            f"{p['curPrice']:.3f}",
            f"{p['currentValue']:.2f}",
            (f"{p['cashPnl']:+.2f}",      pnl_c),
            (f"{p['percentPnl']:+.1f}%",  pnl_c),
            age,
            fmt_settles(p.get("endDate")),
            (ticket, LINK if url else "", url),
        ))
        tot_val  += p["currentValue"]
        tot_pnl  += p["cashPnl"]
        tot_init += p["initialValue"]
    table(
        ("Market", "Outcome", "Size", "Avg", "Cur", "Value $", "PnL $", "PnL %", "Age", "Settles", "Ticket"),
        rows,
        (38, 7, 6, 5, 5, 8, 8, 7, 8, 9, 9),
        align=["L","L","R","R","R","R","R","R","R","R","L"],
    )
    pct = (tot_pnl / tot_init * 100) if tot_init else 0.0
    print(f"  subtotal: {len(group_sorted)} positions   value ${tot_val:,.2f}"
          f"   P&L {pnl_color(tot_pnl)}${tot_pnl:+,.2f}{N}"
          f"   ({pct:+.1f}% on ${tot_init:,.2f} cost)")
    return tot_val, tot_pnl, tot_init

# Always render ACTIVE; render UNSETTLED / LOST only when non-empty.
val_a, pnl_a, init_a = render_position_block("ACTIVE POSITIONS", active_pos)
if unsettled_pos:
    print()
    print(f"{D}    redeemable=true — market resolved, winnings pending claim on Polymarket UI{N}")
    val_u, pnl_u, init_u = render_position_block("UNSETTLED POSITIONS (redeemable)", unsettled_pos)
else:
    val_u = pnl_u = init_u = 0.0
if lost_pos:
    print()
    print(f"{D}    market resolved against you — curPrice≈0, shares worthless{N}")
    val_l, pnl_l, init_l = render_position_block("RESOLVED-LOST POSITIONS", lost_pos)
else:
    val_l = pnl_l = init_l = 0.0

total_unrealized = pnl_a + pnl_u + pnl_l
total_initial    = init_a + init_u + init_l
total_value      = val_a + val_u + val_l
positions_n      = len(active_pos) + len(unsettled_pos) + len(lost_pos)

# Historical claim summary
if redeems:
    print()
    redeem_total = sum(r.get("usdcSize", 0) for r in redeems)
    print(f"  historical claims (REDEEM events): {len(redeems)} payouts "
          f"totaling ${redeem_total:,.2f}")

# ---- Recent fills table ------------------------------------------------
WINDOW_HOURS = os.environ.get("WINDOW_HOURS") or None
WINDOW_HOURS = float(WINDOW_HOURS) if WINDOW_HOURS else None

print()
fills_title = "ALL FILLS" if not WINDOW_HOURS else f"FILLS (last {WINDOW_HOURS:g}h)"
print(f"{B}=== {fills_title}  —  resting open orders require CLOB API auth ==={N}")
print(f"{D}    Cum PnL = running realized total AFTER each trade. "
      f"Top row = latest = Realized PnL in stats.{N}")

def fmt_signed(v):  # avoid "-0.00" / "+0.00" inconsistency for tiny rounding
    return "+0.00" if abs(v) < 0.005 else f"{v:+.2f}"

# Window filtering. We always *compute* realized PnL & avg cost over the full
# history (otherwise SELLs in-window would use a wrong cost basis), but we
# DISPLAY only fills inside the window, and we re-baseline Cum PnL so the
# top-row equals the sum of visible Realized values.
if WINDOW_HOURS:
    cutoff = NOW - WINDOW_HOURS * 3600
    events_disp = [e for e in events if e["timestamp"] >= cutoff]
    win_cum = 0.0
    for e in reversed(events_disp):                      # chronological
        if e["realized"] is not None:
            win_cum += e["realized"]
        e["cum_view"] = win_cum
else:
    events_disp = events
    for e in events_disp:
        e["cum_view"] = e["cum_pnl"]
if not events_disp:
    print("(none)")
else:
    rows = []
    for e in events_disp:
        age = fmt_age(NOW - e["timestamp"])
        realized = e["realized"]
        real_str   = "—"          if realized is None else fmt_signed(realized)
        real_color = D            if realized is None else pnl_color(realized)
        side_color = G if e["side"] == "BUY" else Y
        url = market_url(e)
        rows.append((
            age,
            (e["side"], side_color),
            e["outcome"],
            f"{e['price']:.3f}",
            f"{e['size']:.2f}",
            f"{e['usdcSize']:.2f}",
            (real_str, real_color),
            (fmt_signed(e["cum_view"]), pnl_color(e["cum_view"])),
            (e["ticket"], LINK if url else "", url),
            trunc(e["title"], 33),
        ))
    table(
        ("Age", "Side", "Outcome", "Px", "Size", "$", "Realized", "Cum PnL", "Ticket", "Market"),
        rows,
        (8, 4, 8, 5, 7, 7, 9, 9, 9, 33),
        align=["R","C","L","R","R","R","R","R","L","L"],
    )
    # Verification footer — makes the relationship between Realized column and stats explicit.
    visible_realized = sum(e["realized"] for e in events_disp if e["realized"] is not None)
    n_visible_sells  = sum(1 for e in events_disp if e["realized"] is not None)
    if WINDOW_HOURS:
        print(f"    window realized: {pnl_color(visible_realized)}"
              f"{fmt_signed(visible_realized)}{N} across {n_visible_sells} sells in last "
              f"{WINDOW_HOURS:g}h   "
              f"(lifetime: {pnl_color(total_realized)}{fmt_signed(total_realized)}{N} "
              f"across {sum(1 for e in events if e['realized'] is not None)} sells)")
    else:
        print(f"    sum of visible Realized: {pnl_color(visible_realized)}"
              f"{fmt_signed(visible_realized)}{N} across {n_visible_sells} sells   "
              f"(top-row Cum PnL = total Realized PnL = "
              f"{pnl_color(total_realized)}{fmt_signed(total_realized)}{N})")
    if len(activity) >= 500:
        print(f"    {Y}note: activity API returned 500 rows — older history may be truncated{N}")

# ---- Stats summary -----------------------------------------------------
def cval(v, sign=True, suf=""):
    if sign and abs(v) < 0.005: s = "+0.00" + suf
    else: s = (f"{v:+.2f}" if sign else f"{v:.2f}") + suf
    return f"{pnl_color(v)}{s}{N}"

def render_stats(label, st):
    print(f"{B}=== STATS{label} ==={N}")
    print(f"  Realized PnL:    {cval(st['realized'])}   from {st['n_closed']} closed trades")
    if not label:  # only print position-derived totals on the lifetime block
        print(f"  Unrealized PnL:  {cval(total_unrealized)}   from {len(positions)} open positions")
        total_pnl = st["realized"] + total_unrealized
        print(f"  Total PnL:       {cval(total_pnl)}")
    if st["n_closed"]:
        wr = st["win_rate"]; w = st["wins"]; l = st["losses"]
        win_c = G if wr >= 50 else (R if wr < 40 else Y)
        print(f"  Win rate:        {win_c}{len(w)}/{st['n_closed']} ({wr:.1f}%){N}"
              f"   losses: {len(l)}")
        print(f"  Max win streak:  {G}{st['max_win']}W{N}    "
              f"max loss streak: {R}{st['max_loss']}L{N}")
        if st["cur_streak"][0]:
            c = G if st["cur_streak"][0] == "W" else R
            print(f"  Current streak:  {c}{st['cur_streak'][1]}{st['cur_streak'][0]}{N}")
        if st["largest_win"]:
            print(f"  Largest win:     {cval(st['largest_win']['realized'])}  "
                  f"on \"{trunc(st['largest_win']['title'], 60)}\"")
        if st["largest_loss"]:
            print(f"  Largest loss:    {cval(st['largest_loss']['realized'])}  "
                  f"on \"{trunc(st['largest_loss']['title'], 60)}\"")
    else:
        print("  (no closed trades in this set)")

print()
render_stats("", lifetime)
if WINDOW_HOURS:
    print()
    render_stats(f" — last {WINDOW_HOURS:g}h", compute_stats(events_disp))
PY
