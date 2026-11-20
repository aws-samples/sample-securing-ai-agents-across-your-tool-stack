"""M5c fallback: the RESPONSE interceptor's prompt guard on partner referrals, live.

  .venv/bin/python -m pytest -q tests/test_prompt_guard_live.py     (about 2 minutes; start state required)

Direct calls through the Gateway (gklib.Mcp, no model). Positives: Maya's list with the explicit R-2003 published,
withheld 10 of 10 (HTTP 403, -32002, the interceptor named in the message). Negatives: Leo's clean referrals 10 of 10,
and Maya's list with only the business-style R-2002 (R-2003 held) 10 of 10, so M5 is not disturbed while the guard is
on. Before state: guard off, the explicit referral reaches the caller. Trail rows name the layer. Evidence:
logs/prompt-guard-<epoch>.json. Restores R-2003 held and the guard on.
"""
import time

from boto3.dynamodb.conditions import Key

import gklib as gk
from state import PREFIX

TOOL = "Partner___partner_list_referrals"
RUNS = 10


def partner(user, ps=None):
    m = gk.Mcp(gk.access(user), policy_session=ps or "new")
    msg = m.call(TOOL, {})
    err = (msg or {}).get("error") or {}
    text = " ".join(c.get("text", "") for c in ((msg or {}).get("result") or {}).get("content") or [])
    return {"http": m.last_status, "code": err.get("code"), "message": err.get("message", "")[:160], "ps": m.ps,
            "ids": [r for r in ("R-2001", "R-2002", "R-2003", "R-3001", "R-3002") if r in text]}


def referral(status):
    return gk.invoke_reset({"action": "referral_status", "owner": "maya", "referral_id": "R-2003", "status": status})


def test_prompt_guard_calibration_and_trail():
    out = {}
    try:
        gk.ssm_put(f"{PREFIX}/prompt-guard", "on")
        referral("held")
        time.sleep(6)  # 5-second parameter cache in the interceptor
        out["maya_business_only"] = [partner("maya") for _ in range(RUNS)]
        referral("open")
        out["maya_explicit"] = [partner("maya") for _ in range(RUNS)]
        out["leo"] = [partner("leo") for _ in range(RUNS)]
        gk.ssm_put(f"{PREFIX}/prompt-guard", "off")
        time.sleep(6)
        out["maya_explicit_guard_off"] = partner("maya")
    finally:
        gk.ssm_put(f"{PREFIX}/prompt-guard", "on")
        referral("held")
    sup = [r for r in out["maya_explicit"] if r["http"] == 403 and r["code"] == -32002 and "response interceptor" in r["message"]]
    out["summary"] = {"maya_explicit_withheld": f"{len(sup)}/{RUNS}",
                      "leo_returned": f"{sum(1 for r in out['leo'] if r['http'] == 200 and 'R-3001' in r['ids'])}/{RUNS}",
                      "maya_business_returned": f"{sum(1 for r in out['maya_business_only'] if r['http'] == 200 and 'R-2002' in r['ids'])}/{RUNS}"}
    time.sleep(2)
    t = gk.SESSION.resource("dynamodb").Table("gk-events")
    rows = t.query(KeyConditionExpression=Key("pk").eq(f"TRAIL#{out['maya_explicit'][0]['ps']}"))["Items"]
    out["trail_suppressed"] = [{k: r.get(k) for k in ("tool", "decision", "layer", "control", "http_status", "error_code")} for r in rows]
    rows = t.query(KeyConditionExpression=Key("pk").eq(f"TRAIL#{out['leo'][0]['ps']}"))["Items"]
    out["trail_leo"] = [{k: r.get(k) for k in ("tool", "decision", "layer")} for r in rows]
    print(out["summary"], "evidence", gk.save("prompt-guard", out))
    assert len(sup) == RUNS, out["maya_explicit"]
    assert all(r["http"] == 200 and "R-3001" in r["ids"] for r in out["leo"]), out["leo"]
    assert all(r["http"] == 200 and "R-2002" in r["ids"] and "R-2003" not in r["ids"] for r in out["maya_business_only"]), out["maya_business_only"]
    off = out["maya_explicit_guard_off"]
    assert off["http"] == 200 and "R-2003" in off["ids"], off
    assert any(r["decision"] == "SUPPRESSED" and r["layer"] == "interceptor" and r["control"] == "prompt-guard" for r in out["trail_suppressed"])
    assert any(r["decision"] == "ALLOWED" for r in out["trail_leo"])
