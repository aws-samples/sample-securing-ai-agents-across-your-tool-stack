"""Reset: put the talk's BEFORE state in place, check it, and print a state table.

  .venv/bin/python scripts/reset.py [--cut 40] [--keep-maya]

Before state: deputy mode `service`, masking `off`, prompt guard `off` (the M5c interceptor check); the destination,
approval, SSN and prompt-attack policies LOG_ONLY
(the base permit stays ACTIVE, with --cut 40 the two temporal rules stay ACTIVE); rate limit 10,000 per
second (raised now, at least a minute before the before-storm); seed data rewritten (R-2002 open, R-2003
held); Maya's partner grant revoked with forceAuthentication so consent repeats (skip with --keep-maya); Leo's grant
checked; the engine canary. Exit status 1 on any mismatch. Prints no secrets.
The after state (every control on, the template's end state) is scripts/state.py on.
"""
import json
import os
import sys
import time

import gklib as gk
from state import PREFIX, canary, grant

BEFORE_RATE = 10000


def main(argv):
    cut40, keep_maya = "--cut" in argv and "40" in argv, "--keep-maya" in argv
    t0 = time.time()
    out, bad = {"cut": 40 if cut40 else 60}, []
    out["seed"] = gk.invoke_reset({"action": "seed"})
    gk.ssm_put(f"{PREFIX}/deputy-mode", "service")
    gk.ssm_put(f"{PREFIX}/masking", "off")
    gk.ssm_put(f"{PREFIX}/prompt-guard", "off")
    gk.rate_limit(BEFORE_RATE)
    out["rate_limit_raised_at"] = int(time.time())
    want = {}
    for n in gk.active_policies():
        temporal = "DestinationSeenInSession" in n or "ApprovalOver2500" in n
        want[n] = "ACTIVE" if "PermitAll" in n or (cut40 and temporal) else "LOG_ONLY"
    out["mode_switch_seconds"] = {n: gk.set_policy_mode(n, m, timeout=60) for n, m in want.items()}  # skips ones already there
    out["canary"] = canary()
    if keep_maya:
        out["maya"] = grant("maya")
    else:
        r = gk.invoke_reset({"action": "revoke", "user": "maya"})
        out["maya"] = {"revoke_http": r["revoke_call"].get("http_status"), "consent_repeats": r.get("consent_repeats")}
        bad += [] if r.get("consent_repeats") else ["Maya's grant: consent does not repeat after the revoke"]
    out["leo"] = grant("leo")
    bad += [] if out["leo"] == "connected" else ["Leo must reconnect on the portal (scripts/consent_login.py leo)"]
    # read back
    table = {f"{PREFIX}/deputy-mode": gk.ssm_get(f"{PREFIX}/deputy-mode"), f"{PREFIX}/masking": gk.ssm_get(f"{PREFIX}/masking"),
             f"{PREFIX}/prompt-guard": gk.ssm_get(f"{PREFIX}/prompt-guard"), "rate_limit_per_second": gk.rate_limit()}
    for n, m in want.items():
        p = gk.get_policy(n)
        table[n] = f"{p['status']} {p['enforcementMode']}"
        if table[n] != f"ACTIVE {m}":
            bad.append(f"{n} is {table[n]}, want ACTIVE {m}")
    if table[f"{PREFIX}/deputy-mode"] != "service" or table[f"{PREFIX}/masking"] != "off" or table[f"{PREFIX}/prompt-guard"] != "off":
        bad.append("toggles not in the before state")
    if table["rate_limit_per_second"] != BEFORE_RATE:
        bad.append("rate limit not raised")
    out.update({"state": table, "problems": bad, "seconds": round(time.time() - t0, 1)})
    path = gk.save("reset", out)
    print(gk.mask(json.dumps(out, indent=1, default=str)))
    print("saved", path)
    print("RESET FAILED" if bad else "READY: before state in place. Start new conversations in both panes.")
    clear_moment()
    sys.exit(1 if bad else 0)


def clear_moment():
    """The console's moment tracker back to no current stage (every pill grey), as before Moment 1, and no
    step card or Run results."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # the repo root, for console.client
        from console.client import post_card, post_moment  # noqa: PLC0415 - no-ops when the console is not running
        post_moment(None, source="reset")
        post_card("clear")
    except Exception:  # noqa: BLE001 - the console never decides whether a reset worked
        pass


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main(sys.argv[1:])
