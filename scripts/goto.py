"""Put the stack in the starting (before) state of one moment, as if every earlier moment had run in talk order.

  .venv/bin/python scripts/goto.py <M1|M2|M3|M4|M5|M5c|M5b|M6> [--dry-run]

The plan comes from scripts/moments.py (ORDER, the talk order, and AFTER_STEPS, each moment's state changes), so the
harness and this script share one source: Leo's partner grant checked (re-consent only if needed, so reset can confirm
it), reset.py (the talk's before state, with the engine canary), then, silently and in talk order, every earlier
moment's after-steps: Maya's portal consent (M1), m3_mode.py act_on_behalf (M3), m4_mask.py and m4_guardrail.py (M4),
m5_enforce.py (M5), m5c_guardrail.py (M5c), m5b_enforce.py (M5b). Then the live state is read back and compared with
the state the plan should leave (EFFECTS below), and the presenter's next actions are printed: new conversations in
both panes when the step scripts say so, and the prompts to pick.

Never passes impersonation to m3_mode.py (that beat is a slide). --dry-run prints the plan and the expected end state without
touching AWS. Exit 0 when the live state matches, 1 on any failure or mismatch, 2 on an engine stop (a policy mode
switch or the canary failed with an engine error: nothing more touches the engine). Prints no secrets.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import consent_login  # noqa: E402
import gklib as gk  # noqa: E402
import moments as mo  # noqa: E402
from console.prompts import PROMPTS  # noqa: E402
from reset import BEFORE_RATE  # noqa: E402

TARGETS = ["M1", "M2", "M3", "M4", "M5", "M5c", "M5b", "M6"]
REFERRALS_TABLE = "gk-partner-referrals"  # the stack's referrals table (same name in every Region; tables are Regional)
POLICY_BASES = ("PermitAll", "SuppressSsnInNotes", "DestinationSeenInSession", "ApprovalOver2500")
M6_LIVE_RATE = 2  # scripts/steps/m6_limit.py
START = ("grants", "leo")  # Leo connected first, so reset.py can confirm his grant
RESET = ("reset.py",)
# What each step leaves behind. The reset entry is the whole before state of the talk.
BEFORE_STATE = {"deputy-mode": "service", "masking": "off", "prompt-guard": "off", "PermitAll": "ACTIVE", "SuppressSsnInNotes": "LOG_ONLY",
                "DestinationSeenInSession": "LOG_ONLY", "ApprovalOver2500": "LOG_ONLY", "rate": BEFORE_RATE, "R-2003": "held",
                "maya": "consent needed", "leo": "connected"}
EFFECTS = {
    START: {"leo": "connected"},
    RESET: BEFORE_STATE,
    ("consent", "maya"): {"maya": "connected"},
    ("steps/m3_mode.py", "act_on_behalf"): {"deputy-mode": "act_on_behalf"},
    ("steps/m4_mask.py",): {"masking": "on"},
    ("steps/m4_guardrail.py",): {"SuppressSsnInNotes": "ACTIVE"},
    ("steps/m5_enforce.py",): {"DestinationSeenInSession": "ACTIVE"},
    ("steps/m5c_guardrail.py",): {"prompt-guard": "on", "R-2003": "open"},
    ("steps/m5b_enforce.py",): {"ApprovalOver2500": "ACTIVE"},
    ("steps/m6_limit.py",): {"rate": M6_LIVE_RATE},
}
# The moment's own flow, for the presenter (the prompts themselves come from console/prompts.py).
NOTES = {
    "M1": "Optional opener first: scripts/steps/m1_unauth.py. Maya's first prompt shows the consent link; she connects on the portal.",
    "M2": "Maya is connected: 'M1 Referrals (consent)' in her pane lists her referrals. Then the Disconnect step and Maya reconnects on the portal.",
    "M3": "First prompt in service mode (allowed), then the step, new conversations, and 'M3 Loan summary' in both panes.",
    "M4": "First prompt with masking off, then both steps, a new conversation and the same prompt again.",
    "M5": "Both prompts in LOG_ONLY (the transfer executes), then the step, new conversations in both panes, both prompts again (DENIED).",
    "M5c": "The step first, then 'M5c Referrals again' in Maya's pane (withheld); Leo's pane: 'M5c Referrals' (his list comes back).",
    "M5b": "First prompt in LOG_ONLY (executes), then the step, new conversations, the prompt again (DENIED), Request approval, Approve as Dana, the retry prompt.",
    "M6": "Before storm: scripts/steps/m6_storm.py 12 --expect free. Then the step (35 s countdown) and the after storm "
          "scripts/steps/m6_storm.py 50 --leo-delay 25, with both panes' prompts sent 25 s into it. After any M6, wait for the settle (scripts/prep.py).",
}


def plan(target):
    """[{"moment", "step"}] in talk order: the start steps, then every earlier moment's AFTER_STEPS."""
    if target not in TARGETS:
        raise ValueError(f"target must be one of {TARGETS}")
    steps = [{"moment": "start", "step": START}, {"moment": "start", "step": RESET}]
    for mid in mo.ORDER[:mo.ORDER.index(target)]:
        steps += [{"moment": mid, "step": tuple(s)} for s in mo.AFTER_STEPS[mid]]
    bad = [s for s in steps if any("impersonation" in str(x) for x in s["step"])]
    if bad:
        raise ValueError(f"refusing a plan that passes impersonation: {bad}")
    return steps


