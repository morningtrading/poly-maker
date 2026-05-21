#!/usr/bin/env bash
# PM_menupm.sh — poly-maker control menu (bot, dashboard, stats reset, git push).
#
# Objective : single entrypoint to operate the bot without remembering commands.
# Rational  : reduces operator error (wrong cwd, dual bot launch, accidental push).
# Deps      : bash, pgrep, sqlite3, git, .venv at ./.venv, PM_bot_control.py.
# Output    : interactive menu; status header refreshed every loop.
# Test      : `bash PM_menupm.sh` then choose 0 to exit.

set -u

# ---- config (menu_cfg_01) -----------------------------------------------------
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="${PROJECT_DIR}/.venv/bin/python"
DB_FILE="${PROJECT_DIR}/polymb.db"
PID_FILE="${PROJECT_DIR}/.pm_bot.pid"
DASH_PORT="${PM_DASH_PORT:-8501}"
DASH_LOG="${PROJECT_DIR}/logs/PM_dashboard_$(date +%Y%m%d).log"
DASH_PID_FILE="${PROJECT_DIR}/.pm_dashboard.pid"
RECON_PID_FILE="${PROJECT_DIR}/.pm_reconcile.pid"
RECON_LOG="${PROJECT_DIR}/logs/PM_reconcile_$(date +%Y%m%d).log"
RECON_INTERVAL_S="${PM_RECON_INTERVAL_S:-300}"
POSITIONS_DIR="${PROJECT_DIR}/positions"
DATA_DIR="${PROJECT_DIR}/data"
LOG_DIR="${PROJECT_DIR}/logs"

# ---- colours (menu_cfg_02) ----------------------------------------------------
C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YLW=$'\033[33m'
C_CYN=$'\033[36m'; C_BLD=$'\033[1m'; C_RST=$'\033[0m'

mkdir -p "${LOG_DIR}"

# ---- menu_fn_01 : bot status (via PM_bot_control.py) --------------------------
bot_status() {
  if [[ -x "${VENV_PY}" ]]; then
    "${VENV_PY}" -c "import sys; sys.path.insert(0,'${PROJECT_DIR}'); \
      import PM_bot_control as c; s=c.status(); \
      print('RUNNING' if s.get('running') else 'STOPPED', s.get('pid') or '')" 2>/dev/null
  else
    pgrep -f "python.*main.py" >/dev/null && echo "RUNNING ?" || echo "STOPPED"
  fi
}

# ---- menu_fn_02 : dashboard status -------------------------------------------
dash_status() {
  local pid
  pid="$(pgrep -f "streamlit run.*PM_dashboard.py" | head -1)"
  [[ -n "${pid}" ]] && echo "RUNNING ${pid}" || echo "STOPPED"
}

# ---- menu_fn_02b : reconcile-loop status -------------------------------------
recon_status() {
  if [[ -f "${RECON_PID_FILE}" ]]; then
    local pid
    pid="$(cat "${RECON_PID_FILE}" 2>/dev/null)"
    if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
      echo "RUNNING ${pid}"
      return
    fi
    rm -f "${RECON_PID_FILE}"  # stale
  fi
  echo "STOPPED"
}

# ---- menu_fn_03 : header ------------------------------------------------------
print_header() {
  local bs ds rs bcol dcol rcol
  bs="$(bot_status)"; ds="$(dash_status)"; rs="$(recon_status)"
  [[ "${bs}" == RUNNING* ]] && bcol="${C_GRN}" || bcol="${C_RED}"
  [[ "${ds}" == RUNNING* ]] && dcol="${C_GRN}" || dcol="${C_RED}"
  [[ "${rs}" == RUNNING* ]] && rcol="${C_GRN}" || rcol="${C_RED}"
  clear
  echo "${C_BLD}${C_CYN}=== poly-maker control menu (menupm) ===${C_RST}"
  echo "  project    : ${PROJECT_DIR}"
  echo "  bot        : ${bcol}${bs}${C_RST}"
  echo "  dashboard  : ${dcol}${ds}${C_RST}  (port ${DASH_PORT})"
  echo "  reconcile  : ${rcol}${rs}${C_RST}  (interval ${RECON_INTERVAL_S}s)"
  echo "----------------------------------------------------"
}

