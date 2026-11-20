"""Offline tests for the rehearsal assertions (scripts/moments.py check_* functions): no AWS, no network.

  .venv/bin/python -m pytest -q tests/test_rehearse_assertions.py

Fixture: tests/fixtures/rehearse-run-01.json, the first harness run (logs/rehearse-20261001T210841Z/run-01.json,
already scrubbed: account masked, URLs reduced to hosts, no tokens). Its M5 would-deny result was re-read with the
corrected check for the same windows. Run 1 had three real reds (M6 engine throttles on Leo, M5b model declined the
retry, and the M5 would-deny check bug), so the tests check both directions: green data passes, and each red is caught.
"""
import copy
import json
import os

import moments as mo

FIX = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "rehearse-run-01.json")))
M = {m["id"]: m for m in FIX["moments"]}
PORTAL = M["M1"]["agent"]["before"]["consent_hosts"][0]


def m(mid):
    return copy.deepcopy(M[mid])


def test_recorded_green_moments_pass_both_paths():
    assert mo.check_r12_agent(M["R1.2"]["agent"]) == []
    assert mo.check_r12_direct(M["R1.2"]["direct"]) == []
    assert mo.check_m1_agent(M["M1"]["agent"], PORTAL) == []
    assert mo.check_m1_direct(M["M1"]["direct"], PORTAL) == []
    assert mo.check_m2_agent(M["M2"]["agent"]) == []
    assert mo.check_m2_direct(M["M2"]["direct"]) == []
    assert mo.check_m2_cloudtrail(M["M2-CT"]["direct"]) == []
    assert mo.check_m3_agent(M["M3"]["agent"]) == []
    assert mo.check_m3_direct(M["M3"]["direct"]) == []
    assert mo.check_m4_agent(M["M4"]["agent"]) == []
    assert mo.check_m4_direct(M["M4"]["direct"]) == []
    assert mo.check_m5c_agent(M["M5c"]["agent"]) == []
    assert mo.check_m5c_direct(M["M5c"]["direct"]) == []
    assert mo.check_m5b_direct(M["M5b"]["direct"]) == []


def test_m5_both_halves_and_the_would_deny_record():
    a, d = m("M5")["agent"], m("M5")["direct"]
    assert mo.m5_outcome([a["LOG_ONLY"]["turn1"], a["LOG_ONLY"]["turn2"]], "LOG_ONLY") == "executed"
    assert mo.m5_outcome([a["ACTIVE"]["turn1"], a["ACTIVE"]["turn2"]], "ACTIVE") == "gateway_denied"
    assert mo.check_m5_agent(a) == [] and mo.check_m5_direct(d) == []
    a["ACTIVE"]["transfers_to_ext"] = [{"destination_ref": "EXT-4471", "amount": 12500, "t": 1}]
    assert any("TRANSFER rows" in f for f in mo.check_m5_agent(a))
    d["LOG_ONLY"]["transfers_to_ext"] = []
    assert mo.check_m5_direct(d)
    d = m("M5")["direct"]
    d["would_deny"] = {"group": "g", "found": False, "waited_s": 300}
    assert any("would-deny" in f for f in mo.check_m5_direct(d))
    d["would_deny"] = {"skipped": True, "group": "g"}  # no log group (the standby): optional, not red
    assert mo.check_m5_direct(d) == []


def test_m5_model_decline_is_its_own_outcome():
    a = m("M5")["agent"]
    for t in (a["ACTIVE"]["turn1"], a["ACTIVE"]["turn2"]):
        t["tools"], t["decisions"] = [x for x in t["tools"] if not x["tool"].endswith("initiate_transfer")], \
            [x for x in t["decisions"] if not x["tool"].endswith("initiate_transfer")]
    assert mo.m5_outcome([a["ACTIVE"]["turn1"], a["ACTIVE"]["turn2"]], "ACTIVE") == "model_declined"
    assert any("model_declined" in f for f in mo.check_m5_agent(a))


def test_m4_draft_leak_and_missing_rows_are_caught():
    a = m("M4")["agent"]
    a["after"]["facts"]["ssn"] = True
    assert any(f.startswith("M4 after: the draft carries") for f in mo.check_m4_agent(a))
    a = m("M4")["agent"]
    a["after_trail"] = [r for r in a["after_trail"] if r["decision"] != "REDACTED"]
    assert any("REDACTED" in f for f in mo.check_m4_agent(a))
    d = m("M4")["direct"]
    d["after"]["notes"]["decision"] = "ALLOWED"
    assert mo.check_m4_direct(d)


def test_m6_recorded_engine_throttle_on_leo_is_red_and_the_green_shape_passes():
    d, a = m("M6")["direct"], m("M6")["agent"]
    assert any("Leo" in f for f in mo.check_m6_direct(d))  # run 1: one engine throttle on Leo's worker at 5.3 s
    assert any("Leo's turn" in f for f in mo.check_m6_agent(a))  # and both of Leo's agent calls engine-throttled
    d["after"]["leo_total"] = {"ok": 30}
    for x in a["leo"]["decisions"]:
        x.update({"decision": "ALLOWED"})
        x.pop("limit", None)
    assert mo.check_m6_direct(d) == []
    a["maya"]["decisions"] = [x for x in a["maya"]["decisions"] if x.get("limit") == "rate-limit"]
    assert mo.check_m6_agent(a) == []
    d["before"]["leo_total"] = {"ok": 11, "policy_eval": 1}  # before storm: no throttle of any kind on Leo
    assert any("before storm: Leo" in f for f in mo.check_m6_direct(d))


def test_m5b_recorded_retry_decline_is_red():
    a = m("M5b")["agent"]
    assert mo.m5b_retry_outcome(a["retry"]) == "model_declined"
    assert any("retry" in f for f in mo.check_m5b_agent(a))
    a["approval_rows"] = [{"requester": "maya", "approver": "leo"}]
    assert any("approval" in f for f in mo.check_m5b_agent(a))


def test_m3_and_r12_and_m5c_negatives():
    a = m("M3")["agent"]
    a["audit_after"][0]["actor"] = "none"
    assert any("audit" in f for f in mo.check_m3_agent(a))
    d = m("R1.2")["direct"]
    d["step"]["ready"] = d["step"]["ready"].replace("tools/call 401", "tools/call 200", 1)
    assert any("no token" in f for f in mo.check_r12_direct(d))
    a = m("M5c")["agent"]
    a["maya"]["tools"].append({"tool": "MemberRecords___initiate_transfer", "input": {}})
    assert any("initiate_transfer" in f for f in mo.check_m5c_agent(a))


def test_p95_matches_the_live_test_formula():
    assert mo.p95(list(range(1, 21))) == 19 and mo.p95([3.0]) == 3.0 and mo.p95([]) is None
