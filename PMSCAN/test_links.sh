#!/usr/bin/env bash
# test_links.sh — diagnose why OSC 8 hyperlinks aren't visible in your terminal.
# Just type ./test_links.sh and read the three test outputs.

set -u

BAR='============================================================'

echo
echo "$BAR"
echo "TEST 1 of 3  —  ANSI styling"
echo "$BAR"
echo "If your terminal handles ANSI, the next line will appear in"
echo "CYAN with an UNDERLINE.  If it appears as plain white text,"
echo "your terminal is stripping ANSI escapes — that explains it."
echo
printf '   \e[4;36mTHIS LINE SHOULD BE CYAN AND UNDERLINED\e[0m\n'
echo
read -rp "Was the line above cyan+underlined? (y/n): " ans1
echo

echo "$BAR"
echo "TEST 2 of 3  —  OSC 8 hyperlink"
echo "$BAR"
echo "The next line emits a clickable hyperlink to polymarket.com."
echo "Look for these clues that it worked:"
echo "  - cursor changes shape (pointer / hand) when you hover over"
echo "    the text"
echo "  - a TOOLTIP appears showing https://polymarket.com"
echo "  - Ctrl+click opens your browser to polymarket.com"
echo
printf '   \e]8;;https://polymarket.com\e\\>>> HOVER OR CTRL+CLICK THIS TEXT <<<\e]8;;\e\\\n'
echo
read -rp "Did hover show a tooltip or did Ctrl+click open the browser? (y/n): " ans2
echo

echo "$BAR"
echo "TEST 3 of 3  —  Is show_PM_status.sh actually sending escapes?"
echo "$BAR"
OUT="/tmp/pm_links_check.out"
if [[ ! -x ./show_PM_status.sh ]]; then
    echo "  skipped: ./show_PM_status.sh not found in $(pwd)"
else
    FORCE_COLOR=1 ./show_PM_status.sh >"$OUT" 2>&1
    osc8=$(grep -c $'\x1b]8' "$OUT" || true)
    ansi=$(grep -c $'\x1b\[' "$OUT" || true)
    echo "  OSC 8 hyperlink sequences in output : $osc8   (expect > 0)"
    echo "  ANSI color/style sequences in output: $ansi   (expect > 0)"
    echo
    echo "  Raw bytes around ticket #PMTR0021 (look for ]8;;https://...):"
    if grep -aq PMTR0021 "$OUT"; then
        line=$(grep -a PMTR0021 "$OUT" | head -1 | cat -A)
        echo "  ${line:0:240}"
    else
        echo "  (no PMTR0021 ticket in latest run output)"
    fi
fi
echo
echo "$BAR"
echo "SUMMARY"
echo "$BAR"
echo "  Test 1 (ANSI cyan+underline) : $ans1"
echo "  Test 2 (OSC 8 hyperlink)     : $ans2"
echo
case "$ans1$ans2" in
    yy) echo "  -> Both work. show_PM_status.sh links should be clickable."
        echo "     If still not seeing them, run the script and Ctrl+click"
        echo "     directly on a cyan/underlined ticket like #PMTR0021." ;;
    yn) echo "  -> Terminal renders ANSI but NOT OSC 8 hyperlinks."
        echo "     GNOME Terminal supports OSC 8 since VTE 0.50 — check"
        echo "     that you're on gnome-terminal (not some wrapper), or"
        echo "     update VTE.  Workaround: use kitty or WezTerm." ;;
    ny) echo "  -> Strange: hyperlinks work but ANSI doesn't. Unusual." ;;
    nn) echo "  -> Terminal is stripping all escape sequences. Check"
        echo "     terminal profile settings, or try a different terminal."
        ;;
    *)  echo "  -> One of the tests wasn't answered." ;;
esac
echo