# ---- menu_fn_04 : start bot --------------------------------------------------
start_bot() {
  echo "${C_YLW}[menu_04] starting bot via PM_bot_control.start()${C_RST}"
  "${VENV_PY}" -c "import sys; sys.path.insert(0,'${PROJECT_DIR}'); \
    import PM_bot_control as c; print(c.start())"
}

# ---- menu_fn_05 : stop bot ---------------------------------------------------
stop_bot() {
  echo "${C_YLW}[menu_05] stopping bot (SIGTERM, 10s grace)${C_RST}"
  "${VENV_PY}" -c "import sys; sys.path.insert(0,'${PROJECT_DIR}'); \
    import PM_bot_control as c; print(c.stop())"
}

# ---- menu_fn_06 : start dashboard --------------------------------------------
start_dashboard() {
  if [[ "$(dash_status)" == RUNNING* ]]; then
    echo "${C_YLW}[menu_06] dashboard already running${C_RST}"; return
  fi
  echo "${C_YLW}[menu_06] starting dashboard on :${DASH_PORT} (log: ${DASH_LOG})${C_RST}"
  ( cd "${PROJECT_DIR}" && \
    nohup "${VENV_PY}" -m streamlit run PM_dashboard.py \
        --server.port "${DASH_PORT}" --server.headless true \
        >>"${DASH_LOG}" 2>&1 & echo $! > "${DASH_PID_FILE}" )
  sleep 1
  echo "  pid=$(cat "${DASH_PID_FILE}" 2>/dev/null)  url=http://localhost:${DASH_PORT}"
}

# ---- menu_fn_07 : stop dashboard ---------------------------------------------
stop_dashboard() {
  local pids
  pids="$(pgrep -f "streamlit run.*PM_dashboard.py")"
  if [[ -z "${pids}" ]]; then
    echo "${C_YLW}[menu_07] dashboard not running${C_RST}"; return
  fi
  echo "${C_YLW}[menu_07] killing dashboard pids: ${pids}${C_RST}"
  kill ${pids} 2>/dev/null
  sleep 1
  pids="$(pgrep -f "streamlit run.*PM_dashboard.py")"
  [[ -n "${pids}" ]] && kill -9 ${pids} 2>/dev/null
  rm -f "${DASH_PID_FILE}"
}

# ---- menu_fn_08 : reset stats (positions, trades, summary) -------------------
# Destructive: backs up DB first, requires typed RESET, refuses while bot up.
reset_stats() {
  if [[ "$(bot_status)" == RUNNING* ]]; then
    echo "${C_RED}[menu_08] refusing: stop the bot first${C_RST}"; return
  fi
  echo "${C_RED}This will delete:${C_RST}"
  echo "   - ${POSITIONS_DIR}/*.json"
  echo "   - ${DATA_DIR}/*.csv"
  echo "   - sqlite table 'summary' rows (if present)"
  echo "A timestamped backup of polymb.db will be created."
  read -r -p "Type RESET to confirm: " ans
  if [[ "${ans}" != "RESET" ]]; then
    echo "${C_YLW}[menu_08] aborted${C_RST}"; return
  fi
  local ts; ts="$(date +%Y%m%d_%H%M%S)"
  if [[ -f "${DB_FILE}" ]]; then
    cp -p "${DB_FILE}" "${DB_FILE}.bak.${ts}"
    echo "  backup -> ${DB_FILE}.bak.${ts}"
    sqlite3 "${DB_FILE}" "DELETE FROM summary;" 2>/dev/null && echo "  summary table cleared"
  fi
  [[ -d "${POSITIONS_DIR}" ]] && find "${POSITIONS_DIR}" -maxdepth 1 -name '*.json' -delete -print
  [[ -d "${DATA_DIR}" ]] && find "${DATA_DIR}" -maxdepth 1 -name '*.csv' -delete -print
  echo "${C_GRN}[menu_08] reset complete${C_RST}"
}

