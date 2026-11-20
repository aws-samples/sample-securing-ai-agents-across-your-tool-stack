#!/usr/bin/env bash
# Demo step menu for the AIM331 code talk. Run it in the step terminal (terminal 2):  ./run.sh
# Pick a number and the step runs here, with its output on screen. Then press Enter to come back to the menu.
# The console (python -m console) stays in terminal 1; this menu never starts or stops it.

cd "$(dirname "$0")" || exit 1
PY=.venv/bin/python

# number|label|command|hint
# Entries 3 to 17 run their step through scripts/run_step.py <card id> (console/cards/<id>.json holds the command), so the
# console's step card shows the code and the output; the menu text and hints are unchanged.
# Number 7 is not used. The other numbers match the talk's walkthrough; "next" goes by position in STEPS, not by number.
STEPS=(
  "1|Setup: full reset (prep.py, about 3.5 min)|$PY scripts/prep.py|Before a run. Then restart the console and press New conversation in both panes."
  "2|Setup: quick reset (prep.py --quick, about 1 min)|$PY scripts/prep.py --quick|Only if Moment 6 has not run in the last 15 minutes."
  "3|Clip 00: front door check (m1_unauth.py)|$PY scripts/run_step.py m1-unauth|While clip 00 plays. Leave the output on screen for Moment 1."
  "4|Moment 1 fallback: connect Maya by script|$PY scripts/run_step.py m1-consent-maya|Only if the browser consent fails twice. Then send M1 Again after Connect."
  "5|Moment 2 fallback: connect Leo by script|$PY scripts/run_step.py m2-consent-leo|Only if Leo gets CONSENT_REQUIRED. Then resend."
  "6|Moment 2: disconnect Maya (m2_disconnect.py)|$PY scripts/run_step.py m2-disconnect|Expect READY M2 Maya disconnected; Leo still connected."
  "8|Clip 02: reconnect Maya (needed for Moment 5a)|$PY scripts/run_step.py m2-reconnect-maya|While clip 02 plays. Must be done before Moment 5a."
  "9|Moment 3: act on behalf (m3_mode.py)|$PY scripts/run_step.py m3-mode|Then press New conversation in both panes and send M3 Loan summary in both."
  "10|Moment 4: masking on (m4_mask.py)|$PY scripts/run_step.py m4-mask|Run 11 right after this one."
  "11|Moment 4: SSN guardrail on (m4_guardrail.py)|$PY scripts/run_step.py m4-guardrail|Then press New conversation in Maya's pane."
  "12|Moment 5a: destination rule on (m5_enforce.py)|$PY scripts/run_step.py m5-enforce|Then press New conversation in both panes."
  "13|Clip 05: publish R-2003, prompt guard on (m5c_guardrail.py)|$PY scripts/run_step.py m5c-guardrail|While clip 05 plays. Prints R-2003's text for Moment 5b."
  "14|Clip 06: approval rule on (m5b_enforce.py)|$PY scripts/run_step.py m5b-enforce|While clip 06 plays. Then press New conversation in both panes."
  "15|Moment 6: storm with no limit (12 s)|$PY scripts/run_step.py m6-storm-free|Expect Maya about 48 allowed, 0 rate-limited."
  "16|Moment 6: lower the limit to 2/s (m6_limit.py)|$PY scripts/run_step.py m6-limit|Hand to the deck (clip 08) as soon as the countdown starts."
  "17|Moment 6: storm against the limit (50 s)|$PY scripts/run_step.py m6-storm-limit|At t+24s send Maya's chip, then Leo's."
)

GOTO_TARGETS=(M1 M2 M3 M4 M5 M5c M5b M6)
GOTO_NAMES=("Moment 1" "Moment 2" "Moment 3" "Moment 4" "Moment 5a" "Moment 5b" "Moment 5c" "Moment 6")

bold=$'\e[1m'; dim=$'\e[2m'; grn=$'\e[32m'; red=$'\e[31m'; ylw=$'\e[33m'; off=$'\e[0m'

# GK_EXPECT_ACCOUNT (optional): the 12-digit account the demo runs in. Without it, any working credentials pass here;
# scripts/prep.py still refuses to run without it (or .gk-local.json).
check_account() {
  local acct
  acct=$(aws sts get-caller-identity --query Account --output text 2>/dev/null)
  if [[ -z "$acct" || "$acct" == "None" ]]; then
    echo "${red}AWS credentials missing or expired.${off}"
    echo "Refresh them in this terminal, then choose ${bold}c${off} to check again."
  elif [[ -n "${GK_EXPECT_ACCOUNT:-}" && "$acct" != "$GK_EXPECT_ACCOUNT" ]]; then
    echo "${red}AWS credentials are for the wrong account (ending ${acct: -4}, expected ending ${GK_EXPECT_ACCOUNT: -4}).${off}"
  else
    echo "${grn}AWS credentials OK: account ending ${acct: -4}${off}"
  fi
}

