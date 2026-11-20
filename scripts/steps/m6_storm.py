"""M6 storm: Maya's identity calls get_member_accounts directly through the Gateway from 4
workers, while Leo calls the same target once a second. Prints per-second counts for Maya and Leo's totals. Default
rate: 4 requests per second with --expect free (the before storm), 8 with
--expect throttled (after m6_limit.py).

  python scripts/steps/m6_storm.py [seconds] [--expect throttled|free] [--rps N] [--leo-delay S] [--json PATH]

--leo-delay S starts Leo's worker S seconds into the storm (default 0, the stage behavior). --json PATH also writes a
machine-readable summary for the rehearsal harness (scripts/rehearse.py): per-second counts for Maya, and every one of
Leo's calls with its kind and seconds into the storm.

Two kinds of HTTP 429 (-32003) exist, and the script counts them apart:
- rate limit: "Rate limit exceeded", data.limitKey starting per-staff-member-records (the per-identity limit);
- policy evaluation: "Tool Execution Denied: Policy Evaluation Throttled", no limitKey. The policy engine protects itself
  as well; that throttle is not per identity, so it can hit Leo too.
--expect throttled (default; rate limit 2 per second): READY when Maya gets rate-limit throttles and every one of Leo's
calls is allowed (the rate limit keeps Maya's load off the policy engine). --expect free (the before storm, rate limit
10,000 per second after reset): READY when no rate-limit throttle appears; policy-evaluation throttles are reported.
Rate limits fail open on service timeouts and unresolvable dimensions: they are a cost and availability control, not an
authorization boundary."""
import collections
import json
import sys
import threading
import time

from steplib import finish, gk, post_row

# Print each per-second line as it happens: with stdout on a pipe or a non-terminal (a wrapped or logged terminal),
# Python buffers output, and the t+ lines only appeared after the storm, so the t+24s cue never showed in time.
sys.stdout.reconfigure(line_buffering=True)

VALUE_FLAGS = ("--rps", "--leo-delay", "--json", "--expect")
SECONDS = int(next((a for i, a in enumerate(sys.argv[1:], 1) if a.isdigit() and sys.argv[i - 1] not in VALUE_FLAGS), "60"))
LEO_DELAY = float(sys.argv[sys.argv.index("--leo-delay") + 1]) if "--leo-delay" in sys.argv else 0.0
JSON_OUT = sys.argv[sys.argv.index("--json") + 1] if "--json" in sys.argv else None
EXPECT = sys.argv[sys.argv.index("--expect") + 1] if "--expect" in sys.argv else "throttled"
# 4 per second before m6_limit.py. At 5, Leo got one policy-evaluation throttle in 6 of 11 rehearsal runs (always
# 9 to 11 s into the 12 s storm), because Maya plus Leo came close to the policy engine's own evaluation throttle; at
# 4 the pair stays below it.
# 8 per second after, against the live limit of 2.
RPS = float(sys.argv[sys.argv.index("--rps") + 1]) if "--rps" in sys.argv else (4.0 if EXPECT == "free" else 8.0)
WORKERS = 4
TOOL = "MemberRecords___get_member_accounts"  # Maya's storm
ARGS = {"member_id": "M-1001"}
# Leo works his loan file: get_loan_file is his entitlement. Act-on-behalf mode refuses get_member_accounts for a
# loan officer, and a refusal must never count as "ok".
LEO_TOOL = "MemberRecords___get_loan_file"
SAMPLES = {}


def kind(msg):
    """Count key from the shared classifier (agent/gk_mcp via gklib): the same labels as the agent's hook."""
    d = gk.classify(msg)
    if d["decision"] != "ALLOWED":
        e = (msg or {}).get("error") or {}
        SAMPLES.setdefault(d["decision"] + str(d.get("limit") or ""), gk.mask(e.get("message") or d.get("reason") or "")[:120])
    if d["decision"] == "THROTTLED":
        if d.get("limit") == "policy-evaluation":
            return "policy_eval"
        return "rate_limit" if str(d.get("limit_key") or "").startswith("per-staff-member-records") else "other_429"
    return "ok" if d["decision"] == "ALLOWED" else f"error {d['decision']}"


