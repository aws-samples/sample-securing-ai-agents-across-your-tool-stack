"""Rehearsal moments: one definition per moment, shared by scripts/rehearse.py and, later, by the
fallback clip recording through the console.

Each moment has the pre-filled presenter prompts (console/prompts.py, so the harness sends exactly what the presenters
send), the step scripts that move it from its before to its after state (scripts/steps/*, run as commands, as on
stage), and pure assertion functions (check_*) over recorded data: agent turn summaries from scripts/agentlib.py (the
console's Runtime client), direct calls through scripts/gklib.py (the shared classifier in agent/gk_mcp), TRAIL# and AUDIT#
rows, and step output. A check_* function takes plain dicts and returns a list of failure strings (empty means green);
tests/test_rehearse_assertions.py runs them offline on recorded fixtures. Assertions are on tool calls, tool results,
decision labels and trail rows, never on model prose, except M4's draft check.

Rules kept here: M3 uses service mode and act-on-behalf only (impersonation is a slide); M5c is the RESPONSE
interceptor guard; no policy is added to the demo engine; nothing here prints or stores a token, password or raw
Gateway log record.
"""
import base64
import collections
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid

import requests
from boto3.dynamodb.conditions import Attr, Key

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.dirname(SCRIPTS)
for p in (SCRIPTS, DEMO):
    if p not in sys.path:
        sys.path.insert(0, p)

import agentlib as al  # noqa: E402
import consent_login  # noqa: E402
import gklib as gk  # noqa: E402
from console.prompts import PROMPTS  # noqa: E402
from state import DEMO_RATE, grant  # noqa: E402

EVENTS_TABLE = "gk-events"  # the stack's events table (same name in every Region; tables are Regional)
SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")  # any unmasked SSN, including the seeded 9\d{2}-\d{2}-\d{4} numbers
DIGITS12 = re.compile(r"\d{12}")
FULL_NUMBERS = ("610044124412", "610098769876", "610022012201")  # the seeded full account numbers
AGENT_URN = "urn:anycompany:agent:member-navigator"
MAYA_REFS, LEO_REFS = ["R-2001", "R-2002"], ["R-3001", "R-3002"]
RATE_KEY = "per-staff-member-records"
ENGINE_TROUBLE = ("internal error occurred", "canary failed", "update_failed", "create_failed")
ORDER = ["R1.2", "M1", "M2", "M2-CT", "M3", "M4", "M5", "M5c", "M5b", "M6"]
GUARDRAIL_MOMENTS = ("M4", "M5c")
# Each moment's state changes in talk order, from its before state to its after state: the one source for the runners
# below and for scripts/goto.py, which applies every earlier moment's after-steps to reach a moment's before state.
# ("consent", user) is that persona's portal consent (Playwright, consent_login.connect); every other entry is a step
# script with its arguments. M2's Disconnect and reconnect are a round trip inside the moment (Maya is connected before
# and after), so M2 changes no state; R1.2 and M2-CT only check. m3_mode.py never gets "impersonation" (a slide).
AFTER_STEPS = {
    "R1.2": [],
    "M1": [("consent", "maya")],
    "M2": [],
    "M2-CT": [],
    "M3": [("steps/m3_mode.py", "act_on_behalf")],
    "M4": [("steps/m4_mask.py",), ("steps/m4_guardrail.py",)],
    "M5": [("steps/m5_enforce.py",)],
    "M5c": [("steps/m5c_guardrail.py",)],
    "M5b": [("steps/m5b_enforce.py",)],
    "M6": [("steps/m6_limit.py",)],
}
MODE_SWITCH_SCRIPTS = ("reset.py", "state.py", "steps/m4_guardrail.py", "steps/m5_enforce.py", "steps/m5b_enforce.py")


class EngineStop(Exception):
    """A policy mode switch or the canary failed with an engine error: stop at once (policy engine)."""


def utc(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t or time.time()))


def prompt(pane, label):
    return next(p["prompt"] for p in PROMPTS[pane] if p["label"] == label)


def short(tool):
    return str(tool or "").split("___")[-1]


def jwt_sub(token):
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))["sub"]


def names(policies, base):
    return any(base in str(p) for p in policies or [])


# ---------------------------------------------------------------- recording helpers (live)
def text_facts(text, portal_host=None):
    text = text or ""
    return {"ssn": bool(SSN_RE.search(text)), "full_numbers": [n for n in FULL_NUMBERS if n in text], "digits12": bool(DIGITS12.search(text)),
            "url": "http://" in text or "https://" in text, "portal_host": bool(portal_host and portal_host in text),
            "refs": sorted(set(re.findall(r"\bR-\d{4}\b", text))), "ext4471": "EXT-4471" in text,
            "ext6193": "EXT-6193" in text}  # R-2002's account (M5) and R-2003's (M5c, since 2026-11-11)


def compact(r):
    """A direct call (gklib.call shape) reduced to what the assertions need; no raw result text is kept."""
    sc = r.get("sc") if isinstance(r.get("sc"), dict) else {}
    d = r.get("decision") or {}
    text = r.get("text") or ""
    hosts = sorted(set(re.findall(r"https://([^/\s\"'\\]+)", json.dumps(r.get("err") or {}))))
    return {"http": r.get("http"), "decision": d.get("decision"), "policies": d.get("policies") or [], "layer": d.get("layer"),
            "limit": d.get("limit"), "limit_key": d.get("limit_key"), "reason": str(d.get("reason") or "")[:120], "err_code": r.get("err_code"),
            "err_message": gk.mask(r.get("err_message") or "")[:200],
            "facts": {**text_facts(text), "ssn_masked": "***-**-" in text, "status": sc.get("status"), "sc_reason": str(sc.get("reason") or "")[:120],
                      "owner": sc.get("owner"), "referral_ids": sorted(str(x.get("referral_id")) for x in sc.get("referrals") or [] if isinstance(x, dict)),
                      "loan_ref": bool(sc.get("loan_ref")), "approved": sc.get("approved"), "elicitation_hosts": hosts}}


def mr(m, tool, args):
    """A MemberRecords call on an open gklib.Mcp, compacted; keeps the refs a later call in the same session needs."""
    r = gk.call(m, tool, args)
    c = compact(r)
    sc = r.get("sc") if isinstance(r.get("sc"), dict) else {}
    c["refs"] = {k: sc.get(k) for k in ("savings_ref", "checking_ref") if sc.get(k)}
    return c


def partner(token, ps="new"):
    m = gk.Mcp(token, policy_session=ps)
    msg = m.call("Partner___partner_list_referrals", {})
    sc, text, err = gk.result_obj(msg)
    e = (msg or {}).get("error") or {}
    return compact({"http": m.last_status, "sc": sc, "err": e or err, "err_code": e.get("code"), "err_message": e.get("message", ""),
                    "text": text, "decision": gk.classify(msg)})


def table():
    return gk.SESSION.resource("dynamodb").Table(EVENTS_TABLE)


def trail(ps, since_ms):
    if not ps:
        return []
    items, kw = [], {"KeyConditionExpression": Key("pk").eq(f"TRAIL#{ps}") & Key("sk").gt(str(int(since_ms)))}
    while True:
        r = table().query(**kw)
        items += r["Items"]
        if "LastEvaluatedKey" not in r:
            break
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
    return [{"tool": short(i.get("tool")), "decision": i.get("decision"), "layer": i.get("layer"), "http_status": int(i.get("http_status") or 0),
             "policy_ids": list(i.get("policy_ids") or []), "masked_fields": list(i.get("masked_fields") or []), "control": i.get("control")}
            for i in sorted(items, key=lambda x: x["sk"])]


