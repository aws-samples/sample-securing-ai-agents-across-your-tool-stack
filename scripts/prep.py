"""Prepare the stack for a dry run or the talk: the one command a presenter runs 10 to 15 minutes before going on.

  .venv/bin/python scripts/prep.py [--quick]

1. The AWS identity (sts get-caller-identity): prints the account and role and refuses unless the account is the demo
   account. The expected account is not in the repo: set GK_EXPECT_ACCOUNT, or put {"expected_account":
   "<12 digits>"} in .gk-local.json in the repo root (git-ignored) once per machine.
2. The stack and Region (GK_STACK, GK_REGION through scripts/gklib.py).
3. Leo's partner grant (consent_login.ensure_grants): renewed by Playwright when his last recorded re-consent is missing
   or older than 12 hours (a grant lasts exactly 24), otherwise re-consented only if needed; then when it runs out, in
   Eastern Time. Maya's is not checked: reset.py revokes it next (her consent is part of M1), and after M5c the prompt
   guard withholds her partner list, so a probe before the reset cannot read her grant.
4. reset.py: the talk's before state, with the engine canary.
5. The settle probe, the harness's own (scripts/moments.py cooldown, as scripts/rehearse.py runs it after M6): a burst
   of 4 direct calls as Maya every 30 s until 6 consecutive probes see no rate-limit throttle, cap 10 minutes, so stray
   throttles left by an earlier M6 cannot reach the first moments. --quick skips it (only for a stack that has not run
   M6 in the last 15 minutes).
6. One READY block read back live: stack, Region, gateway, engine, rate limit, deputy mode, every control, when the
   partner grants were last refreshed and when Leo's runs out.

Exit 0 with READY; 1 with a plain message on any failure; 2 on an engine stop (a policy mode switch or the canary
failed with an engine error: do not retry, nothing more touches the engine). Prints no token, password or secret.
"""
import datetime
import json
import os
import sys
import threading
import time
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import consent_login  # noqa: E402
import gklib as gk  # noqa: E402
import goto  # noqa: E402
import moments as mo  # noqa: E402

LOCAL = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".gk-local.json"))
# A partner grant lasts exactly 24 hours from its consent: the partner app client's refresh token
# (infra/gk_demo/stack.py, refresh_token_validity=Duration.days(1)). Renewing only a grant that is already gone is too
# late, so prep and the pre-flight renew Leo's once it is 12 hours old.
GRANT_LIFE_H = 24
GRANT_RENEW_H = 12
ET = ZoneInfo("America/New_York")


def renewal_due(rec, now=None):
    """True when the last recorded re-consent is missing or older than GRANT_RENEW_H hours (rec: last_refresh(user))."""
    if not rec or not isinstance(rec.get("epoch"), (int, float)):
        return True
    return ((now if now is not None else time.time()) - rec["epoch"]) / 3600 > GRANT_RENEW_H


def grant_expiry_line(user, rec, now=None):
    """When the user's partner grant runs out, in Eastern Time and in hours from now, assuming GRANT_LIFE_H hours from
    the last recorded re-consent."""
    if not rec or not isinstance(rec.get("epoch"), (int, float)):
        return f"{user}'s partner grant: expiry unknown (no re-consent recorded on this machine)"
    now = now if now is not None else time.time()
    end = rec["epoch"] + GRANT_LIFE_H * 3600
    et = datetime.datetime.fromtimestamp(end, ET)
    left = (end - now) / 3600
    when = f"in {left:.1f} h" if left >= 0 else f"{-left:.1f} h ago"
    return f"{user}'s partner grant runs out {et:%a %d %b %H:%M} {et.tzname()} ({when}; {GRANT_LIFE_H} h from the re-consent at {rec.get('utc')})"


