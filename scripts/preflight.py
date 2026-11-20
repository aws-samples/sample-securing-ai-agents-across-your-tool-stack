"""Pre-flight check before a demo run: one line per check, OK, WARN or FAIL, then what it found and what
to do. run.sh runs it when its menu starts; menu entry p runs it again.

  .venv/bin/python scripts/preflight.py

1. AWS credentials are for the demo account (the expected account as scripts/prep.py finds it).
2. The stack is found; the gateway is READY; its policy engine is ENFORCE and is the stack's engine.
3. Leo's partner grant: connected, and his last recorded re-consent under 12 hours old; otherwise it is renewed now (the
   portal consent by Playwright, as prep.py does, about 20 s) and checked again. Prints when it runs out.
4. Maya's partner grant: "consent needed" (she consents live in Moment 1).
5. The controls are in the before state that prep.py leaves (the start of Moment 1); if not, which goto.py start they match.
6. Staff sign-in works for maya, leo and dana (Cognito passwords in step with the secrets).
7. The console: it answers a health message on its Unix socket (console/channel.py), it has working AWS credentials,
   it runs the latest console files, and its step-card registry loaded with no errors.

Checks 2 to 6 need check 1, and 3 to 5 need check 2: when one fails, those after it print SKIP. Saves
logs/preflight-<epoch>.json. Exit 0 when every check is OK, 1 otherwise. Changes nothing on the stack except the renewal
of Leo's partner grant. Prints no token, password or secret.
"""
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.dirname(HERE)
for _p in (HERE, DEMO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from botocore.config import Config  # noqa: E402

import consent_login  # noqa: E402
import gklib as gk  # noqa: E402
import goto  # noqa: E402
import prep  # noqa: E402
from console import client as console_client  # noqa: E402

OK, WARN, FAIL, SKIP = "OK", "WARN", "FAIL", "SKIP"
CONTROLS = [k for k in goto.BEFORE_STATE if k not in ("maya", "leo")]  # check 5; the grants are checks 3 and 4
STAGE_NAMES = {"M1": "Moment 1", "M2": "Moment 2", "M3": "Moment 3", "M4": "Moment 4", "M5": "Moment 5a", "M5c": "Moment 5b",
               "M5b": "Moment 5c", "M6": "Moment 6"}  # goto.py targets as the audience hears them (run.sh's jump menu)
STAFF = (("maya", "console"), ("leo", "console"), ("dana", "approver"))  # Dana signs in with the approvals app
QUICK = Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 2})


ACCOUNT_ID = re.compile(r"\b\d{12}\b")


class Report:
    def __init__(self, out=print):
        self.rows, self.out = [], out

    def add(self, n, name, status, text):
        if n != 1:  # check 1 names the account on purpose, as prep.py does; elsewhere an ID can only come from an error text
            text = ACCOUNT_ID.sub("<acct>", str(text))
        self.rows.append({"n": n, "check": name, "status": status, "text": text})
        self.out(f"{status:4s}  {n} {name}: {text}")

    def problems(self):
        return [r for r in self.rows if r["status"] in (WARN, FAIL)]


def role_of(arn):
    return arn.split(":assumed-role/")[1].split("/")[0] if ":assumed-role/" in arn else arn.split(":")[-1]


def check_account(r):
    want = prep.expected_account()
    name = "AWS credentials"
    if not want:
        r.add(1, name, FAIL, f"no expected account set: export GK_EXPECT_ACCOUNT=<demo account ID>, or write "
                             f"{{\"expected_account\": \"<ID>\"}} to {prep.LOCAL}")
        return False
    try:
        me = gk.SESSION.client("sts", config=QUICK).get_caller_identity()
    except Exception as e:  # noqa: BLE001 - no or expired credentials
        r.add(1, name, FAIL, f"none that work ({type(e).__name__}): refresh them (menu c), then run this check again (menu p)")
        return False
    if me["Account"] != want:
        r.add(1, name, FAIL, f"for account {me['Account']} (role {role_of(me['Arn'])}), not the demo account {want}: refresh them "
                             "for the demo account (menu c), then run this check again (menu p)")
        return False
    r.add(1, name, OK, f"demo account {want}, role {role_of(me['Arn'])}")
    return True