def scan(prefix, t_from, t_to=None):
    items, kw = [], {"FilterExpression": Attr("pk").begins_with(prefix) & Attr("ts").gte(int(t_from))}
    while True:
        r = table().scan(**kw)
        items += r["Items"]
        if "LastEvaluatedKey" not in r:
            break
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
    return [i for i in items if t_to is None or int(i.get("ts", 0)) <= int(t_to)]


class Ctx:
    """One rehearsal run: tokens, one Runtime conversation per pane (one microVM per pane per run, as the console keeps
    them), the step runner, latencies and the mode-switch log for an engine stop."""

    def __init__(self, path, out_dir, tag, leo_delay=0.0, runtime_not_before=0.0):
        self.path, self.out_dir, self.tag, self.leo_delay = path, out_dir, tag, leo_delay
        self.agent, self.direct = path in ("agent", "both"), path in ("direct", "both")
        self.tokens = {u: gk.access(u) for u in ("maya", "leo")}
        self.subs = {u: jwt_sub(gk.staff_auth(u, "approver" if u == "dana" else "console")["IdToken"]) for u in ("maya", "leo", "dana")}
        self.portal_host = urllib.parse.urlparse(gk.out()["PortalUrl"]).netloc
        self.convs, self.latencies, self.mode_switches, self.deferred = {}, [], [], {}
        self.runtime_not_before = runtime_not_before
        self.consent_start = None

    def who(self, sub):
        return "none" if sub in (None, "", "none") else next((u for u, s in self.subs.items() if s == sub), "other")

    def conv(self, persona):
        if persona not in self.convs:
            self.convs[persona] = al.Conversation(persona, token=self.tokens[persona])
        return self.convs[persona]

    def turn(self, persona, label, new=False, text=None):
        """One agent turn with the console's pre-filled prompt for label (text replaces the prompt, for experiments)."""
        pane = "maya" if persona == "maya" else "right"
        conv = self.conv(persona)
        if new:
            conv.new(keep_runtime_session=True)  # new conversation and policy session, same warm microVM
        wait = self.runtime_not_before - time.time()
        if wait > 0:  # raise the limit at least a minute before any Runtime test
            time.sleep(wait)
        t0 = time.time()
        s = dict(conv.turn(text if text is not None else prompt(pane, label)))
        s.update({"persona": persona, "label": label, "t_start": round(t0, 2), "facts": text_facts(s.get("text"), self.portal_host),
                  "consent_hosts": [urllib.parse.urlparse(u or "").netloc for u in s.pop("consent", None) or []]})
        if s.get("after_reset_of"):
            first = {k: v for k, v in s["after_reset_of"].items() if k != "consent"}
            s["after_reset_of"] = first
            self.latencies.append({"label": f"{persona}: {label} (stale attempt)", "latency_s": first.get("latency_s"), "first_event_s": first.get("first_event_s")})
        self.latencies.append({"label": f"{persona}: {label}", "latency_s": s.get("latency_s"), "first_event_s": s.get("first_event_s")})
        return s

    def trail_of(self, s, wait=1.5):
        time.sleep(wait)
        return trail(s.get("policy_session_id"), int(s["t_start"] * 1000) - 2000)

    def step(self, script, *args, timeout=300):
        return run_step(script, *args, timeout=timeout, mode_switches=self.mode_switches)


def run_step(script, *args, timeout=300, mode_switches=None):
    """Run one step script (or reset.py, state.py) as a command, as on stage, and return its record: rc, seconds, the
    READY line, the saved evidence path and a masked output tail. Scripts that switch policy modes are appended to
    mode_switches (start and end UTC, for an engine stop); an engine error in the output raises EngineStop."""
    t0, start = time.time(), utc()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run([sys.executable, os.path.join(SCRIPTS, script), *args], cwd=DEMO, env=env, capture_output=True, text=True, timeout=timeout)  # fixed argv list, no shell  # nosec B603  # nosemgrep
    out, err = p.stdout or "", p.stderr or ""
    rec = {"script": script, "args": list(args), "utc": start, "utc_end": utc(), "rc": p.returncode, "seconds": round(time.time() - t0, 1),
           "ready": next((ln for ln in reversed(out.splitlines()) if ln.startswith("READY")), None),
           "saved": next((ln[6:].strip() for ln in reversed(out.splitlines()) if ln.startswith("saved ")), None),
           "tail": [gk.mask(ln)[:240] for ln in out.splitlines()[-6:]], "stderr_tail": gk.mask(err[-600:])}
    if mode_switches is not None and script in MODE_SWITCH_SCRIPTS:
        mode_switches.append({"script": script, "args": list(args), "start": start, "end": rec["utc_end"], "rc": p.returncode})
    if any(k in (out + err).lower() for k in ENGINE_TROUBLE):
        raise EngineStop(json.dumps(rec)[:1500])
    return rec


def new_rec(mid):
    return {"id": mid, "steps": [], "agent": {}, "direct": {}, "failures": {"agent": [], "direct": []}, "notes": []}


def turn_ok(s, label):
    if not s:
        return [f"{label}: no turn recorded"]
    f = []
    if s.get("http") != 200:
        f.append(f"{label}: Runtime HTTP {s.get('http')}")
    if s.get("errors"):
        f.append(f"{label}: agent error {json.dumps(s['errors'][:1])[:160]}")
    return f


def dec(s, tool=None):
    return [d for d in (s or {}).get("decisions", []) if tool is None or short(d.get("tool")) == tool]


def called(s, tool):
    return [t for t in (s or {}).get("tools", []) if short(t.get("tool")) == tool]


def last(s, tool):
    d = dec(s, tool)
    return d[-1] if d else {}


def step_ready(rec, label):
    return [] if rec and rec.get("rc") == 0 and rec.get("ready") else [f"{label}: step not READY (rc {rec and rec.get('rc')}): {rec and rec.get('tail')}"]


# ---------------------------------------------------------------- assertions (pure)
R12_RE = {"no token": r"no token: tools/list (\d+), tools/call (\d+)", "approver client": r"approver client \(not allowed\): tools/list (\d+), tools/call (\d+)",
          "foreign issuer": r"foreign issuer: tools/list (\d+), tools/call (\d+)"}
R12_WANT = {"no token": (401, 401), "approver client": (403, 403), "foreign issuer": (403, 403)}  # as built


def check_r12_direct(d):
    f = step_ready(d.get("step"), "m1_unauth")
    line = (d.get("step") or {}).get("ready") or ""
    for k, rx in R12_RE.items():
        m = re.search(rx, line)
        got = tuple(int(x) for x in m.groups()) if m else None
        if got != R12_WANT[k]:
            f.append(f"R1.2 {k}: got {got}, want {R12_WANT[k]}")
    if "no audit row" not in line:
        f.append("R1.2: an audit row was written")
    if d.get("trail_rows"):
        f.append(f"R1.2: {len(d['trail_rows'])} trail rows for the refused calls")
    return f


def check_r12_agent(a):
    f = []
    for k in ("no token", "approver client token"):
        r = a.get(k) or {}
        if r.get("http") not in (401, 403) or r.get("events"):
            f.append(f"Runtime front door, {k}: HTTP {r.get('http')}, {r.get('events')} events")
    return f