def ensure_leo(log, now=None):
    """Leo's grant step: re-consent when his last recorded re-consent is missing or older than GRANT_RENEW_H hours,
    otherwise only if he is not connected. Returns (ensure_grants result, renewed on age). If a renewal on age fails
    (the portal or Playwright), his grant is probed again: still connected means prep goes on with the old grant, whose
    expiry it prints; not connected ends in the existing failure path."""
    rec = consent_login.last_refresh("leo")
    force = renewal_due(rec, now)
    if force:
        log(f"  leo: last re-consent {'not recorded' if not rec else 'at ' + str(rec.get('utc'))}; renewing now (every {GRANT_RENEW_H} h)")
    try:
        return consent_login.ensure_grants(("leo",), force=force), force
    except Exception as e:  # noqa: BLE001 - the consent portal or the browser failed
        if not force:
            raise
        from state import grant  # noqa: PLC0415
        after = grant("leo")
        log(f"  leo: the renewal failed ({type(e).__name__}); his grant is {after}")
        return {"leo": {"before": "?", "reconsented": False, "partner_signin_shown": None, "after": after,
                        "renewal_error": type(e).__name__}}, force


def expected_account():
    v = os.environ.get("GK_EXPECT_ACCOUNT")
    if not v and os.path.exists(LOCAL):
        v = (json.load(open(LOCAL)) or {}).get("expected_account")
    return str(v).strip() if v else None


def fail(msg, code=1):
    print(f"PREP FAILED: {msg}", flush=True)
    sys.exit(code)


def identity():
    want = expected_account()
    if not want:
        fail(f"no expected account set: export GK_EXPECT_ACCOUNT=<demo account ID>, or write {{\"expected_account\": \"<ID>\"}} to {LOCAL}")
    try:
        me = gk.SESSION.client("sts").get_caller_identity()
    except Exception as e:  # noqa: BLE001 - no or expired credentials
        fail(f"no AWS credentials for the demo account ({type(e).__name__}): sign in and rerun")
    arn = me["Arn"]
    role = arn.split(":assumed-role/")[1].split("/") if ":assumed-role/" in arn else [arn.split(":")[-1], ""]
    print(f"AWS account {me['Account']}, role {role[0]}" + (f" (session {role[1]})" if len(role) > 1 and role[1] else ""), flush=True)
    if me["Account"] != want:
        fail(f"these credentials are for account {me['Account']}, not the demo account {want}: switch credentials and rerun")
    return {"account_ok": True, "role": role[0]}


def grants_line(user, row, live):
    rec = consent_login.last_refresh(user)
    if row and row.get("reconsented"):
        when = f"re-consented by this prep at {rec['utc'] if rec else mo.utc()}"
    elif rec:
        age_h = (time.time() - rec["epoch"]) / 3600
        when = f"last re-consent recorded {rec['utc']} ({age_h:.1f} h ago; prep renews it after {GRANT_RENEW_H} h)"
    else:
        when = "no re-consent recorded on this machine (grant age unknown)"
    return f"{user}: {live}; {when}"


def settle(log):
    """The harness's settle probe (moments.cooldown) from now, with a progress line every 30 s."""
    res, t0 = {}, time.time()
    th = threading.Thread(target=lambda: res.update(mo.cooldown(t0, gk.access("maya"), cap_s=600)), daemon=True)
    th.start()
    while th.is_alive():
        th.join(30)
        if th.is_alive():
            log(f"  settle probe running: {int(time.time() - t0)} s (needs 6 consecutive clean probes, 30 s apart; cap 600 s)")
    return res


