"""Put the stack into the build's start state (every control on) and report it; engine canary.

  .venv/bin/python scripts/state.py on        seed, toggles on, demo policies ACTIVE, rate 10/s, grants, canary, report
  .venv/bin/python scripts/state.py check     report only (exit 1 if not in the start state)
  .venv/bin/python scripts/state.py canary    switch one temporal policy's mode and back (each within 60 s)
  .venv/bin/python scripts/state.py pin       record the invariants (gateway ID, portal URL) in logs/invariants.json

Start state: seed data rewritten by the reset function (both poisoned referrals, R-2003 held), <prefix>/deputy-mode
act_on_behalf, <prefix>/masking on, <prefix>/prompt-guard on (the M5c interceptor check), <prefix>/portal-url = the
portal URL output, every policy of the active generation
ACTIVE, the rate limit at 10 per second (the template value, the stage demo lowers it to 2 for M6), Maya's and Leo's
partner grants connected. This is the template's end state of
the talk; scripts/reset.py puts the talk's before state in place. Run `on` after every deploy. Prints no secrets.
"""
import json
import os
import sys
import time

import gklib as gk

PREFIX = os.environ.get("GK_PARAM_PREFIX", "/gk")
DEMO_RATE = 10  # the template's value; the M6 step lowers it to 2 live (scripts/steps/m6_limit.py)
CANARY_POLICY_BASE = "DestinationSeenInSession"
INVARIANTS = os.path.join(gk.LOGS, "invariants.json")


def grant(user):
    r = gk.invoke_reset({"action": "probe", "user": user})
    if r.get("error_code") == -32042:
        return "consent needed"
    if not r.get("is_error") and f'"owner":"{user}"' in (r.get("text_prefix") or "").replace(" ", ""):
        return "connected"
    if r.get("owner") == user:
        return "connected"
    return f"unexpected: {gk.mask(json.dumps(r))[:160]}"


def canary_name():
    return next(n for n in gk.active_policies() if CANARY_POLICY_BASE in n)


def canary(limit=60):
    """Switch the destination rule to the other mode and back. Raises if either switch takes more than `limit` s."""
    name = canary_name()
    start = gk.get_policy(name)
    if start.get("status") != "ACTIVE":
        raise RuntimeError(f"CANARY FAILED: {name} is {start.get('status')} before the canary")
    orig = start["enforcementMode"]
    other = "LOG_ONLY" if orig == "ACTIVE" else "ACTIVE"
    out = {"policy": name, "from": orig}
    for label, mode in (("switch_s", other), ("back_s", orig)):
        try:
            out[label] = gk.set_policy_mode(name, mode, timeout=limit)
        except RuntimeError as e:
            raise RuntimeError(f"CANARY FAILED on {name} ({label} to {mode}): {e}. The engine may no longer compile temporal "
                               "policies: do not rehearse; move to a new generation.") from e
    return out


def policies():
    c, eng = gk.ctl(), gk.engine_id()
    res = {}
    for n in gk.active_policies():
        p = c.get_policy(policyEngineId=eng, policyId=gk.policy_id(n))
        res[n] = f"{p['status']} {p['enforcementMode']}"
    return res


def report():
    g = gk.ctl().get_gateway(gatewayIdentifier=gk.out(refresh=True)["GatewayId"])
    st = {"gateway_id": g["gatewayId"], "gateway_status": g["status"],
          "gateway_engine": (g.get("policyEngineConfiguration") or {}).get("arn", "").split("/")[-1],
          "gateway_engine_mode": (g.get("policyEngineConfiguration") or {}).get("mode"),
          "output_engine": gk.engine_id(),
          **{p: gk.ssm_get(f"{PREFIX}/{p}") for p in ("deputy-mode", "masking", "portal-url", "prompt-guard")},
          "portal_url_output": gk.out()["PortalUrl"], "policies": policies(), "rate_limit_per_second": gk.rate_limit(),
          "grant_maya": grant("maya"), "grant_leo": grant("leo")}
    return st


def problems(st):
    bad = []
    if st["gateway_status"] != "READY":
        bad.append("gateway not READY")
    if st["gateway_engine"] != st["output_engine"]:
        bad.append(f"gateway uses {st['gateway_engine']}, stack output says {st['output_engine']}")
    if st["deputy-mode"] != "act_on_behalf" or st["masking"] != "on" or st["prompt-guard"] != "on":
        bad.append("toggles not on")
    if st["portal-url"] != st["portal_url_output"]:
        bad.append("portal-url parameter differs from the portal URL output")
    bad += [f"{n} {v}" for n, v in st["policies"].items() if v != "ACTIVE ACTIVE"]
    if st["rate_limit_per_second"] != DEMO_RATE:
        bad.append(f"rate limit {st['rate_limit_per_second']}")
    bad += [f"grant {u}: {st['grant_' + u]}" for u in ("maya", "leo") if st["grant_" + u] != "connected"]
    if os.path.exists(INVARIANTS):
        inv = json.load(open(INVARIANTS))
        for k in ("gateway_id", "portal_url_output"):
            if inv.get(k) and inv[k] != st[k]:
                bad.append(f"INVARIANT CHANGED: {k}")
    return bad


def on(with_canary=True):
    t0 = time.time()
    seed = gk.invoke_reset({"action": "seed"})
    gk.ssm_put(f"{PREFIX}/deputy-mode", "act_on_behalf")
    gk.ssm_put(f"{PREFIX}/masking", "on")
    gk.ssm_put(f"{PREFIX}/prompt-guard", "on")
    gk.ssm_put(f"{PREFIX}/portal-url", gk.out()["PortalUrl"])
    switched = {n: gk.set_policy_mode(n, "ACTIVE") for n in gk.active_policies()}
    gk.rate_limit(DEMO_RATE)
    consent = {}
    for user in ("maya", "leo"):
        if grant(user) != "connected":
            import consent_login  # noqa: PLC0415 - Playwright only when needed
            consent[user] = consent_login.connect(user).get("partner_signin_page_shown")
    can = canary() if with_canary else None
    st = report()
    st.update({"seed": seed, "mode_switch_seconds": switched, "canary": can, "reconnected": consent, "seconds": round(time.time() - t0, 1)})
    return st


def main(argv):
    cmd = argv[0] if argv else "check"
    if cmd == "canary":
        result = canary()
    elif cmd == "pin":
        st = report()
        os.makedirs(gk.LOGS, exist_ok=True)
        json.dump({k: st[k] for k in ("gateway_id", "portal_url_output")}, open(INVARIANTS, "w"), indent=1)
        result = {"pinned": INVARIANTS}
    else:
        result = on(with_canary="--no-canary" not in argv) if cmd == "on" else report()
        result["problems"] = problems(result)
    path = gk.save(f"state-{cmd}", result)
    print(gk.mask(json.dumps(result, indent=1, default=str)))
    print("saved", path)
    if isinstance(result, dict) and result.get("problems"):
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv[1:])