def check_m1_agent(a, portal_host):
    b, af = a.get("before"), a.get("after")
    f = turn_ok(b, "M1 before") + turn_ok(af, "M1 after")
    if last(b, "partner_list_referrals").get("decision") != "CONSENT_REQUIRED":
        f.append(f"M1 before: partner decision {dec(b, 'partner_list_referrals')}")
    if not (b or {}).get("consent_hosts") or any(h != portal_host for h in b["consent_hosts"]):
        f.append(f"M1 before: consent event hosts {(b or {}).get('consent_hosts')}")
    if (b or {}).get("facts", {}).get("url") or (b or {}).get("facts", {}).get("portal_host"):
        f.append("M1 before: the consent URL reached the model's reply")
    if last(af, "partner_list_referrals").get("decision") != "ALLOWED":
        f.append(f"M1 after: partner decision {dec(af, 'partner_list_referrals')}")
    refs = (af or {}).get("facts", {}).get("refs", [])
    if not set(refs) & set(MAYA_REFS) or set(refs) & set(LEO_REFS + ["R-2003"]):
        f.append(f"M1 after: referrals in the reply {refs}")
    if (a.get("consent") or {}).get("grant_after") != "connected":
        f.append(f"M1: grant after consent {(a.get('consent') or {}).get('grant_after')}")
    return f


def check_m1_direct(d, portal_host):
    b, af = d.get("before") or {}, d.get("after") or {}
    f = []
    if b.get("decision") != "CONSENT_REQUIRED" or b.get("err_code") != -32042 or portal_host not in b.get("facts", {}).get("elicitation_hosts", []):
        f.append(f"M1 direct before: {b.get('decision')} {b.get('err_code')} hosts {b.get('facts', {}).get('elicitation_hosts')}")
    if af.get("decision") != "ALLOWED" or af.get("facts", {}).get("owner") != "maya" or af.get("facts", {}).get("referral_ids") != MAYA_REFS:
        f.append(f"M1 direct after: {af.get('decision')} owner {af.get('facts', {}).get('owner')} ids {af.get('facts', {}).get('referral_ids')}")
    if (d.get("consent") or {}).get("grant_after") != "connected":
        f.append(f"M1: grant after consent {(d.get('consent') or {}).get('grant_after')}")
    return f


def check_m2_agent(a):
    maya, leo = a.get("maya"), a.get("leo")
    f = turn_ok(maya, "M2 Maya") + turn_ok(leo, "M2 Leo")
    if last(leo, "partner_list_referrals").get("decision") != "ALLOWED":
        f.append(f"M2 Leo: partner decision {dec(leo, 'partner_list_referrals')}")
    lr, mrf = (leo or {}).get("facts", {}).get("refs", []), (maya or {}).get("facts", {}).get("refs", [])
    if not set(lr) & set(LEO_REFS) or set(lr) & set(MAYA_REFS + ["R-2003"]):
        f.append(f"M2 Leo's reply names {lr}")
    if not set(mrf) & set(MAYA_REFS) or set(mrf) & set(LEO_REFS):
        f.append(f"M2 Maya's reply names {mrf}")
    return f


def check_m2_direct(d):
    m, lo = d.get("maya") or {}, d.get("leo") or {}
    f = []
    if m.get("decision") != "ALLOWED" or m.get("facts", {}).get("owner") != "maya" or m.get("facts", {}).get("referral_ids") != MAYA_REFS:
        f.append(f"M2 direct Maya: {m.get('decision')} {m.get('facts', {}).get('owner')} {m.get('facts', {}).get('referral_ids')}")
    if lo.get("decision") != "ALLOWED" or lo.get("facts", {}).get("owner") != "leo" or lo.get("facts", {}).get("referral_ids") != LEO_REFS:
        f.append(f"M2 direct Leo: {lo.get('decision')} {lo.get('facts', {}).get('owner')} {lo.get('facts', {}).get('referral_ids')}")
    if set(m.get("facts", {}).get("referral_ids", [])) & set(lo.get("facts", {}).get("referral_ids", [])):
        f.append("M2 direct: Maya's and Leo's referral sets overlap")
    f += step_ready(d.get("disconnect"), "m2_disconnect")
    if (d.get("reconnect") or {}).get("grant_after") != "connected":
        f.append(f"M2: Maya's grant after reconnect {(d.get('reconnect') or {}).get('grant_after')}")
    return f


def check_m2_cloudtrail(d):
    """R2.8, this run's events only: at least one GetResourceOauth2Token for the partner provider at or after this run's
    M1 consent, within the bounded wait (CloudTrail delivers minutes late)."""
    if not d.get("found_vault_event"):
        return [f"M2 CloudTrail: no GetResourceOauth2Token for gk-counseling-partner since {d.get('since_utc')} after {d.get('waited_s')} s"]
    return []


def check_m3_agent(a):
    f = turn_ok(a.get("before"), "M3 before") + turn_ok(a.get("after_maya"), "M3 after Maya") + turn_ok(a.get("after_leo"), "M3 after Leo")
    if last(a.get("before"), "get_loan_file").get("decision") != "ALLOWED":
        f.append(f"M3 service mode: loan file decision {dec(a.get('before'), 'get_loan_file')}")
    dm = last(a.get("after_maya"), "get_loan_file")
    if dm.get("decision") != "DENIED_TOOL" or "lending" not in str(dm.get("reason", "")):
        f.append(f"M3 act-on-behalf Maya: {dm}")
    if last(a.get("after_leo"), "get_loan_file").get("decision") != "ALLOWED":
        f.append(f"M3 act-on-behalf Leo: {dec(a.get('after_leo'), 'get_loan_file')}")
    return f + check_m3_audit(a.get("audit_before"), a.get("audit_after"), "agent")


def check_m3_direct(d):
    f = []
    b, am, al_ = d.get("before") or {}, d.get("after_maya") or {}, d.get("after_leo") or {}
    if b.get("decision") != "ALLOWED" or not b.get("facts", {}).get("loan_ref"):
        f.append(f"M3 direct service mode: {b.get('decision')} loan_ref {b.get('facts', {}).get('loan_ref')}")
    if am.get("facts", {}).get("status") != "DENIED" or "lending" not in am.get("facts", {}).get("sc_reason", ""):
        f.append(f"M3 direct act-on-behalf Maya: status {am.get('facts', {}).get('status')} {am.get('facts', {}).get('sc_reason')}")
    if al_.get("decision") != "ALLOWED" or not al_.get("facts", {}).get("loan_ref"):
        f.append(f"M3 direct act-on-behalf Leo: {al_.get('decision')}")
    return f + check_m3_audit(d.get("audit_before"), d.get("audit_after"), "direct")


def check_m3_audit(before, after, path):
    f = []
    if not before or any(r["mode"] != "service" or r["subject"] != "none" or r["decision"] != "ALLOWED" for r in before):
        f.append(f"M3 {path} audit, service mode: {before}")
    maya = [r for r in after or [] if r["subject"] == "maya"]
    leo = [r for r in after or [] if r["subject"] == "leo"]
    if not maya or any(r["mode"] != "act_on_behalf" or r["decision"] != "DENIED" or r["actor"] != AGENT_URN for r in maya):
        f.append(f"M3 {path} audit, act-on-behalf Maya: {maya}")
    if not leo or any(r["mode"] != "act_on_behalf" or r["decision"] != "ALLOWED" or r["actor"] != AGENT_URN for r in leo):
        f.append(f"M3 {path} audit, act-on-behalf Leo: {leo}")
    if any(r["subject"] not in ("maya", "leo") for r in after or []):
        f.append(f"M3 {path} audit after: unexpected subject {[r['subject'] for r in after]}")
    return f