# ---- menu_fn_09 : git push ---------------------------------------------------
git_push() {
  ( cd "${PROJECT_DIR}" && \
    echo "${C_CYN}--- git status ---${C_RST}" && git status -sb && \
    echo "${C_CYN}--- ahead/behind ---${C_RST}" && git log --oneline @{u}..HEAD 2>/dev/null )
  read -r -p "Push current branch to origin? Type YES: " ans
  if [[ "${ans}" != "YES" ]]; then
    echo "${C_YLW}[menu_09] aborted${C_RST}"; return
  fi
  ( cd "${PROJECT_DIR}" && git push )
}

# ---- menu_fn_11 : reconcile (one-shot) ---------------------------------------
# WHY: out-of-band check that Polymarket's view matches what we expect.
# Catches orphan positions (e.g. Mariners 0.26sh), dead markets in
# selected_markets, bot heartbeat, and network-latency drift. Read-only —
# never modifies bot state.
recon_once() {
  echo "${C_CYN}[menu_11] reconcile (one-shot)${C_RST}"
  ( cd "${PROJECT_DIR}" && "${VENV_PY}" PM_reconcile.py )
}

# ---- menu_fn_12 : reconcile loop toggle (start ⇄ stop) -----------------------
# WHY: a continuous background check at PM_RECON_INTERVAL_S (default 300s)
# so drift is caught within minutes rather than discovered hours later.
recon_loop_toggle() {
  if [[ "$(recon_status)" == RUNNING* ]]; then
    local pid
    pid="$(cat "${RECON_PID_FILE}")"
    echo "${C_YLW}[menu_12] stopping reconcile loop (PID ${pid})${C_RST}"
    kill "${pid}" 2>/dev/null
    sleep 1
    kill -0 "${pid}" 2>/dev/null && kill -9 "${pid}" 2>/dev/null
    rm -f "${RECON_PID_FILE}"
    echo "${C_GRN}[menu_12] stopped${C_RST}"
  else
    echo "${C_YLW}[menu_12] starting reconcile loop "
    echo "  interval: ${RECON_INTERVAL_S}s  (override via PM_RECON_INTERVAL_S env var)"
    echo "  log:      ${RECON_LOG}${C_RST}"
    ( cd "${PROJECT_DIR}" && \
      nohup "${VENV_PY}" PM_reconcile.py --loop "${RECON_INTERVAL_S}" \
          >>"${RECON_LOG}" 2>&1 & echo $! > "${RECON_PID_FILE}" )
    sleep 1
    local pid
    pid="$(cat "${RECON_PID_FILE}" 2>/dev/null)"
    echo "${C_GRN}[menu_12] started (PID ${pid})${C_RST}"
  fi
}

# ---- menu_fn_99 : KILL SWITCH (emergency stop + cancel all open orders) ------
# WHY a separate item from "Stop bot": stop_bot only ends the process.
# Open orders posted by the bot persist on Polymarket's CLOB until cancelled
# server-side and can still get filled. In an emergency you want both:
# kill the process AND yank all quotes off the book.
# Does NOT liquidate positions — that's intentional; positions are static risk,
# orders are leaking risk.
kill_switch() {
  echo "${C_RED}${C_BLD}[menu_99] KILL SWITCH${C_RST}"
  echo "  - SIGTERM bot"
  echo "  - cancel ALL open orders on Polymarket (positions are NOT closed)"
  read -r -p "Type KILL to confirm: " ans
  if [[ "${ans}" != "KILL" ]]; then
    echo "${C_YLW}[menu_99] aborted${C_RST}"; return
  fi
  echo "${C_YLW}[menu_99] stopping bot…${C_RST}"
  ( cd "${PROJECT_DIR}" && "${VENV_PY}" -c \
    "import PM_bot_control as c; print(c.stop())" )
  echo "${C_YLW}[menu_99] cancelling all open orders on Polymarket…${C_RST}"
  ( cd "${PROJECT_DIR}" && "${VENV_PY}" - <<'PY'
import os
from dotenv import load_dotenv
load_dotenv(".env", override=False)
from py_clob_client_v2 import ClobClient, SignatureTypeV2
from web3 import Web3
key = os.getenv("PK")
funder = Web3.to_checksum_address(os.getenv("BROWSER_ADDRESS"))
c0 = ClobClient(host="https://clob.polymarket.com", chain_id=137, key=key,
                funder=funder, signature_type=SignatureTypeV2.POLY_1271)
creds = c0.create_or_derive_api_key()
client = ClobClient(host="https://clob.polymarket.com", chain_id=137, key=key,
                    creds=creds, funder=funder, signature_type=SignatureTypeV2.POLY_1271)
before = client.get_open_orders()
print(f"  open orders before: {len(before)}")
if before:
    resp = client.cancel_all()
    print(f"  canceled: {len(resp.get('canceled', []))}")
    not_c = resp.get("not_canceled") or {}
    if not_c:
        print(f"  not_canceled: {len(not_c)}")
after = client.get_open_orders()
print(f"  open orders after:  {len(after)}")
PY
  )
  echo "${C_GRN}[menu_99] kill switch complete${C_RST}"
}

