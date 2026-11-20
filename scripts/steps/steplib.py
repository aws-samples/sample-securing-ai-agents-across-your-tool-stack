"""Shared helpers for the step scripts: each prints READY once its change is in effect, within 60 s.

Console effects: when a step script imports this module, the console's moment tracker moves to the
script's stage and a "Running: <label>" banner shows (SCRIPTS below; an unknown script shows its file name and no
stage). ready() turns the banner into "Done: <label>" on READY and "Failed: <label>" on NOT READY. A script that does not
end through ready() calls finish(ok) itself (m6_storm.py, m6_count.py). If the script exits without either (an early
refusal, a crash), the banner turns to "Failed". All of it is a quiet no-op when the console is not running.
"""
import atexit
import os
import sys
import time

STEPS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(STEPS_DIR, ".."))
sys.path.insert(0, os.path.join(STEPS_DIR, "..", ".."))  # the repo root, for console.client

import gklib as gk  # noqa: E402
from console.client import post_banner, post_moment, post_row  # noqa: E402 - no-ops when the console is not running
from state import PREFIX  # noqa: E402

LIMIT = 60
# Script (and, for m3_mode.py, its first argument) -> (tracker stage, banner label). Stages use the on-stage names:
# 5a the sequence rule (m5_enforce.py), 5b the prompt guard (m5c_guardrail.py), 5c human approval (m5b_enforce.py).
SCRIPTS = {
    "m1_unauth.py": ("1", "front door check"),
    "m2_disconnect.py": ("2", "disconnect Maya's partner account"),
    "m2_cloudtrail.py": ("2", "the vault's CloudTrail record"),
    "m3_mode.py act_on_behalf": ("3", "deputy mode: act on behalf"),
    "m3_mode.py service": ("3", "deputy mode: service"),
    "m3_mode.py impersonation": ("3", "deputy mode: impersonation"),
    "m4_mask.py": ("4", "masking on"),
    "m4_guardrail.py": ("4", "SSN guardrail: enforce"),
    "m5_enforce.py": ("5a", "destination rule: enforce"),
    "m5c_guardrail.py": ("5b", "R-2003 published, prompt guard on"),
    "m5b_enforce.py": ("5c", "approval rule: enforce"),
    "m6_storm.py": ("6", "storm: Maya's script at {rps} calls a second"),
    "m6_limit.py": ("6", "rate limit: 2 per second per staff member"),
    "m6_count.py": ("6", "per-session count check"),
}


def storm_rps(args):
    """m6_storm.py's request rate, read from its arguments the way it reads them: --rps N, else 4 with --expect free
    (the before storm) and 8 otherwise (against the limit)."""
    if "--rps" in args:
        try:
            return float(args[args.index("--rps") + 1])
        except (IndexError, ValueError):
            pass
    i = args.index("--expect") + 1 if "--expect" in args else None
    return 4.0 if i is not None and i < len(args) and args[i] == "free" else 8.0


def banner_for(script, args=()):
    """(stage or None, label) for a step script's file name and arguments."""
    args = list(args)
    key = f"{script} {args[0]}" if script == "m3_mode.py" and args else script
    if key not in SCRIPTS:
        return None, script
    stage, label = SCRIPTS[key]
    if script == "m6_storm.py":
        label = label.format(rps=f"{storm_rps(args):g}")
    return stage, label


def _running_script():
    """The step script being run (its file name), or None when this module is imported by something else (a test)."""
    path = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    return os.path.basename(path) if os.path.dirname(path) == STEPS_DIR else None


SCRIPT = _running_script()
STAGE, LABEL = banner_for(SCRIPT, sys.argv[1:]) if SCRIPT else (None, None)
_final = []  # set once the done or failed banner is posted


def finish(ok):
    """Post this script's final banner, once: "Done: <label>" if ok, else "Failed: <label>"."""
    if LABEL and not _final:
        _final.append(ok)
        post_banner(LABEL, "done" if ok else "failed")


def _at_exit():
    if LABEL and not _final:  # an early sys.exit or a crash before READY
        finish(False)


if SCRIPT:
    if STAGE:
        post_moment(STAGE, source="script")
    post_banner(LABEL, "running")
    atexit.register(_at_exit)


def ready(label, check, timeout=LIMIT, every=2, final=True, t0=None):
    """Poll check() until it returns a truthy detail; print READY or fail loudly. Posts the result to the console feed and,
    unless final is False (a check in the middle of a script), the script's done or failed banner. t0: the start to time from."""
    t0 = t0 or time.time()
    while True:
        detail = check()
        if detail:
            print(f"READY {label} in {time.time() - t0:.1f} s: {gk.mask(detail)}")
            post_row("-", label[:48], "READY", "step script", gk.mask(detail)[:600])  # was 160; the console keeps 600
            if final:
                finish(True)
            return
        if time.time() - t0 > timeout:
            print(f"NOT READY {label} after {timeout} s")
            post_row("-", label[:48], "ERROR", "step script", f"not ready after {timeout} s")
            finish(False)
            sys.exit(1)
        time.sleep(every)


def policy(base):
    return next(n for n in gk.active_policies() if base in n)


def switch(base, mode, label, final=True):
    """READY <label> in <whole switch> s: <policy> mode <mode>, status ACTIVE (one timing, plain words)."""
    t0 = time.time()
    name = policy(base)
    gk.set_policy_mode(name, mode, timeout=LIMIT)
    ready(label, lambda: switched(name, mode), final=final, t0=t0)


def switched(name, mode):
    p = gk.get_policy(name)
    return f"{name} mode {mode}, status ACTIVE" if p.get("enforcementMode") == mode and p.get("status") == "ACTIVE" else None