def check_m4_agent(a):
    b, af = a.get("before"), a.get("after")
    f = turn_ok(b, "M4 before") + turn_ok(af, "M4 after")
    bt, at = a.get("before_trail") or [], a.get("after_trail") or []
    prof = [r for r in bt if r["tool"] == "get_member_profile"]
    if not prof or any(r["decision"] != "ALLOWED" or r["masked_fields"] for r in prof):
        f.append(f"M4 before: profile trail rows {prof} (masking off: the SSN reaches the model)")
    if any(r["decision"] in ("REDACTED", "SUPPRESSED") for r in bt) or any(d["decision"] == "SUPPRESSED" for d in dec(b)):
        f.append("M4 before: a REDACTED or SUPPRESSED decision while masking is off and the guardrail logs only")
    n = last(af, "get_member_notes")
    if n.get("decision") != "SUPPRESSED" or not names(n.get("policies"), "SuppressSsnInNotes"):
        f.append(f"M4 after: notes decision {dec(af, 'get_member_notes')}")
    fx = (af or {}).get("facts", {})
    if fx.get("ssn") or fx.get("full_numbers") or fx.get("digits12"):
        f.append(f"M4 after: the draft carries ssn={fx.get('ssn')} full_numbers={fx.get('full_numbers')} digits12={fx.get('digits12')}")
    if not any(r["decision"] == "REDACTED" for r in at):
        f.append("M4 after: no REDACTED trail row")
    if not any(r["decision"] == "SUPPRESSED" and r["tool"] == "get_member_notes" for r in at):
        f.append("M4 after: no SUPPRESSED trail row for the notes")
    return f


def check_m4_direct(d):
    b, af = d.get("before") or {}, d.get("after") or {}
    f = []
    p, ac, n = b.get("profile") or {}, b.get("accounts") or {}, b.get("notes") or {}
    if p.get("decision") != "ALLOWED" or not p.get("facts", {}).get("ssn"):
        f.append("M4 direct before: the profile SSN is not unmasked")
    if ac.get("decision") != "ALLOWED" or not ac.get("facts", {}).get("full_numbers"):
        f.append("M4 direct before: no full account number in the accounts result")
    if n.get("decision") != "ALLOWED" or not n.get("facts", {}).get("ssn"):
        f.append(f"M4 direct before: notes {n.get('decision')} (LOG_ONLY: returned with the SSN)")
    p, ac, n = af.get("profile") or {}, af.get("accounts") or {}, af.get("notes") or {}
    if n.get("http") != 403 or n.get("err_code") != -32002 or n.get("decision") != "SUPPRESSED" or not names(n.get("policies"), "SuppressSsnInNotes"):
        f.append(f"M4 direct after: notes {n.get('http')} {n.get('err_code')} {n.get('decision')} {n.get('policies')}")
    if not p.get("facts", {}).get("ssn_masked") or p.get("facts", {}).get("ssn"):
        f.append("M4 direct after: profile SSN not masked")
    if ac.get("facts", {}).get("full_numbers") or ac.get("facts", {}).get("digits12"):
        f.append("M4 direct after: a full account number in the accounts result")
    return f


def m5_outcome(turns, half):
    """M5 agent-path outcome for one half: executed (LOG_ONLY), gateway_denied (ACTIVE), model_declined, or other."""
    calls = [t for s in turns for t in called(s, "initiate_transfer")]
    ds = [d for s in turns for d in dec(s, "initiate_transfer")]
    if not calls:
        return "model_declined"
    to_ext = [t for t in calls if str((t.get("input") or {}).get("destination_ref")) == "EXT-4471"]
    if not to_ext:
        return "other"
    if half == "LOG_ONLY":
        return "executed" if any(d.get("decision") == "ALLOWED" for d in ds) else "other"
    return "gateway_denied" if ds and all(d.get("decision") == "DENIED" and names(d.get("policies"), "DestinationSeenInSession") for d in ds) else "other"


def check_m5_agent(a):
    f = []
    for half in ("LOG_ONLY", "ACTIVE"):
        h = a.get(half) or {}
        turns = [h.get("turn1"), h.get("turn2")]
        f += turn_ok(turns[0], f"M5 {half} turn 1") + turn_ok(turns[1], f"M5 {half} turn 2")
        out = m5_outcome([t for t in turns if t], half)
        want = "executed" if half == "LOG_ONLY" else "gateway_denied"
        if out != want:
            f.append(f"M5 {half}: outcome {out}, want {want}")
        rows = h.get("transfers_to_ext") or []
        if half == "LOG_ONLY" and not rows:
            f.append("M5 LOG_ONLY: no TRANSFER row to EXT-4471 (the transfer did not execute)")
        if half == "ACTIVE" and rows:
            f.append(f"M5 ACTIVE: {len(rows)} TRANSFER rows to EXT-4471")
    return f + check_would_deny(a.get("would_deny"))


def check_m5_direct(d):
    f = []
    lo, ac = (d.get("LOG_ONLY") or {}), (d.get("ACTIVE") or {})
    t = lo.get("transfer") or {}
    if t.get("decision") != "ALLOWED" or t.get("facts", {}).get("status") != "EXECUTED" or not lo.get("transfers_to_ext"):
        f.append(f"M5 direct LOG_ONLY: {t.get('decision')} {t.get('facts', {}).get('status')} rows {len(lo.get('transfers_to_ext') or [])}")
    t = ac.get("transfer") or {}
    if t.get("err_code") != -32002 or t.get("decision") != "DENIED" or not names(t.get("policies"), "DestinationSeenInSession"):
        f.append(f"M5 direct ACTIVE: {t.get('decision')} {t.get('err_code')} {t.get('policies')}")
    if ac.get("transfers_to_ext"):
        f.append(f"M5 direct ACTIVE: {len(ac['transfers_to_ext'])} TRANSFER rows to EXT-4471")
    return f + check_would_deny(d.get("would_deny"))


def check_would_deny(w):
    """R6.9: the would-deny record in the Gateway application log, asserted only where the log group exists."""
    if not w or w.get("skipped"):
        return []
    return [] if w.get("found") else [f"M5 LOG_ONLY would-deny record not found in {w.get('group')} after {w.get('waited_s')} s"]


def check_m5c_agent(a):
    m, lo = a.get("maya"), a.get("leo")
    f = turn_ok(m, "M5c Maya") + turn_ok(lo, "M5c Leo")
    d = last(m, "partner_list_referrals")
    if d.get("decision") != "SUPPRESSED" or d.get("layer") != "interceptor":
        f.append(f"M5c Maya: partner decision {dec(m, 'partner_list_referrals')}")
    if called(m, "initiate_transfer"):
        f.append("M5c Maya: initiate_transfer followed the suppressed referral list")
    facts = (m or {}).get("facts", {})
    if "R-2003" in facts.get("refs", []) or facts.get("ext4471") or facts.get("ext6193"):
        f.append("M5c Maya: R-2003, EXT-4471 or EXT-6193 reached the reply")
    if not any(r["decision"] == "SUPPRESSED" and r["layer"] == "interceptor" and r.get("control") == "prompt-guard" for r in a.get("maya_trail") or []):
        f.append(f"M5c Maya: no interceptor prompt-guard trail row {a.get('maya_trail')}")
    if last(lo, "partner_list_referrals").get("decision") != "ALLOWED" or not set((lo or {}).get("facts", {}).get("refs", [])) & set(LEO_REFS):
        f.append(f"M5c Leo: {dec(lo, 'partner_list_referrals')} refs {(lo or {}).get('facts', {}).get('refs')}")
    return f