def expected_state(steps):
    st = {}
    for s in steps:
        st.update(EFFECTS[s["step"]])
    return st


def live_state():
    """The controls read back live: SSM toggles, policy modes (each must be status ACTIVE), the rate limit, R-2003's
    status and both partner grants (state.py's report: the reset function's probe call per persona).
    Returns (live, gateway, notes, problems)."""
    import state  # noqa: PLC0415
    st = state.report()
    live, notes, problems = {}, [], []
    for name, v in st["policies"].items():
        status, mode = v.split()
        base = next((b for b in POLICY_BASES if b in name), name)
        live[base] = mode
        if status != "ACTIVE":
            problems.append(f"{name} status {status}")
    item = gk.SESSION.resource("dynamodb").Table(REFERRALS_TABLE).get_item(Key={"pk": "OWNER#maya", "sk": "REF#R-2003"}).get("Item") or {}
    live.update({"deputy-mode": st["deputy-mode"], "masking": st["masking"], "prompt-guard": st["prompt-guard"],
                 "rate": int(float(st["rate_limit_per_second"])), "R-2003": item.get("status"), "maya": st["grant_maya"], "leo": st["grant_leo"]})
    # With the prompt guard on and R-2003 published (after M5c), Maya's probe call comes back withheld by the RESPONSE
    # interceptor (403, -32002). That can only happen after the partner answered with her stored token, so she is connected.
    if live["prompt-guard"] == "on" and str(live["maya"]).startswith("unexpected") and '"error_code": -32002' in live["maya"]:
        live["maya"] = "connected"
        notes.append("Maya's probe was withheld by the prompt guard (R-2003 published), which needs her stored partner token: connected")
    gw = {k: st[k] for k in ("gateway_id", "gateway_status", "gateway_engine", "gateway_engine_mode", "output_engine")}
    if gw["gateway_status"] != "READY" or gw["gateway_engine"] != gw["output_engine"]:
        problems.append(f"gateway {gw}")
    return live, gw, notes, problems


def compare(want, live):
    return [f"{k}: live {live.get(k)!r}, want {v!r}" for k, v in want.items() if live.get(k) != v]


def next_actions(target, said_new_conversations):
    lines = []
    if said_new_conversations:
        lines.append("Start new conversations in both panes (the step scripts said so).")
    for pane, who in (("maya", "Maya's pane"), ("right", "Right pane (Leo, or Dana's card in M5b)")):
        labels = [p["label"] for p in PROMPTS[pane] if p["step"] == target]
        if labels:
            lines.append(f"{who}: pick " + ", then ".join(f"'{lab}'" for lab in labels) + " (console prompts).")
    own = [" ".join(("scripts/" + s[0],) + tuple(s[1:])) for s in mo.AFTER_STEPS[target] if s[0] != "consent"]
    if own:
        lines.append(f"{target}'s own step{'s' if len(own) > 1 else ''}: " + "; ".join(own) + ".")
    lines.append(NOTES[target])
    return lines