def run():
    gk.gk_mcp()  # load the classifier once, before the worker threads start
    maya, leo = gk.access("maya"), gk.access("leo")
    per, leo_n, lock = collections.defaultdict(collections.Counter), collections.Counter(), threading.Lock()
    leo_calls = []
    t0 = time.time()
    stop = t0 + SECONDS

    def worker(token, gap, is_leo):
        m = gk.Mcp(token)
        if is_leo and LEO_DELAY:
            time.sleep(max(0.0, t0 + LEO_DELAY - time.time()))
        while time.time() < stop:
            s = time.time()
            k = kind(m.call(LEO_TOOL if is_leo else TOOL, ARGS))
            with lock:
                (leo_n if is_leo else per[int(s - t0)])[k] += 1
                if is_leo:
                    leo_calls.append({"t": round(s - t0, 1), "kind": k})
            time.sleep(max(0.0, gap - (time.time() - s)))

    ths = [threading.Thread(target=worker, args=(maya, WORKERS / RPS, False)) for _ in range(WORKERS)]
    ths.append(threading.Thread(target=worker, args=(leo, 1.0, True)))
    for t in ths:
        t.start()
    last = -1
    while any(t.is_alive() for t in ths):
        time.sleep(1)
        with lock:
            for s in [s for s in sorted(per) if last < s < int(time.time() - t0)]:
                c = per[s]
                print(f"  t+{s:02d}s  Maya allowed {c['ok']:2d}  rate-limited {c['rate_limit']:2d}  policy-eval throttled {c['policy_eval']:2d}")
                last = s
    for t in ths:
        t.join()
    total = collections.Counter()
    for c in per.values():
        total.update(c)
    return total, leo_n, per, leo_calls


total, leo_n, per_second, leo_calls = run()
leo_bad = sum(v for k, v in leo_n.items() if k != "ok")
n_maya = sum(total.values())
print(f"Maya ({RPS:g} rps): {total['ok']} allowed, {total['rate_limit']} rate-limited, {total['policy_eval']} policy-eval throttled, "
      f"other {sum(v for k, v in total.items() if k not in ('ok', 'rate_limit', 'policy_eval'))}; Leo: {leo_n['ok']} allowed, {leo_bad} not {dict(leo_n)}")
post_row("Maya", "get_member_accounts", "THROTTLED" if total["rate_limit"] else "ALLOWED",
         "Rate limit per-staff-member-records" if total["rate_limit"] else "storm script",
         f"storm {RPS:g}/s: {total['rate_limit']} of {n_maya} rate-limited, {total['policy_eval']} engine-throttled; "
         f"Leo {leo_n['ok']} of {sum(leo_n.values())} allowed")
for k, v in SAMPLES.items():
    print(f"  sample {k}: {v}")
if EXPECT == "free":
    ok = total["rate_limit"] == 0 and total["ok"] > 0
else:
    ok = total["rate_limit"] > 0 and leo_n["ok"] > 0 and leo_bad == 0
if JSON_OUT:
    with open(JSON_OUT, "w") as f:
        json.dump({"seconds": SECONDS, "rps": RPS, "expect": EXPECT, "leo_delay": LEO_DELAY, "ready": ok, "maya_total": dict(total),
                   "maya_per_second": {str(k): dict(v) for k, v in sorted(per_second.items())}, "leo_total": dict(leo_n),
                   "leo_calls": leo_calls, "samples": SAMPLES}, f, indent=1)
print(f"READY M6 storm ({EXPECT})" if ok else f"NOT READY M6 storm ({EXPECT})")
finish(ok)  # the console banner: Done or Failed (this script does not end through steplib.ready)
sys.exit(0 if ok else 1)