def check_stack(r):
    """Returns (stack found, goto.live_state()'s live dict or None when the state cannot be read)."""
    name = "Stack and gateway"
    try:
        gk.out(refresh=True)
    except Exception as e:  # noqa: BLE001
        r.add(2, name, FAIL, f"stack {gk.STACK} not found in {gk.REGION} ({type(e).__name__}): check GK_STACK and GK_REGION")
        return False, None
    try:
        live, gw, _notes, problems = goto.live_state()
    except Exception as e:  # noqa: BLE001
        r.add(2, name, FAIL, f"could not read the stack's state ({type(e).__name__}: {str(e)[:160]}): check the credentials and rerun")
        return True, None
    bad = [] if gw.get("gateway_status") == "READY" else [f"gateway {gw.get('gateway_status')}"]
    if gw.get("gateway_engine_mode") != "ENFORCE":
        bad.append(f"policy engine mode {gw.get('gateway_engine_mode')}")
    if gw.get("gateway_engine") != gw.get("output_engine"):
        bad.append(f"gateway engine {gw.get('gateway_engine')} is not the stack's engine {gw.get('output_engine')}")
    bad += [p for p in problems if not p.startswith("gateway ")]  # a policy whose status is not ACTIVE
    found = f"stack {gk.STACK} in {gk.REGION}, gateway {gw.get('gateway_id')} {gw.get('gateway_status')}, engine {gw.get('gateway_engine')} " \
            f"{gw.get('gateway_engine_mode')}"
    if bad:
        r.add(2, name, FAIL, f"{found}: {'; '.join(bad)}. Do not start: the stack needs a fix first")
    else:
        r.add(2, name, OK, f"{found} (the stack's engine)")
    return True, live


def check_leo(r, live, log=print):
    name = "Leo's partner grant"
    rec = consent_login.last_refresh("leo")
    connected = live.get("leo") == "connected"
    if connected and not prep.renewal_due(rec):
        age = (time.time() - rec["epoch"]) / 3600
        r.add(3, name, OK, f"connected, re-consented {age:.1f} h ago; {prep.grant_expiry_line('Leo', rec)}")
        return
    if not connected:
        why = f"not connected ({str(live.get('leo'))[:80]})"
    elif not rec:
        why = "connected, but no re-consent is recorded on this machine"
    else:
        why = f"connected, but the last re-consent ({rec.get('utc')}) is over {prep.GRANT_RENEW_H} h old"
    log(f"      Leo's grant: {why}. Renewing it now (portal consent, about 20 s) ...")
    error = None
    try:
        after = consent_login.ensure_grants(("leo",), force=True)["leo"]["after"]
    except Exception as e:  # noqa: BLE001 - Playwright or the portal failed: probe his grant again and report it below
        error = type(e).__name__
        try:
            from state import grant  # noqa: PLC0415
            after = grant("leo")
        except Exception as e2:  # noqa: BLE001
            after = f"unreadable ({type(e2).__name__})"
    live["leo"] = after
    expiry = prep.grant_expiry_line("Leo", consent_login.last_refresh("leo"))
    fix = "run .venv/bin/python scripts/consent_login.py leo --screens and check logs/consent/"
    if after == "connected" and not error:
        r.add(3, name, OK, f"renewed now ({why}); {expiry}")
    elif after == "connected":
        r.add(3, name, WARN, f"the renewal failed ({error}) but he is still connected on the old grant; {expiry}: {fix}")
    else:
        r.add(3, name, FAIL, f"not connected after the renewal ({str(after)[:80]}{'; ' + error if error else ''}): {fix}; {expiry}")


def check_maya(r, live):
    name = "Maya's partner grant"
    m = live.get("maya")
    if m == "consent needed":
        r.add(4, name, OK, "consent needed (she consents live in Moment 1)")
    elif m == "connected":
        r.add(4, name, WARN, "connected, so Moment 1 shows no consent link: run the full reset (1)")
    else:
        r.add(4, name, WARN, f"could not be read ({str(m)[:120]}): run the full reset (1)")


def start_matches(live):
    """The goto.py targets whose start state has these control values."""
    return [t for t in goto.TARGETS if all(live.get(k) == goto.expected_state(goto.plan(t)).get(k) for k in CONTROLS)]


def check_controls(r, live):
    name = "Controls"
    want = goto.expected_state(goto.plan("M1"))  # prep.py leaves the stack where the talk starts
    diffs = [f"{k} {live.get(k)} (want {want[k]})" for k in CONTROLS if live.get(k) != want[k]]
    if not diffs:
        r.add(5, name, OK, "the before state: " + ", ".join(f"{k} {live.get(k)}" for k in CONTROLS))
        return
    m = start_matches(live)
    where = ("they match the start of " + " or ".join(f"{STAGE_NAMES[t]} (goto.py {t})" for t in m)) if m else \
        "they match no goto.py start state"
    r.add(5, name, WARN, f"not the before state: {', '.join(diffs)}; {where}. To start at Moment 1, run the full reset (1)")


def check_staff(r):
    name = "Staff sign-in"
    res = {}
    for user, app_client in STAFF:
        try:
            a = gk.staff_auth(user, app_client)
            res[user] = "OK" if a.get("AccessToken") else "no token"
            a.clear()
        except KeyError:  # gk.staff_auth found no AuthenticationResult: Cognito answered with a challenge
            res[user] = "SignInChallenge"
        except Exception as e:  # noqa: BLE001 - print only the error type
            res[user] = type(e).__name__
    found = ", ".join(f"{u} {v}" for u, v in res.items())
    if all(v == "OK" for v in res.values()):
        r.add(6, name, OK, found)
    elif any(v == "NotAuthorizedException" for v in res.values()):
        r.add(6, name, FAIL, f"{found}: the Cognito passwords are out of step with the secrets; run "
                             ".venv/bin/python scripts/sync_cognito_passwords.py, then this check again")
    else:
        r.add(6, name, FAIL, f"{found}: check the staff pool users, then run this check again")


