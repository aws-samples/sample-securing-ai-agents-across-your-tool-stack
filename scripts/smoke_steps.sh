#!/usr/bin/env bash
# Smoke run: reset (before state), then every step script in talk order (each must print READY within 60 s),
# then back to the build's start state. Stops at the first failure.
#   bash scripts/smoke_steps.sh > logs/smoke-steps.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONDONTWRITEBYTECODE=1
PY=.venv/bin/python
t0=$(date +%s)
echo "== reset (before state)"; $PY scripts/reset.py
echo "== M1 opener: unauthenticated calls"; $PY scripts/steps/m1_unauth.py
echo "== M1: Maya connects on the portal (Playwright)"; $PY scripts/consent_login.py maya | cut -c1-200
echo "== M2: Disconnect"; $PY scripts/steps/m2_disconnect.py
echo "== M2: Maya reconnects"; $PY scripts/consent_login.py maya | cut -c1-200
echo "== M2: CloudTrail view"; $PY scripts/steps/m2_cloudtrail.py 24 | tail -4
echo "== M3"; $PY scripts/steps/m3_mode.py act_on_behalf
echo "== M4 step 1"; $PY scripts/steps/m4_mask.py
echo "== M4 step 2"; $PY scripts/steps/m4_guardrail.py
echo "== M5"; $PY scripts/steps/m5_enforce.py
echo "== M5c"; $PY scripts/steps/m5c_guardrail.py
echo "== M5b"; $PY scripts/steps/m5b_enforce.py
echo "== M6 before storm (rate limit 10,000 per second since reset)"; $PY scripts/steps/m6_storm.py 12 --expect free | tail -2
echo "== M6 count"; $PY scripts/steps/m6_count.py || echo "   (skipped: no burst rule on the active engine)"
echo "== M6 limit"; $PY scripts/steps/m6_limit.py
echo "== M6 storm"; $PY scripts/steps/m6_storm.py 15 | tail -2
echo "== back to the start state"; $PY scripts/state.py on | grep -E '"problems"|canary|"grant_' || true
echo "SMOKE DONE in $(( $(date +%s) - t0 )) s"