# ---- menu_fn_10 : tail bot log -----------------------------------------------
tail_log() {
  local f
  f="$(ls -1t "${LOG_DIR}"/PM_main_*.log 2>/dev/null | head -1)"
  if [[ -z "${f}" ]]; then echo "no PM_main_*.log found"; return; fi
  echo "${C_CYN}tailing ${f} (Ctrl-C to stop)${C_RST}"
  tail -n 50 -f "${f}"
}

# ---- menu_fn_13 : tail reconcile log -----------------------------------------
tail_recon_log() {
  local f
  f="$(ls -1t "${LOG_DIR}"/PM_reconcile_*.log 2>/dev/null | head -1)"
  if [[ -z "${f}" ]]; then echo "no PM_reconcile_*.log found"; return; fi
  echo "${C_CYN}tailing ${f} (Ctrl-C to stop)${C_RST}"
  tail -n 50 -f "${f}"
}

# ---- menu_fn_14 : Polymarket account snapshot (PMSCAN/show_PM_status.sh) -----
# WHY: human-readable PnL portrait (positions, fills, win-rate, streaks) from
# Polymarket's public endpoints. No PK / no auth required. Optional time window.
pm_status() {
  local script="${PROJECT_DIR}/PMSCAN/show_PM_status.sh"
  if [[ ! -x "${script}" ]]; then
    echo "${C_RED}[menu_14] PMSCAN/show_PM_status.sh not found or not executable${C_RST}"
    return
  fi
  read -r -p "Hours window (blank = all activity): " hours
  if [[ -n "${hours}" && ! "${hours}" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
    echo "${C_RED}hours must be a number (e.g. 1, 4, 0.5)${C_RST}"; return
  fi
  echo "${C_CYN}[menu_14] running show_PM_status.sh morningtrading ${hours:-(all)}${C_RST}"
  "${script}" morningtrading ${hours}
}

# ---- main loop (menu_loop_01) ------------------------------------------------
while true; do
  print_header
  cat <<EOF
  1) Start bot
  2) Stop bot
  3) Start dashboard
  4) Stop dashboard
  5) Reset stats (positions, trades, summary)  [destructive]
  6) Git push (current branch)
  7) Tail latest bot log
  8) Reconcile (one-shot)
  9) Reconcile loop (toggle start/stop)
 10) Tail reconcile log
 11) Polymarket account snapshot (PnL, positions, fills, streaks)
 99) ${C_RED}${C_BLD}KILL SWITCH${C_RST} (emergency stop + cancel all open orders)
  0) Exit
EOF
  read -r -p "select> " choice
  case "${choice}" in
    1) start_bot ;;
    2) stop_bot ;;
    3) start_dashboard ;;
    4) stop_dashboard ;;
    5) reset_stats ;;
    6) git_push ;;
    7) tail_log ;;
    8) recon_once ;;
    9) recon_loop_toggle ;;
    10) tail_recon_log ;;
    11) pm_status ;;
    99) kill_switch ;;
    0) echo "bye"; exit 0 ;;
    *) echo "${C_RED}unknown choice: ${choice}${C_RST}" ;;
  esac
  read -r -p $'\npress Enter to continue...' _
done