def console_version_now():
    from console.app import newest_mtime  # noqa: PLC0415 - the same file list the console reads at start
    return int(newest_mtime())


def check_console(r, sock=None, wait_s=6):
    """Check 7, over the console's Unix socket (sock: a test console's path; default the client's)."""
    name = "Console"
    start = "start the console in terminal 1 (.venv/bin/python -m console, from the repo root)"
    path = sock or console_client.SOCK
    deadline = time.time() + wait_s
    while True:
        h = health_at(path)
        if h is None:
            if not os.path.exists(path):
                r.add(7, name, FAIL, f"not running (no socket {path}): {start}")
            else:
                r.add(7, name, FAIL, f"no answer on its socket {path}: {start}")
            return
        st = h.get("status") or {}
        if st.get("state") != "starting" or time.time() > deadline:
            break
        time.sleep(2)  # the first status read runs right after the start
    old = int(h.get("version") or 0) < console_version_now()
    moment = (h.get("moment") or {}).get("current")
    errors = h.get("card_errors") or []
    found = f"running on 127.0.0.1:{h.get('port')} (pid {h.get('pid')}, started {h.get('started_utc')}), moment tracker " + \
            (f"at stage {moment}" if moment else "clear") + f", {h.get('cards', 0)} step cards"
    if st.get("state") != "ok":
        r.add(7, name, FAIL, f"{found}; its status is {st.get('state')} ({st.get('error')}): the console has no working AWS "
                             "credentials: refresh them in terminal 1, then restart the console" + (" (it also runs an older version)" if old else ""))
    elif errors:
        r.add(7, name, FAIL, f"{found}; the card registry skipped {len(errors)} card(s): {'; '.join(errors)[:400]}. "
                             "Fix console/cards/, then restart the console")
    elif not h.get("cards"):
        r.add(7, name, FAIL, f"{found}; no step cards loaded: the Run buttons will not show. Check console/cards/, then restart the console")
    elif old:
        r.add(7, name, WARN, f"{found}; it started before the latest change to its files: restart the console to load the latest version")
    else:
        r.add(7, name, OK, f"{found}; AWS credentials OK; latest version; card registry OK")


def health_at(path):
    """console_client.health() on path (the client's SOCK for the duration of the call)."""
    saved = console_client.SOCK
    console_client.SOCK = path
    try:
        return console_client.health()
    finally:
        console_client.SOCK = saved


def save_result(data):
    """gk.save("preflight", ...), with every 12-digit account ID masked first (also one from the wrong account)."""
    data = json.loads(ACCOUNT_ID.sub("<acct>", json.dumps(data, default=str)))
    try:
        return gk.save("preflight", data)
    except Exception:  # noqa: BLE001 - gk.save masks with STS, which fails without credentials; the data is masked already
        os.makedirs(gk.LOGS, exist_ok=True)
        path = os.path.join(gk.LOGS, f"preflight-{int(time.time())}.json")
        with open(path, "w") as f:
            json.dump(data, f, indent=1)
        return path


def main(argv=(), out=print):
    t0 = time.time()
    r = Report(out)
    out(f"Pre-flight on stack {gk.STACK} in {gk.REGION} ...")
    live = None
    if check_account(r):
        found, live = check_stack(r)
        if live is not None:
            check_leo(r, live, log=out)
            check_maya(r, live)
            check_controls(r, live)
        else:
            for n, name in ((3, "Leo's partner grant"), (4, "Maya's partner grant"), (5, "Controls")):
                r.add(n, name, SKIP, "needs the stack's state (check 2)")
        if found:
            check_staff(r)
        else:
            r.add(6, "Staff sign-in", SKIP, "needs the stack (check 2)")
    else:
        for n, name in ((2, "Stack and gateway"), (3, "Leo's partner grant"), (4, "Maya's partner grant"), (5, "Controls"),
                        (6, "Staff sign-in")):
            r.add(n, name, SKIP, "needs the demo account's credentials (check 1)")
    check_console(r)
    problems = r.problems()
    data = {"start_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0)), "stack": gk.STACK, "region": gk.REGION,
            "checks": r.rows, "problems": len(problems), "live": live, "seconds": round(time.time() - t0, 1)}
    out(f"saved {save_result(data)} ({data['seconds']:.0f} s)")
    out("PREFLIGHT OK: ready for the demo" if not problems else f"PREFLIGHT: {len(problems)} problem(s): fix them before you start")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], out=lambda s: print(s, flush=True)))