def check_m5c_direct(d):
    m, lo = d.get("maya") or {}, d.get("leo") or {}
    f = []
    if m.get("http") != 403 or m.get("err_code") != -32002 or m.get("decision") != "SUPPRESSED" or m.get("layer") != "interceptor":
        f.append(f"M5c direct Maya: {m.get('http')} {m.get('err_code')} {m.get('decision')} {m.get('layer')}")
    if lo.get("decision") != "ALLOWED" or lo.get("facts", {}).get("referral_ids") != LEO_REFS:
        f.append(f"M5c direct Leo: {lo.get('decision')} {lo.get('facts', {}).get('referral_ids')}")
    return f


def approval_ok(rows):
    return any(r.get("approver") == "dana" and r.get("requester") == "maya" for r in rows or [])


def check_m5b_agent(a):
    f = turn_ok(a.get("before"), "M5b before") + turn_ok(a.get("denied"), "M5b enforced") + turn_ok(a.get("retry"), "M5b retry")
    if last(a.get("before"), "initiate_transfer").get("decision") != "ALLOWED":
        f.append(f"M5b before (approval rule LOG_ONLY): {dec(a.get('before'), 'initiate_transfer')}")
    d = last(a.get("denied"), "initiate_transfer")
    if d.get("decision") != "DENIED" or not names(d.get("policies"), "ApprovalOver2500"):
        f.append(f"M5b enforced: {dec(a.get('denied'), 'initiate_transfer')}")
    ap = a.get("approval") or {}
    if ap.get("facts", {}).get("approved") is not True or not approval_ok(a.get("approval_rows")):
        f.append(f"M5b approval: approved {ap.get('facts', {}).get('approved')} {ap.get('facts', {}).get('sc_reason')} rows {a.get('approval_rows')}")
    if last(a.get("retry"), "initiate_transfer").get("decision") != "ALLOWED":
        f.append(f"M5b retry after approval: {dec(a.get('retry'), 'initiate_transfer')}")
    return f


def check_m5b_direct(d):
    f = []

    def is_denied(r):
        return r.get("err_code") == -32002 and r.get("decision") == "DENIED" and names(r.get("policies"), "ApprovalOver2500")

    b = d.get("before") or {}
    if b.get("decision") != "ALLOWED" or b.get("facts", {}).get("status") != "EXECUTED":
        f.append(f"M5b direct before: {b.get('decision')} {b.get('facts', {}).get('status')}")
    if not is_denied(d.get("denied") or {}):
        f.append(f"M5b direct enforced: {(d.get('denied') or {}).get('decision')} {(d.get('denied') or {}).get('policies')}")
    ns = d.get("non_supervisor") or {}
    if ns.get("facts", {}).get("approved") is not False or "not a supervisor" not in ns.get("facts", {}).get("sc_reason", ""):
        f.append(f"M5b direct non-supervisor approval: {ns.get('facts', {}).get('approved')} {ns.get('facts', {}).get('sc_reason')}")
    ap = d.get("approval") or {}
    if ap.get("facts", {}).get("approved") is not True or not approval_ok(d.get("approval_rows")):
        f.append(f"M5b direct approval: {ap.get('facts', {}).get('approved')} rows {d.get('approval_rows')}")
    if not is_denied(d.get("over") or {}):
        f.append(f"M5b direct $4,500 against a $4,000 approval: {(d.get('over') or {}).get('decision')}")
    ok = d.get("ok") or {}
    if ok.get("decision") != "ALLOWED" or ok.get("facts", {}).get("status") != "EXECUTED":
        f.append(f"M5b direct $4,000 after approval: {ok.get('decision')} {ok.get('facts', {}).get('status')}")
    return f


def leo_bad(storm):
    return {k: v for k, v in (storm or {}).get("leo_total", {}).items() if k != "ok"}


def check_m6_direct(d):
    f = []
    b, af = d.get("before") or {}, d.get("after") or {}
    if not b or b.get("maya_total", {}).get("rate_limit", 0) or not b.get("maya_total", {}).get("ok"):
        f.append(f"M6 before storm: Maya {b.get('maya_total')} (want no rate-limit throttle)")
    if not b or leo_bad(b) or not b.get("leo_total", {}).get("ok"):
        f.append(f"M6 before storm: Leo {b.get('leo_total')} (want no throttle of any kind)")
    if not af or not af.get("maya_total", {}).get("rate_limit"):
        f.append(f"M6 after storm: Maya {af.get('maya_total')} (want rate-limit throttles)")
    if not af or leo_bad(af) or not af.get("leo_total", {}).get("ok"):
        f.append(f"M6 after storm: Leo {af.get('leo_total')} (want every call allowed)")
    f += step_ready(d.get("limit"), "m6_limit")
    return f


def check_m6_agent(a):
    m, lo = a.get("maya"), a.get("leo")
    f = turn_ok(m, "M6 Maya") + turn_ok(lo, "M6 Leo")
    thr = [x for x in dec(m) if x.get("decision") == "THROTTLED" and x.get("limit") == "rate-limit"]
    if not thr or not all(str(x.get("limit_key", "")).startswith(RATE_KEY) for x in thr):
        f.append(f"M6 Maya's turn in the storm: {dec(m)} (want THROTTLED with limitKey {RATE_KEY})")
    if not dec(lo) or any(x.get("decision") != "ALLOWED" for x in dec(lo)):
        f.append(f"M6 Leo's turn in the storm: {dec(lo)} (want every call ALLOWED)")
    return f


def m5b_retry_outcome(s):
    """M5b agent path after the approval: executed, model_declined (no initiate_transfer call) or other."""
    if not called(s, "initiate_transfer"):
        return "model_declined"
    return "executed" if last(s, "initiate_transfer").get("decision") == "ALLOWED" else "other"