def run_plan(steps, log):
    """Run the steps; returns (step records, said_new_conversations). Raises mo.EngineStop on an engine error."""
    recs, said, switches = [], False, []
    for s in steps:
        name, args = s["step"][0], s["step"][1:]
        t0 = time.time()
        if name == "grants":
            r = consent_login.ensure_grants(tuple(args))
            rec = {"step": "grants " + " ".join(args), "seconds": round(time.time() - t0, 1), "result": r,
                   "ok": all(row["after"] == "connected" for row in r.values())}
        elif name == "consent":
            import state  # noqa: PLC0415
            c = consent_login.connect(args[0])
            after = state.grant(args[0])
            rec = {"step": f"consent {args[0]}", "seconds": round(time.time() - t0, 1), "partner_signin_shown": c.get("partner_signin_page_shown"),
                   "grant_after": after, "ok": after == "connected"}
        else:
            r = mo.run_step(name, *args, mode_switches=switches)
            said = said or any("Start new conversations in both panes" in ln for ln in r["tail"] + [r.get("ready") or ""])
            rec = {"step": " ".join((name,) + tuple(args)), "seconds": r["seconds"], "rc": r["rc"], "ready": r["ready"], "saved": r["saved"],
                   "utc": r["utc"], "utc_end": r["utc_end"], "ok": r["rc"] == 0 and bool(r["ready"]),
                   **({} if r["rc"] == 0 else {"tail": r["tail"], "stderr_tail": r["stderr_tail"]})}
        rec["moment"] = s["moment"]
        recs.append(rec)
        log(f"  {rec['moment']:5s} {rec['step']:38s} {'ok' if rec['ok'] else 'FAILED'} in {rec['seconds']} s"
            + (f"  {gk.mask(rec.get('ready') or '')[:110]}" if rec.get("ready") else ""))
        if not rec["ok"]:
            break
    return recs, said, switches


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 1 or args[0] not in TARGETS:
        sys.exit(f"usage: goto.py <{'|'.join(TARGETS)}> [--dry-run]")
    target, dry = args[0], "--dry-run" in argv
    steps = plan(target)
    want = expected_state(steps)
    print(f"goto {target} on stack {gk.STACK} in {gk.REGION}: {len(steps)} steps")
    for s in steps:
        print(f"  {s['moment']:5s} {' '.join(s['step'])}")
    print("expected end state: " + ", ".join(f"{k} {v}" for k, v in want.items()))
    if dry:
        print("dry run: nothing changed. Next, once there:")
        for ln in next_actions(target, True):
            print("  " + ln)
        return 0
    t0 = time.time()
    out = {"target": target, "stack": gk.STACK, "region": gk.REGION, "start_utc": mo.utc(t0), "plan": [{"moment": s["moment"], "step": list(s["step"])} for s in steps],
           "expected": want}
    try:
        recs, said, switches = run_plan(steps, lambda ln: print(ln, flush=True))
    except mo.EngineStop as e:
        import rehearse  # noqa: PLC0415 - reads only: every policy's status
        out.update({"engine_stop": str(e)[:1500], "policies": rehearse.capture_engine(), "seconds": round(time.time() - t0, 1)})
        print("saved", gk.save(f"goto-{target}", out))
        print("ENGINE STOP: a policy mode switch or the canary failed with an engine error. Do not retry; nothing more "
              "touches the engine. Capture the state above; see 'If a step fails' in README.md.")
        return 2
    out.update({"steps": recs, "mode_switches": switches})
    problems = [f"step failed: {r['step']} {r.get('tail') or r.get('result') or r.get('grant_after')}" for r in recs if not r["ok"]]
    if not problems:
        live, gw, notes, bad = live_state()
        out.update({"live": live, "gateway": gw, "notes": notes})
        problems += compare(want, live) + bad
        print("live state: " + ", ".join(f"{k} {live.get(k)}" for k in want))
        for n in notes:
            print("  note: " + n)
    out.update({"problems": problems, "seconds": round(time.time() - t0, 1), "end_utc": mo.utc()})
    print("saved", gk.save(f"goto-{target}", out))
    if problems:
        print(f"GOTO {target} FAILED after {out['seconds']} s:")
        for p in problems:
            print("  " + gk.mask(p)[:300])
        return 1
    print(f"READY: {target} before state in place in {out['seconds']} s (stack {gk.STACK}, {gk.REGION}). Next:")
    for ln in next_actions(target, said):
        print("  " + ln)
    from console.client import post_moment  # noqa: PLC0415 - a no-op when the console is not running
    post_moment(step=target, source="goto")  # the console's moment tracker shows the target's stage
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