def main(argv):
    quick = "--quick" in argv
    t0 = time.time()
    log = lambda s: print(s, flush=True)  # noqa: E731
    out = {"start_utc": mo.utc(t0), "quick": quick}
    out["identity"] = identity()
    log(f"stack {gk.STACK}, Region {gk.REGION}")
    try:
        gk.out(refresh=True)
    except Exception as e:  # noqa: BLE001
        fail(f"stack {gk.STACK} not found in {gk.REGION} ({type(e).__name__}): check GK_STACK and GK_REGION")
    log(f"partner grants (Leo's: renewed when his last re-consent is over {GRANT_RENEW_H} h old, else only if needed) ...")
    # Only Leo's grant is checked here: reset.py revokes Maya's right after (her consent is part of M1) and confirms
    # that her consent link repeats. Probing Maya's grant before the reset is also unreliable: after M5c (prompt guard
    # on, R-2003 published) her partner list is withheld by the interceptor, so the probe cannot read "connected".
    g, renewed = ensure_leo(log)
    out["grants"] = g
    out["leo_renewed_on_age"] = renewed
    for u, row in g.items():
        log(f"  {u:5s} before {row['before']:16s} reconsented {str(row['reconsented']):5s} after {row['after']}")
    if not all(row["after"] == "connected" for row in g.values()):
        fail("Leo's partner grant is not connected after the re-consent: run .venv/bin/python scripts/consent_login.py leo --screens and check logs/consent/")
    leo_expiry = grant_expiry_line("Leo", consent_login.last_refresh("leo"))
    out["leo_expiry"] = leo_expiry
    log(f"  {leo_expiry}")
    log("reset.py ...")
    switches = []
    try:
        r = mo.run_step("reset.py", mode_switches=switches)
    except mo.EngineStop as e:
        import rehearse  # noqa: PLC0415 - reads only: every policy's status
        out.update({"engine_stop": str(e)[:1500], "policies": rehearse.capture_engine(), "mode_switches": switches})
        print("saved", gk.save("prep", out))
        fail("ENGINE STOP: a policy mode switch or the canary failed with an engine error. Do not retry; nothing more touches the "
             "engine. See 'If a step fails' in README.md.", code=2)
    out["reset"] = {k: r.get(k) for k in ("rc", "seconds", "ready", "saved", "utc", "utc_end")}
    rj = json.load(open(r["saved"])) if r.get("saved") and os.path.exists(r["saved"]) else {}
    out["reset"].update({"canary": rj.get("canary"), "problems": rj.get("problems")})
    log(f"  reset rc {r['rc']} in {r['seconds']} s: {r.get('ready') or r['tail'][-2:]}")
    if r["rc"] != 0:
        print("saved", gk.save("prep", out))
        fail(f"reset.py did not reach the before state: {rj.get('problems') or r['tail'][-3:]}")
    if quick:
        out["settle"] = {"skipped": "--quick"}
        log("settle probe skipped (--quick)")
    else:
        log("settle probe ...")
        s = settle(log)
        out["settle"] = s
        if not s.get("settled"):
            print("saved", gk.save("prep", out))
            fail(f"the rate limit did not settle within 600 s (last rate-limit throttle at {s.get('last_throttle_s')} s of probing): wait a few "
                 "minutes and rerun prep.py")
        log(f"  settled: 6 consecutive clean probes from {s['settled_s']} s; last rate-limit throttle at {s.get('last_throttle_s')} s; "
            f"{len(s['probes'])} probes; engine throttles {s.get('engine_throttles')}")
    live, gw, notes, bad = goto.live_state()
    want = goto.expected_state(goto.plan("M1"))  # prep leaves the stack where the talk starts: M1's before state
    problems = goto.compare(want, live) + bad
    out.update({"live": live, "gateway": gw, "notes": notes, "problems": problems, "seconds": round(time.time() - t0, 1), "end_utc": mo.utc()})
    print("saved", gk.save("prep", out))
    if problems:
        fail("the live state is not the talk's start state: " + "; ".join(problems))
    can = out["reset"].get("canary") or {}
    pol = " | ".join(f"{b} {live[b]}" for b in goto.POLICY_BASES)
    settle_line = ("skipped (--quick)" if quick else
                   f"settled ({out['settle']['settled_s']} s to the first of 6 clean probes, last throttle "
                   f"{out['settle'].get('last_throttle_s') if out['settle'].get('last_throttle_s') is not None else 'none'})")
    print("\n".join([
        "",
        f"READY  {out['end_utc']}  (prep took {out['seconds']:.0f} s)",
        f"  stack      {gk.STACK}  Region {gk.REGION}",
        f"  gateway    {gw['gateway_id']} {gw['gateway_status']}, engine {gw['gateway_engine']} {gw['gateway_engine_mode']}",
        f"  rate limit {live['rate']} per second per staff member (reset raised it; M6 lowers it to 2)",
        f"  deputy     {live['deputy-mode']}   masking {live['masking']}   prompt guard {live['prompt-guard']}   R-2003 {live['R-2003']}",
        f"  policies   {pol}",
        f"  canary     {can.get('switch_s')} s and {can.get('back_s')} s",
        f"  settle     {settle_line}",
        f"  grants     maya: {live['maya']} (reset revoked it: she consents on the portal in M1)",
        f"             {grants_line('leo', g.get('leo'), live['leo'])}",
        f"             {leo_expiry}",
        "  next       start the console (.venv/bin/python -m console), new conversations in both panes, then R1.2 and M1.",
        "             After any M6 (rehearsal or warm-up), run prep.py again (full settle) before the next start.",
    ]), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
