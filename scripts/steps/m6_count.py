"""M6 count: a per-session count is not a per-identity limit.

  python scripts/steps/m6_count.py

Run 1: ReadBurstPerSession ACTIVE, one policy session: the 20th read in a minute is DENIED by the policy. Run 2: a new
policy session ID on every call: the count never builds and every call is allowed. Run 3 is the rate limit (m6_storm.py),
which keys on the identity and throttles regardless of session IDs. READY when runs 1 and 2 behave that way.

Needs a generation whose engine carries ReadBurstPerSession. Engine 2 (generation 2, the rehearsal engine) does not: the
burst rule compiled next to the demo set only on an experiment engine, and nothing new goes on the demo
engine without the talk-order sequence test on an experiment engine first. Until then this refuses."""
import sys
import time

from steplib import finish, gk, switch

name = next((n for n in gk.active_policies() if "ReadBurstPerSession" in n), None)
if not name:
    sys.exit("NOT READY: the active engine has no ReadBurstPerSession policy (engine 2 has none; see the docstring). "
             "Narrate R7.6 from the slide, or move to a generation that carries the burst rule.")
switch("ReadBurstPerSession", "ACTIVE", "burst rule ACTIVE", final=False)  # the console banner waits for the runs below
maya = gk.access("maya")
m = gk.Mcp(maya)
one = []
for i in range(22):
    r = gk.call(m, "get_member_accounts", {"member_id": "M-1001"})
    one.append(r["err_code"] or "ok")
    time.sleep(0.6)
fresh = []
for i in range(22):
    r = gk.call(gk.Mcp(maya), "get_member_accounts", {"member_id": "M-1001"})
    fresh.append(r["err_code"] or "ok")
    time.sleep(0.6)
first_deny = next((i + 1 for i, c in enumerate(one) if c == -32002), None)
print(f"one session: first DENIED at read {first_deny}; new session per call: {fresh.count(-32002)} denied of {len(fresh)}")
ok = first_deny is not None and first_deny <= 21 and fresh.count(-32002) == 0
print("READY M6 count" if ok else "NOT READY M6 count")
finish(ok)  # the console banner: Done or Failed
sys.exit(0 if ok else 1)