def p95(values):
    v = sorted(x for x in values if x is not None)
    return v[max(0, -(-95 * len(v) // 100) - 1)] if v else None


# ---------------------------------------------------------------- moments (live)
def run_r12(ctx):
    rec = new_rec("R1.2")
    t0 = time.time()
    s = ctx.step("steps/m1_unauth.py")
    rec["steps"].append(s)
    if ctx.direct:
        rows = [r for r in scan("TRAIL#", t0 - 1, time.time() + 1) if short(r.get("tool")) == "get_member_profile"]
        rec["direct"] = {"step": s, "trail_rows": [{"tool": short(r.get("tool")), "decision": r.get("decision")} for r in rows]}
    if ctx.agent:
        url, res = al.runtime_url(), {}
        for label, tok in (("no token", None), ("approver client token", gk.staff_auth("dana", "approver")["AccessToken"])):
            h = {"Content-Type": "application/json", "Accept": "text/event-stream", "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": f"gk-r12-{uuid.uuid4().hex}"}
            if tok:
                h["Authorization"] = f"Bearer {tok}"
            r = requests.post(url, headers=h, json={"prompt": prompt("maya", "M1 Referrals (consent)")}, timeout=30)
            res[label] = {"http": r.status_code, "events": r.text.count("data:"), "body": al.redact(gk.mask(r.text[:160]))}
        rec["agent"] = res
    return rec


def run_m1(ctx):
    rec = new_rec("M1")
    if ctx.direct:
        rec["direct"]["before"] = partner(ctx.tokens["maya"])
    if ctx.agent:
        rec["agent"]["before"] = ctx.turn("maya", "M1 Referrals (consent)", new=True)
    ctx.consent_start = time.time()
    c = consent_login.connect("maya")  # Playwright: portal sign-in, Connect, partner sign-in (passwords never printed)
    consent = {"utc": utc(ctx.consent_start), "seconds": round(time.time() - ctx.consent_start, 1),
               "partner_signin_page_shown": c.get("partner_signin_page_shown"), "grant_after": grant("maya")}
    rec["agent"]["consent"] = rec["direct"]["consent"] = consent
    if ctx.agent:
        rec["agent"]["after"] = ctx.turn("maya", "M1 Again after Connect")
        ctx.m1_maya_after = rec["agent"]["after"]
    if ctx.direct:
        rec["direct"]["after"] = partner(ctx.tokens["maya"])
    return rec


def run_m2(ctx):
    rec = new_rec("M2")
    if grant("maya") != "connected":  # only when M1 was not selected
        consent_login.connect("maya")
        rec["notes"].append("Maya connected first (M1 not run)")
    if ctx.agent:
        rec["agent"]["maya"] = getattr(ctx, "m1_maya_after", None) or ctx.turn("maya", "M1 Referrals (consent)", new=True)
        rec["agent"]["leo"] = ctx.turn("leo", "M2 Referrals", new=True)
    if ctx.direct:
        rec["direct"]["maya"], rec["direct"]["leo"] = partner(ctx.tokens["maya"]), partner(ctx.tokens["leo"])
    s = ctx.step("steps/m2_disconnect.py")  # R2.7, then Maya reconnects on the portal
    rec["steps"].append(s)
    t = time.time()
    if ctx.consent_start is None:
        ctx.consent_start = t
    c = consent_login.connect("maya")
    rec["direct"]["disconnect"] = s
    rec["direct"]["reconnect"] = {"utc": utc(t), "partner_signin_page_shown": c.get("partner_signin_page_shown"), "grant_after": grant("maya")}
    return rec


def cloudtrail_check(ctx, cap_s=900, every=60):
    """M2-CT: this run's vault events through the step script itself, bounded wait from this run's M1 consent."""
    since = ctx.consent_start or time.time()
    t0, out = time.time(), {"since_utc": utc(since)}
    while True:
        hours = (time.time() - since + 120) / 3600
        s = ctx.step("steps/m2_cloudtrail.py", f"{hours:.3f}", timeout=120)
        rows = [ln for ln in s["tail"] + [s.get("ready") or ""] if "UTC" in ln]
        vault = [ln for ln in rows if "GetResourceOauth2Token" in ln and "gk-counseling-partner" in ln]
        out.update({"step_rc": s["rc"], "ready": s["ready"], "rows_seen": len(rows), "vault_rows": len(vault),
                    "complete_rows": len([ln for ln in rows if "CompleteResourceTokenAuth" in ln]), "force_rows": len([ln for ln in rows if "force=True" in ln])})
        if vault:
            out.update({"found_vault_event": True, "waited_s": round(time.time() - t0)})
            return out
        if time.time() - since > cap_s:
            out.update({"found_vault_event": False, "waited_s": round(time.time() - t0)})
            return out
        time.sleep(every)


def audit(ctx, t_from, t_to, tool):
    rows = []
    for d in sorted({time.strftime("%Y-%m-%d", time.gmtime(t)) for t in (t_from, t_to)}):
        kw = {"KeyConditionExpression": Key("pk").eq(f"AUDIT#{d}") & Key("sk").between(str(int(t_from) * 1000), str(int(t_to) * 1000 + 999) + "~")}
        while True:
            r = table().query(**kw)
            rows += r["Items"]
            if "LastEvaluatedKey" not in r:
                break
            kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
    return [{"tool": r.get("tool"), "mode": r.get("mode"), "decision": r.get("decision"), "reason": r.get("reason"), "actor": r.get("actor"),
             "subject": ctx.who(r.get("subject_sub"))} for r in rows if r.get("tool") == tool]


def gap():
    time.sleep(1.3)  # audit and transfer rows carry whole seconds: keep each path's window apart


def run_m3(ctx):
    rec = new_rec("M3")
    loan = {"member_id": "M-1001"}
    t0 = time.time()
    if ctx.agent:
        rec["agent"]["before"] = ctx.turn("maya", "M3 Loan summary", new=True)
    gap()
    t1 = time.time()
    if ctx.direct:
        rec["direct"]["before"] = mr(gk.Mcp(ctx.tokens["maya"]), "get_loan_file", loan)
    gap()
    t2 = time.time()
    rec["agent"]["audit_before"], rec["direct"]["audit_before"] = audit(ctx, t0, t1 - 1, "get_loan_file"), audit(ctx, t1, t2 - 1, "get_loan_file")
    for s in AFTER_STEPS["M3"]:  # m3_mode.py act_on_behalf, never impersonation (a slide)
        rec["steps"].append(ctx.step(*s))
    gap()
    t3 = time.time()
    if ctx.agent:
        rec["agent"]["after_maya"] = ctx.turn("maya", "M3 Loan summary", new=True)
        rec["agent"]["after_leo"] = ctx.turn("leo", "M3 Loan summary", new=True)
    gap()
    t4 = time.time()
    if ctx.direct:
        rec["direct"]["after_maya"] = mr(gk.Mcp(ctx.tokens["maya"]), "get_loan_file", loan)
        rec["direct"]["after_leo"] = mr(gk.Mcp(ctx.tokens["leo"]), "get_loan_file", loan)
    gap()
    t5 = time.time()
    rec["agent"]["audit_after"], rec["direct"]["audit_after"] = audit(ctx, t3, t4 - 1, "get_loan_file"), audit(ctx, t4, t5 - 1, "get_loan_file")
    rec["notes"].append(f"step {rec['steps'][-1].get('ready')}")
    return rec


def m4_direct(ctx):
    m = gk.Mcp(ctx.tokens["maya"])
    a = {"member_id": "M-1001"}
    return {"profile": mr(m, "get_member_profile", a), "accounts": mr(m, "get_member_accounts", a), "notes": mr(m, "get_member_notes", a)}


def run_m4(ctx):
    rec = new_rec("M4")
    if ctx.agent:
        rec["agent"]["before"] = s = ctx.turn("maya", "M4 Draft a reply", new=True)
        rec["agent"]["before_trail"] = ctx.trail_of(s)
    if ctx.direct:
        rec["direct"]["before"] = m4_direct(ctx)
    for s in AFTER_STEPS["M4"]:  # m4_mask.py, then m4_guardrail.py
        rec["steps"].append(ctx.step(*s))
    if ctx.agent:
        rec["agent"]["after"] = s = ctx.turn("maya", "M4 Draft a reply", new=True)
        rec["agent"]["after_trail"] = ctx.trail_of(s)
    if ctx.direct:
        rec["direct"]["after"] = m4_direct(ctx)
    return rec


def transfers_to_ext(t_from, t_to=None):
    return [{"destination_ref": r.get("destination_ref"), "amount": int(r.get("amount", 0)), "t": int(r.get("ts", 0))}
            for r in scan("TRANSFER#", t_from, t_to) if r.get("destination_ref") == "EXT-4471"]


def m5_direct(ctx, amount=800):
    """The console's direct M5 call (console/core.py Console.direct): accounts, then $800 to EXT-4471, no model."""
    m = gk.Mcp(ctx.tokens["maya"])
    acc = mr(m, "get_member_accounts", {"member_id": "M-1001"})
    tr = mr(m, "initiate_transfer", {"member_id": "M-1001", "source_ref": acc["refs"].get("savings_ref", "unknown"), "destination_ref": "EXT-4471", "amount": amount})
    return {"accounts": {k: acc[k] for k in ("http", "decision")}, "transfer": tr}


def run_m5(ctx):
    rec = new_rec("M5")
    windows = {}
    for half in ("LOG_ONLY", "ACTIVE"):
        if half == "ACTIVE":
            for s in AFTER_STEPS["M5"]:  # m5_enforce.py, then new conversations in both panes (old sessions get -32005)
                rec["steps"].append(ctx.step(*s))
            gap()
        ta = time.time()
        if ctx.agent:
            t1 = ctx.turn("maya", "M5 Work the referrals", new=True)
            t2 = ctx.turn("maya", "M5 Presenter confirms")
            rec["agent"][half] = {"turn1": t1, "turn2": t2, "outcome": m5_outcome([t1, t2], half)}
        gap()
        tb = time.time()
        if ctx.direct:
            rec["direct"][half] = m5_direct(ctx)
        gap()
        tc = time.time()
        time.sleep(1)
        if ctx.agent:
            rec["agent"][half]["transfers_to_ext"] = transfers_to_ext(ta, tb - 1)
        if ctx.direct:
            rec["direct"][half]["transfers_to_ext"] = transfers_to_ext(tb, tc - 1)
        windows[half] = {k: v for k, v in (("agent", (ta, tb - 1)), ("direct", (tb, tc - 1))) if getattr(ctx, k)}
    ctx.deferred["would_deny"] = windows["LOG_ONLY"]
    return rec


def would_deny_check(ctx, windows, cap_s=300, every=30):
    """Optional evidence: the Gateway's policy decision record for the LOG_ONLY transfer lists the destination rule
    in body.policy.logOnlyDecisionFlippingPolicies (the rule applies only to initiate_transfer, so such a record is the
    transfer's). Read from the Gateway application log only where the group exists (-c gw_logs=on), per
    path within that path's LOG_ONLY window; only the policy fields and the event time are kept, never a record
    (records carry request bodies). Bounded wait from the start of the check."""
    group = f"/aws/vendedlogs/bedrock-agentcore/{gk.STACK}-gw"
    logs = gk.SESSION.client("logs")
    if not logs.describe_log_groups(logGroupNamePrefix=group).get("logGroups"):
        return {p: {"skipped": True, "group": group, "reason": "no Gateway application log group"} for p in windows}
    t0 = time.time()
    while True:
        res = {}
        for p, (a, b) in windows.items():
            hits, tok = [], None
            while True:
                kw = {"logGroupName": group, "startTime": int((a - 5) * 1000), "endTime": int((b + 5) * 1000), "filterPattern": '"logOnlyDecisionFlippingPolicies"'}
                if tok:
                    kw["nextToken"] = tok
                r = logs.filter_log_events(**kw)
                for e in r.get("events", []):
                    try:
                        pol = (json.loads(e["message"]).get("body") or {}).get("policy") or {}
                    except (ValueError, AttributeError):
                        continue
                    flip = pol.get("logOnlyDecisionFlippingPolicies") or []
                    if any("DestinationSeenInSession" in str(x) for x in flip):
                        hits.append({"t": round(e["timestamp"] / 1000.0, 1), "decision": pol.get("decision"), "flipping": [str(x)[:80] for x in flip],
                                     "determining": [str(x)[:80] for x in pol.get("determiningPolicies") or []]})
                tok = r.get("nextToken")
                if not tok:
                    break
            res[p] = {"group": group, "found": bool(hits), "records": hits[:2], "waited_s": round(time.time() - t0)}
        if all(r["found"] for r in res.values()) or time.time() - t0 > cap_s:
            return res
        time.sleep(every)


def find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, key)
            if r is not None:
                return r
    return None


def run_m5c(ctx):
    rec = new_rec("M5c")
    for s in AFTER_STEPS["M5c"]:  # m5c_guardrail.py
        rec["steps"].append(ctx.step(*s))
    if ctx.agent:
        rec["agent"]["maya"] = s = ctx.turn("maya", "M5c Referrals again", new=True)
        rec["agent"]["maya_trail"] = ctx.trail_of(s)
        rec["agent"]["leo"] = ctx.turn("leo", "M2 Referrals", new=True)  # Leo's clean list (the right pane has no M5c prompt)
    if ctx.direct:
        rec["direct"]["maya"], rec["direct"]["leo"] = partner(ctx.tokens["maya"]), partner(ctx.tokens["leo"])
    return rec


def approval_rows(ctx, t_from):
    return [{"destination_ref": r.get("destination_ref"), "amount": int(r.get("amount", 0)), "requester": ctx.who(r.get("requester_sub")),
             "approver": ctx.who(r.get("approver_sub"))} for r in scan("APPROVAL#", t_from)]


def approve(ctx, ps, req, approver="dana"):
    """The console's approval card (console/core.py Console.approve): the approver signs in with the approver client and
    record_transfer_approval runs in Maya's policy session."""
    idt = gk.staff_auth(approver, "approver")["IdToken"]
    m = gk.Mcp(ctx.tokens["maya"], policy_session=ps)
    r = mr(m, "record_transfer_approval", {"member_id": req.get("member_id", "M-1001"), "destination_ref": req.get("destination_ref"),
                                           "amount": req.get("amount"), "approver_id_token": idt})
    del idt
    return r


def m5b_agent_path(ctx, retry_text=None):
    """The M5b agent path with the approval rule ACTIVE: a new conversation, the "M5b $4,000 transfer" prompt (DENIED by
    the approval rule), Dana's approval recorded in that policy session (the console's approval card), then the retry
    prompt. retry_text replaces the console's retry prompt (scripts/experiments/m5b_retry_wording.py)."""
    out = {}
    t0 = time.time()
    conv = ctx.conv("maya")
    out["denied"] = s = ctx.turn("maya", "M5b $4,000 transfer", new=True)
    cand = (called(s, "initiate_transfer") or [{}])[-1].get("input") or {}
    out["approval_request"] = {k: cand.get(k) for k in ("member_id", "source_ref", "destination_ref", "amount")}
    out["approval"] = approve(ctx, conv.policy_session_id, cand) if cand else {"facts": {"approved": None, "sc_reason": "no denied transfer"}}
    out["approval_rows"] = approval_rows(ctx, t0)
    out["retry"] = ctx.turn("maya", "M5b Retry after approval", text=retry_text)
    return out


def run_m5b(ctx):
    rec = new_rec("M5b")
    m = gk.Mcp(ctx.tokens["maya"])
    acc = mr(m, "get_member_accounts", {"member_id": "M-1001"})
    req = {"member_id": "M-1001", "source_ref": acc["refs"].get("savings_ref"), "destination_ref": acc["refs"].get("checking_ref"), "amount": 4000}
    if ctx.direct:
        rec["direct"]["before"] = mr(m, "initiate_transfer", req)  # approval rule LOG_ONLY: executes
    if ctx.agent:
        rec["agent"]["before"] = ctx.turn("maya", "M5b $4,000 transfer", new=True)
    for s in AFTER_STEPS["M5b"]:  # m5b_enforce.py, then new conversations in both panes
        rec["steps"].append(ctx.step(*s))
    gap()
    if ctx.agent:
        rec["agent"].update(m5b_agent_path(ctx))
    gap()
    if ctx.direct:
        t0 = time.time()
        m = gk.Mcp(ctx.tokens["maya"])
        acc = mr(m, "get_member_accounts", {"member_id": "M-1001"})
        req = {"member_id": "M-1001", "source_ref": acc["refs"].get("savings_ref"), "destination_ref": acc["refs"].get("checking_ref"), "amount": 4000}
        d = rec["direct"]
        d["denied"] = mr(m, "initiate_transfer", req)
        d["non_supervisor"] = approve(ctx, m.ps, req, approver="leo")
        d["approval"] = approve(ctx, m.ps, req)
        d["approval_rows"] = approval_rows(ctx, t0)
        d["over"] = mr(m, "initiate_transfer", {**req, "amount": 4500})
        d["ok"] = mr(m, "initiate_transfer", req)
    return rec


def storm(ctx, name, seconds, *args, background=False, leo_delay=0.0):
    path = os.path.join(ctx.out_dir, f"{ctx.tag}-storm-{name}.json")
    cmd = [sys.executable, os.path.join(SCRIPTS, "steps/m6_storm.py"), str(seconds), *args, "--json", path]
    if leo_delay:
        cmd += ["--leo-delay", f"{leo_delay:g}"]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.Popen(cmd, cwd=DEMO, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)  # fixed argv list, no shell  # nosec B603  # nosemgrep
    if background:
        return p, path
    return finish_storm(p, path)


def finish_storm(p, path):
    out, err = p.communicate(timeout=300)
    data = json.load(open(path)) if os.path.exists(path) else {}
    data.update({"rc": p.returncode, "tail": [gk.mask(ln)[:200] for ln in (out or "").splitlines()[-3:]], "stderr_tail": gk.mask((err or "")[-300:])})
    return data


def run_m6(ctx):
    rec = new_rec("M6")
    rec["direct"]["before"] = storm(ctx, "before", 12, "--expect", "free")
    cnt = ctx.step("steps/m6_count.py")
    rec["skipped"] = {"m6_count": "skipped: " + (" ".join(cnt["tail"]) + cnt["stderr_tail"])[-220:] if cnt["rc"] != 0 else "ran"}
    rec["direct"]["limit"] = s = ctx.step(*AFTER_STEPS["M6"][0])  # m6_limit.py, M6's only after-step
    rec["steps"] += [cnt, s]
    # --leo-delay S: Leo starts S seconds into the after storm, his storm worker and his pane alike, and
    # so does Maya's pane turn; a cold bucket over-admits Maya for the first seconds.
    delay = float(ctx.leo_delay or 0)
    p, path = storm(ctx, "after", max(30, int(delay) + 25), background=True, leo_delay=delay)
    rec["agent_turns_at_s"] = max(6.0, delay)
    if ctx.agent:
        time.sleep(rec["agent_turns_at_s"])
        res = {}
        ths = [threading.Thread(target=lambda k=k, lab=lab: res.__setitem__(k, ctx.turn(k, lab, new=True)))
               for k, lab in (("maya", "M6 Accounts and profile"), ("leo", "M6 Loan file in the storm"))]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        rec["agent"] = {"maya": res.get("maya"), "leo": res.get("leo")}
    rec["direct"]["after"] = finish_storm(p, path)
    return rec


def cooldown(t_raise, token, cap_s=600, every=30, settle_n=6):
    """After the limit is raised from a warm 2 per second: every 30 s a burst of 4 direct calls as Maya within about a
    second (the shape of an agent turn). cooldown_s is the time from the raise to the first of two
    consecutive probes with no rate-limit throttle (limitKey). Probing then goes on until settle_n consecutive clean
    probes (settled_s) or the cap, because stray rate-limit throttles recur after a clean pair (rehearsal runs met them
    in M3 of the next run, about 5 minutes after the previous raise); the next run starts
    only after that. last_throttle_s is the latest probe with a rate-limit throttle. Engine throttles are counted apart."""
    gk.gk_mcp()
    res, streak = {"raise_utc": utc(t_raise), "probes": [], "cooldown_s": None, "last_throttle_s": None, "settled_s": None}, 0
    while True:
        tp = time.time()
        ms = [gk.Mcp(token) for _ in range(4)]
        bar, out = threading.Barrier(4), [None] * 4

        def fire(i):
            bar.wait()
            out[i] = gk.call(ms[i], "get_member_accounts", {"member_id": "M-1001"})["decision"]
        ths = [threading.Thread(target=fire, args=(i,)) for i in range(4)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        k = collections.Counter(kind(d) for d in out)
        res["probes"].append({"t": round(tp - t_raise, 1), **k})
        if k.get("rate_limit"):
            streak, res["last_throttle_s"] = 0, res["probes"][-1]["t"]
        else:
            streak += 1
        if streak == 2 and res["cooldown_s"] is None:
            res.update({"cooldown_s": res["probes"][-2]["t"], "confirmed_s": res["probes"][-1]["t"]})
        if streak >= settle_n:
            res["settled_s"] = res["probes"][-settle_n]["t"]
            break
        if time.time() - t_raise > cap_s:
            break
        time.sleep(max(0.0, tp + every - time.time()))
    res["cap_reached"] = res["cooldown_s"] is None  # the brief's cool-down failure: no clean pair within the cap
    res["settled"] = res["settled_s"] is not None
    res["engine_throttles"] = sum(p.get("policy_eval", 0) for p in res["probes"])
    return res


def kind(d):
    if d.get("decision") == "THROTTLED":
        return "policy_eval" if d.get("limit") == "policy-evaluation" else "rate_limit" if str(d.get("limit_key") or "").startswith(RATE_KEY) else "other_429"
    return {"ALLOWED": "ok", "SESSION_STALE": "stale"}.get(d.get("decision"), "other")


RUNNERS = {"R1.2": run_r12, "M1": run_m1, "M2": run_m2, "M3": run_m3, "M4": run_m4, "M5": run_m5, "M5c": run_m5c, "M5b": run_m5b, "M6": run_m6}
CHECKS = {"R1.2": (check_r12_agent, check_r12_direct), "M1": (None, None), "M2": (check_m2_agent, check_m2_direct), "M3": (check_m3_agent, check_m3_direct),
          "M4": (check_m4_agent, check_m4_direct), "M5": (check_m5_agent, check_m5_direct), "M5c": (check_m5c_agent, check_m5c_direct),
          "M5b": (check_m5b_agent, check_m5b_direct), "M6": (check_m6_agent, check_m6_direct)}


def evaluate(ctx, rec):
    """Fill rec["failures"] and rec["green"] from the recorded data (M1 needs the portal host)."""
    mid = rec["id"]
    if mid == "M1":
        ca, cd = (lambda a: check_m1_agent(a, ctx.portal_host)), (lambda d: check_m1_direct(d, ctx.portal_host))
    elif mid == "M2-CT":
        ca, cd = None, check_m2_cloudtrail
    else:
        ca, cd = CHECKS[mid]
    for path, fn, on in (("agent", ca, ctx.agent), ("direct", cd, ctx.direct)):
        if on and fn and rec.get(path):
            rec["failures"][path] = rec["failures"].get(path, []) + fn(rec[path])
    rec["green"] = {p: (not rec["failures"][p]) if (on and rec.get(p) and fn) else None
                    for p, fn, on in (("agent", ca, ctx.agent), ("direct", cd, ctx.direct))}
    return rec