refresh_creds() {
  echo "Refresh your AWS credentials the way you usually do (for example: aws sso login --profile <name>), then check:"
  check_account
}

# The pre-flight (scripts/preflight.py, under 30 s, plus about 20 s if it renews Leo's partner grant): credentials,
# stack, Leo's and Maya's grants, the before state, staff sign-in and the console. One line per check.
run_preflight() {
  echo "${bold}Pre-flight check${off} ${dim}(\$ $PY scripts/preflight.py)${off}"
  PYTHONUNBUFFERED=1 $PY scripts/preflight.py
  if [[ $? -eq 0 ]]; then
    echo "${grn}Pre-flight OK.${off}"
  else
    echo "${red}Fix the problems above, then choose p to run the check again.${off}"
  fi
}

menu() {
  clear
  echo "${bold}AIM331 demo steps${off}   ${dim}(demo folder: $PWD)${off}"
  [[ -n "$LAST" ]] && echo "${dim}Last run: $LAST${off}"
  echo
  local entry n label cmd hint
  for entry in "${STEPS[@]}"; do
    IFS='|' read -r n label cmd hint <<<"$entry"
    if [[ "$n" == "$NEXT" ]]; then
      printf "  ${ylw}%3s  %s   <- next${off}\n" "$n" "$label"
    else
      printf "  %3s  %s\n" "$n" "$label"
    fi
  done
  echo
  echo "    g  Jump to the start of a moment (goto.py)"
  echo "    p  Run the pre-flight check again"
  echo "    c  Check AWS credentials"
  echo "    q  Quit"
  echo
}

run_cmd() {
  local label=$1 cmd=$2 hint=$3 rc start
  echo
  echo "${bold}$label${off}"
  echo "${dim}\$ $cmd${off}"
  [[ -n "$hint" ]] && echo "${ylw}$hint${off}"
  echo
  start=$(date +%s)
  # PYTHONUNBUFFERED makes every line print as it happens (the M6 t+ lines depend on it).
  PYTHONUNBUFFERED=1 bash -c "$cmd"
  rc=$?
  echo
  if [[ $rc -eq 0 ]]; then
    echo "${grn}Done in $(( $(date +%s) - start )) s.${off}"
  else
    echo "${red}Exit code $rc after $(( $(date +%s) - start )) s.${off} See 'If a step fails' in README.md."
    [[ $rc -eq 2 ]] && echo "${red}Exit code 2 is a policy engine error: do not retry.${off}"
  fi
  LAST="$label (exit $rc, $(date +%H:%M:%S))"
  echo "${hint:+${ylw}Reminder: $hint${off}}"
  read -r -p "Press Enter for the menu " _
}

goto_menu() {
  echo
  local i
  for i in "${!GOTO_TARGETS[@]}"; do
    printf "  %d  %s  ${dim}(goto.py %s)${off}\n" $((i + 1)) "${GOTO_NAMES[$i]}" "${GOTO_TARGETS[$i]}"
  done
  read -r -p "Jump to which moment? (Enter to cancel) " pick
  [[ -z "$pick" ]] && return
  if [[ "$pick" =~ ^[0-9]+$ ]] && (( pick >= 1 && pick <= ${#GOTO_TARGETS[@]} )); then
    local t=${GOTO_TARGETS[$((pick - 1))]}
    run_cmd "Jump to ${GOTO_NAMES[$((pick - 1))]}" "$PY scripts/goto.py $t" \
      "Run the full reset (1) first if Moment 6 ran recently. Then press New conversation in both panes."
  else
    echo "${red}Not a choice: $pick${off}"; sleep 1
  fi
}

# The number the menu marks "<- next" after the entry at position $1 in STEPS: the next entry in talk order, skipping
# the quick reset and the fallback steps. By position, not by number, because the numbers have a gap (7).
next_after() {
  local j
  for (( j = $1 + 1; j < ${#STEPS[@]}; j++ )); do
    [[ "${STEPS[$j]}" =~ (fallback|quick\ reset) ]] && continue
    echo "${STEPS[$j]%%|*}"
    return
  done
}

LAST=""
NEXT=1
clear
run_preflight
read -r -p "Press Enter for the menu " _

while true; do
  menu
  read -r -p "Choose a number, g, p, c or q: " choice
  case "$choice" in
    q|Q) exit 0 ;;
    p|P) echo; run_preflight; read -r -p "Press Enter for the menu " _ ;;
    c|C) refresh_creds; read -r -p "Press Enter for the menu " _ ;;
    g|G) goto_menu ;;
    "") ;;
    *)
      found=""
      for i in "${!STEPS[@]}"; do
        IFS='|' read -r n label cmd hint <<<"${STEPS[$i]}"
        if [[ "$n" == "$choice" ]]; then
          found=1
          run_cmd "$n. $label" "$cmd" "$hint"
          NEXT=$(next_after "$i")
          break
        fi
      done
      [[ -z "$found" ]] && { echo "${red}Not a choice: $choice${off}"; sleep 1; }
      ;;
  esac
done
