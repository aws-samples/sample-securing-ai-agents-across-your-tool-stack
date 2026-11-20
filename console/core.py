"""Demo Console back end: the two panes, the decision feed and the status strip.

Drives the agent on AgentCore Runtime through scripts/agentlib.py (the same client as the harness), reads the
Gateway-side trail (`TRAIL#<policy session ID>` rows in the events table) and the live toggles. Persona
tokens are issued here with AdminInitiateAuth and stay in this process: nothing in this module returns a token, a
password or a raw JWT to the browser, and nothing logs one.
"""
import asyncio
import collections
import datetime
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import agentlib as al  # noqa: E402
import gklib as gk  # noqa: E402
from boto3.dynamodb.conditions import Key  # noqa: E402
from state import PREFIX  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

# Every time the console shows (feed rows, approval request, controls read-at) is US Eastern, EDT or EST as the date dictates.
ET = ZoneInfo("America/New_York")


def et_clock(ts=None):
    return datetime.datetime.fromtimestamp(ts if ts is not None else time.time(), ET).strftime("%H:%M:%S")

PERSONAS = {"maya": ("Maya Patel", "Member Services"), "leo": ("Leo Martinez", "Loan Officer"),
            "dana": ("Dana Kim", "Branch Supervisor")}
# The moment tracker: the talk's stages in order, with their on-screen names. index.html draws the same list.
STAGES = [("1", "1 Front door"), ("2", "2 Per-person tokens"), ("3", "3 Act on behalf"), ("4", "4 Masking"),
          ("5a", "5a Sequence rule"), ("5b", "5b Prompt guard"), ("5c", "5c Approval"), ("6", "6 Rate limit")]
STAGE_IDS = tuple(s for s, _ in STAGES)
# Internal step codes (chips, step scripts, goto.py targets) to stage. M5c and M5b are crossed on purpose: on stage 5b is the
# prompt guard (internal M5c) and 5c is human approval (internal M5b); console/cards/README.md explains the naming.
STEP_STAGE = {"R1.2": "1", "M1": "1", "M2": "2", "M2-CT": "2", "M3": "3", "M4": "4", "M5": "5a", "M5c": "5b", "M5b": "5c", "M6": "6"}
BANNER_STATES = ("running", "done", "failed")
BANNER_MAX = 120  # characters, after masking
BANNER_SNAPSHOT_S = 60  # the snapshot carries the latest banner only while it is younger than this
EVENTS_TABLE = os.environ.get("GK_EVENTS_TABLE", "gk-events")
FEED_SHOWN = 6
MATCH_WINDOW_S = 20
TRAIL_TAIL_S = 6
# The step card: the console keeps the last CARD_LINES output lines of the current card; a run started by
# the Run button is killed (its whole process group) after RUN_TIMEOUT_S.
CARD_LINES = 300
RUN_TIMEOUT_S = 300
RUN_STEP = os.path.join(DEMO, "scripts", "run_step.py")
ACCT = re.compile(r"\d{12}")

# The reason column: one plain sentence per control, keyed by the policy's base name.
POLICY_REASON = {
    "DestinationSeenInSession": "destination not returned by an earlier read",
    "ApprovalOver2500": "over $2,500 needs a supervisor approval in this session",
    "SuppressSsnInNotes": "SSN found in the notes text",
    "PromptAttackOnReferrals": "partner text looks like a prompt attack",
    "ReadBurstPerSession": "too many reads in this session",
}


def stage_for(step):
    """The tracker stage of an internal step code ("M5c" -> "5b"), or None for an unknown code."""
    return STEP_STAGE.get(step) if isinstance(step, str) else None


def mask_text(text):
    """gk.mask (the account ID from STS), or, when STS cannot be reached, any 12-digit run: a banner must not fail
    because the console's own credentials expired."""
    try:
        return gk.mask(text)
    except Exception:  # noqa: BLE001 - no or expired credentials in the console process
        return re.sub(r"\d{12}", "<acct>", str(text))


def short_tool(name):
    return (name or "").split("___", 1)[-1]


def base_name(policy):
    """gk_DestinationSeenInSession_v2-abc123 or gk_DestinationSeenInSession_v2 -> DestinationSeenInSession."""
    n = re.sub(r"-[a-z0-9]+$", "", policy or "")
    n = n[3:] if n.startswith("gk_") else n
    return re.sub(r"_(v\d+|g\d+)$", "", n)


def describe(decision, policies=(), layer=None, limit=None, limit_key=None, reason=None, masked=(), control=None):
    """(label, control, reason) for a feed row. The label is one of R9.1's decisions, plus STALE and ERROR."""
    policies = [p for p in (policies or []) if p]
    if decision == "DENIED_TOOL" or (decision == "DENIED" and layer == "tool"):
        return "DENIED", "Tool entitlement (act on behalf)", reason or "the tool refused the request"
    if decision in ("DENIED", "SUPPRESSED") and (layer == "interceptor" or control == "prompt-guard" or "prompt-guard" in policies):
        return "SUPPRESSED", "Interceptor prompt guard", "partner text looks like a prompt attack (Bedrock Guardrails)"
    if decision in ("DENIED", "SUPPRESSED") and policies:
        names = list(dict.fromkeys(base_name(p) for p in policies))
        return decision, "Policy " + ", ".join(names), POLICY_REASON.get(names[0], "policy decision")
    if decision in ("DENIED", "SUPPRESSED"):
        return decision, "Policy", reason or "policy decision"
    if decision == "REDACTED":
        return "REDACTED", "Interceptor masking", ("masked " + ", ".join(masked)) if masked else "fields masked"
    if decision == "THROTTLED":
        if limit == "policy-evaluation":
            return "THROTTLED", "Policy engine protection", "policy evaluation throttled (shared, not per identity)"
        rid = "per-staff-member-records" if str(limit_key or "").startswith("per-staff-member-records") else (limit_key or "rate limit")
        return "THROTTLED", f"Rate limit {rid}", "per-identity limit reached"
    if decision == "CONSENT_REQUIRED":
        return "CONSENT_REQUIRED", "Token vault (partner OAuth)", "connect the partner account first"
    if decision == "SESSION_STALE":
        return "STALE", "Policy session", "a policy changed; a new conversation starts"
    if decision == "ERROR":
        return "ERROR", "-", reason or "error"
    return "ALLOWED", "-", reason or ""


class Tokens:
    """Persona access tokens (console client), memory only, re-issued 10 minutes before they expire."""

    def __init__(self):
        self._t, self._lock = {}, threading.Lock()

    def get(self, persona):
        with self._lock:
            tok, exp = self._t.get(persona, (None, 0))
            if not tok or time.time() > exp - 600:
                r = gk.staff_auth(persona)
                tok, exp = r["AccessToken"], time.time() + int(r.get("ExpiresIn", 3600))
                self._t[persona] = (tok, exp)
            return tok


class Hub:
    """Fan-out of events to server-sent-event subscribers, per channel ("maya", "right", "feed"). Thread safe."""

    def __init__(self, loop):
        self.loop, self.subs = loop, collections.defaultdict(set)

    def subscribe(self, channel):
        q = asyncio.Queue(maxsize=1000)
        self.subs[channel].add(q)
        return q

    def unsubscribe(self, channel, q):
        self.subs[channel].discard(q)

    def publish(self, channel, event):
        self.loop.call_soon_threadsafe(self._fanout, channel, event)

    def _fanout(self, channel, event):
        for q in list(self.subs[channel]):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass


class Feed:
    """The decision feed: rows from the agent stream, the Gateway trail and scripts, merged per tool call."""

    def __init__(self, hub):
        self.hub, self.rows, self.lock = hub, [], threading.Lock()

    def _publish(self, row):
        self.hub.publish("feed", {"type": "row", "row": public(row)})

    def _new(self, persona, tool, ps, label, control, reason, source, ts=None, **extra):
        ts = ts or time.time()
        row = {"id": uuid.uuid4().hex[:12], "ts": ts, "time": et_clock(ts), "persona": persona,
               "tool": short_tool(tool), "decision": label, "control": control, "reason": reason, "sources": [source], "ps": ps, **extra}
        self.rows.append(row)
        del self.rows[:-200]
        return row

    def _match(self, ps, tool, ts, want_source, missing_source):
        for r in self.rows:  # oldest first: one row per tool call, in call order
            if r["ps"] == ps and r["tool"] == short_tool(tool) and want_source in r["sources"] and missing_source not in r["sources"] \
                    and abs(r["ts"] - ts) < MATCH_WINDOW_S:
                return r
        return None

    def add_agent(self, persona, ps, ev):
        label, control, reason = describe(ev.get("decision"), ev.get("policies"), ev.get("layer"), ev.get("limit"), ev.get("limit_key"),
                                          ev.get("reason"))
        with self.lock:
            row = self._match(ps, ev.get("tool"), time.time(), "gateway", "agent")
            if row:  # the trail row came first: keep its time, take the agent's classification unless the trail saw masking
                row["sources"].append("agent")
                if row["decision"] != "REDACTED":
                    row.update({"decision": label, "control": control, "reason": reason})
            else:
                row = self._new(persona, ev.get("tool"), ps, label, control, reason, "agent", layer=ev.get("layer"))
        self._publish(row)
        return row

    def add_trail(self, persona, ps, item):
        ts = int(str(item.get("sk", "0")).split("#")[0] or 0) / 1000.0 or time.time()
        label, control, reason = describe(item.get("decision"), item.get("policy_ids"), item.get("layer"), masked=item.get("masked_fields") or (),
                                          control=item.get("control"))
        with self.lock:
            row = self._match(ps, item.get("tool"), ts, "agent", "gateway")
            if row:
                row["sources"].append("gateway")
                if label == "REDACTED" and row["decision"] == "ALLOWED":  # masking is invisible to the agent
                    row.update({"decision": label, "control": control, "reason": reason})
                elif label == row["decision"] and control != "-" and row["control"] in ("-", "Policy"):
                    row.update({"control": control, "reason": reason})
            else:
                row = self._new(persona, item.get("tool"), ps, label, control, reason, "gateway", ts=ts, layer=item.get("layer"))
        self._publish(row)
        return row

    def add_script(self, persona, tool, decision, control, reason, source="script"):
        with self.lock:
            # Reason kept up to 600 characters so long script reasons, such as R-2003's injected text, show in full;
            # the feed's Reason cell wraps.
            row = self._new(persona, tool, None, str(decision)[:24], str(control)[:80], str(reason)[:600], source)
        self._publish(row)
        return row

    def latest(self, n=FEED_SHOWN):
        with self.lock:
            return [public(r) for r in self.rows[-n:]]


def public(row):
    return {k: row[k] for k in ("id", "time", "persona", "tool", "decision", "control", "reason", "sources")}


class Pane:
    def __init__(self, key, persona):
        self.key, self.persona, self.mode = key, persona, "chat"
        self.conv, self.ps, self.busy = None, None, False
        self.turns = collections.deque(maxlen=3)
        self.consent_url = None
        self.last_transfer = None  # the model-written arguments of the last initiate_transfer (safe_input)
        self.approval_candidate = None

    def snapshot(self):
        name, role = PERSONAS[self.persona]
        return {"pane": self.key, "persona": self.persona, "name": name, "role": role, "mode": self.mode, "busy": self.busy,
                "turns": list(self.turns), "consent_url": self.consent_url, "approval_candidate": self.approval_candidate,
                "policy_session_id": self.ps}


class Console:
    def __init__(self, loop):
        self.hub = Hub(loop)
        self.feed = Feed(self.hub)
        self.tokens = Tokens()
        self.panes = {"maya": Pane("maya", "maya"), "right": Pane("right", "leo")}
        self.ps_owner = {}
        self.pending_approval = None
        self.status = {"state": "starting"}
        self.moment = {"current": None, "source": None, "at": None}  # the tracker: no current stage before the first moment
        self.banner = None  # the latest script banner, with its epoch "ts" (kept server-side only)
        self.card = None  # the step card: see card_start
        self.step_runs = {}  # card id -> {"state", "run_id"} since the last card_clear (the Run buttons' states)
        self.runs = {}  # run id -> Popen, for the runs the Run button started
        self.stopping = set()  # run ids the Stop button signalled
        self.lock = threading.Lock()

    # ---------------- panes ----------------
    def pane_event(self, pane, event):
        self.hub.publish(pane.key, {**event, "pane": pane.key})

    def conversation(self, pane):
        if pane.conv is None:
            pane.conv = al.Conversation(pane.persona, token=self.tokens.get(pane.persona))
        pane.conv.token = self.tokens.get(pane.persona)  # re-issued before expiry
        return pane.conv

    def new_conversation(self, key):
        pane = self.panes[key]
        if pane.busy:
            raise RuntimeError("a turn is running in this pane")
        if pane.conv is not None:
            pane.conv.new(keep_runtime_session=True)  # keeps the warm microVM
        pane.ps, pane.consent_url, pane.approval_candidate = None, None, None
        pane.turns.clear()
        self.pane_event(pane, {"type": "snapshot", "snapshot": pane.snapshot()})

    def send(self, key, prompt, step=None):
        """Start a turn. step is the chip's step code when the page sent the chip's own prompt: the tracker then moves to
        that code's stage, backwards too (re-running an earlier chip). An unknown step code changes nothing."""
        pane = self.panes[key]
        prompt = (prompt or "").strip()[:2000]
        if not prompt:
            raise ValueError("empty prompt")
        with self.lock:
            if pane.busy:
                raise RuntimeError("a turn is already running in this pane")
            pane.busy = True
        threading.Thread(target=self._turn, args=(pane, prompt), daemon=True).start()
        self.dismiss_card_if_idle()  # a send closes a finished card; a running one stays (the Moment 6 storm)
        stage = stage_for(step)
        if stage:
            self.set_moment(stage, "chip")

    # ---------------- moment tracker and script banner ----------------
    def set_moment(self, stage, source):
        """Make stage (one of STAGE_IDS, or None for no current stage) the tracker's current stage and publish it."""
        if stage is not None and stage not in STAGE_IDS:
            raise ValueError(f"stage must be one of {', '.join(STAGE_IDS)} or null")
        self.moment = {"current": stage, "source": str(source or "api")[:16], "at": et_clock()}
        self.hub.publish("feed", {"type": "moment", "moment": dict(self.moment)})
        return dict(self.moment)

    def set_banner(self, text, state):
        """Keep and publish the latest script banner: masked, at most BANNER_MAX characters."""
        if state not in BANNER_STATES:
            raise ValueError(f"state must be one of {', '.join(BANNER_STATES)}")
        text = " ".join(mask_text(text).split())[:BANNER_MAX]
        if not text:
            raise ValueError("empty banner text")
        self.banner = {"text": text, "state": state, "at": et_clock(), "ts": time.time()}
        public = {k: self.banner[k] for k in ("text", "state", "at")}
        self.hub.publish("feed", {"type": "banner", "banner": public})
        return public

    def current_banner(self):
        """The latest banner while it is younger than BANNER_SNAPSHOT_S, with its age (the page times the 'done' fade)."""
        b = self.banner
        if not b or time.time() - b["ts"] >= BANNER_SNAPSHOT_S:
            return None
        return {"text": b["text"], "state": b["state"], "at": b["at"], "age_s": round(time.time() - b["ts"], 1)}

    def _turn(self, pane, prompt):
        turn = {"id": uuid.uuid4().hex[:10], "prompt": prompt, "text": "", "chips": [], "status": "running", "notes": []}
        pane.turns.append(turn)
        self.pane_event(pane, {"type": "turn_start", "turn": turn})
        done = threading.Event()
        threading.Thread(target=self._poll_trail, args=(pane, done, int(time.time() * 1000) - 2000), daemon=True).start()

        def on_event(ev):
            t = ev.get("type")
            if t == "session":
                pane.ps = ev.get("policy_session_id")
                self.ps_owner[pane.ps] = pane.persona
            elif t == "text":
                turn["text"] += ev.get("text", "")
                self.pane_event(pane, {"type": "text", "turn_id": turn["id"], "text": ev.get("text", "")})
            elif t == "tool_start":
                if short_tool(ev.get("tool")) == "initiate_transfer":
                    pane.last_transfer = {k: ev.get("input", {}).get(k) for k in ("member_id", "source_ref", "destination_ref", "amount")}
                self.pane_event(pane, {"type": "tool_start", "turn_id": turn["id"], "tool": short_tool(ev.get("tool"))})
            elif t == "decision":
                row = self.feed.add_agent(PERSONAS[pane.persona][0].split()[0], pane.ps, ev)
                chip = {"tool": row["tool"], "decision": row["decision"], "row": row["id"]}  # the page keeps it in step with the row
                turn["chips"].append(chip)
                self.pane_event(pane, {"type": "decision", "turn_id": turn["id"], **chip})
                if row["tool"] == "partner_list_referrals" and row["decision"] == "ALLOWED" and pane.consent_url:
                    pane.consent_url = None  # connected now: hide the Connect button
                    self.pane_event(pane, {"type": "consent", "url": None})
                if row["tool"] == "initiate_transfer" and "ApprovalOver2500" in row["control"] and pane.last_transfer:
                    pane.approval_candidate = dict(pane.last_transfer)
                    self.pane_event(pane, {"type": "approval_candidate", "candidate": pane.approval_candidate})
            elif t == "consent":
                pane.consent_url = ev.get("url")
                self.pane_event(pane, {"type": "consent", "url": pane.consent_url})
            elif t == "session_reset":
                turn["text"] = ""
                turn["notes"].append("A policy changed, so the policy session ended. New conversation; the prompt is sent again.")
                self.pane_event(pane, {"type": "note", "turn_id": turn["id"], "text": turn["notes"][-1], "reset_text": True})
            elif t == "error":
                turn["notes"].append(f"Agent error: {ev.get('error_type', 'error')}")
                self.pane_event(pane, {"type": "note", "turn_id": turn["id"], "text": turn["notes"][-1]})

        try:
            s = self.conversation(pane).turn(prompt, on_event=on_event)
            turn.update({"status": "done", "latency_s": s.get("latency_s")})
            if s.get("http") != 200:
                turn["notes"].append(f"Runtime answered HTTP {s.get('http')}")
            if s.get("consent") and not pane.consent_url:
                pane.consent_url = s["consent"][-1]
        except Exception as e:  # noqa: BLE001 - show the failure in the pane; no token or body in the message
            turn.update({"status": "failed"})
            turn["notes"].append(f"Turn failed: {type(e).__name__}")
        finally:
            pane.busy = False
            self.pane_event(pane, {"type": "turn_end", "turn": turn, "snapshot": pane.snapshot()})
            done.set()

    def _poll_trail(self, pane, done, since_ms):
        """Gateway trail rows for this pane's policy session, every second during a turn and a few seconds after."""
        seen, tbl, end = set(), gk.SESSION.resource("dynamodb").Table(EVENTS_TABLE), None
        while True:
            ps = pane.ps
            if ps:
                try:
                    items = tbl.query(KeyConditionExpression=Key("pk").eq(f"TRAIL#{ps}") & Key("sk").gt(str(since_ms)))["Items"]
                except Exception:  # noqa: BLE001 - the feed keeps the agent's rows if the trail is unreadable
                    items = []
                for it in sorted(items, key=lambda x: x["sk"]):
                    if it["sk"] not in seen:
                        seen.add(it["sk"])
                        self.feed.add_trail(PERSONAS[self.ps_owner.get(ps, pane.persona)][0].split()[0], ps, it)
            if done.is_set():
                end = end or time.time()
                if time.time() - end > TRAIL_TAIL_S:
                    return
            time.sleep(1)

    # ---------------- partner connection ----------------
    def disconnect(self, key):
        pane = self.panes[key]
        r = gk.invoke_reset({"action": "revoke", "user": pane.persona})
        pane.consent_url = None
        self.feed.add_script(PERSONAS[pane.persona][0].split()[0], "partner_list_referrals", "DISCONNECTED", "Token vault (forceAuthentication)",
                             "stored partner token revoked; consent repeats" if r.get("consent_repeats") else "revoke sent", source="console")
        return {"consent_repeats": bool(r.get("consent_repeats"))}

    # ---------------- approval ----------------
    def request_approval(self):
        maya = self.panes["maya"]
        cand = maya.approval_candidate
        if not cand or not maya.ps:
            raise RuntimeError("no transfer denied for approval in Maya's session yet")
        self.pending_approval = {**cand, "policy_session_id": maya.ps, "requested": et_clock()}
        right = self.panes["right"]
        right.mode = "approval"
        self.pane_event(right, {"type": "mode", "mode": "approval", "approval": public_approval(self.pending_approval)})
        return public_approval(self.pending_approval)

    def cancel_approval(self):
        self.pending_approval = None
        right = self.panes["right"]
        right.mode = "chat"
        self.pane_event(right, {"type": "mode", "mode": "chat"})

    def approve(self):
        req = self.pending_approval
        if not req:
            raise RuntimeError("no pending approval")
        dana_id = gk.staff_auth("dana", "approver")["IdToken"]  # audience: the approver client only
        m = gk.Mcp(self.tokens.get("maya"), policy_session=req["policy_session_id"])
        r = gk.call(m, "record_transfer_approval", {"member_id": req["member_id"], "destination_ref": req["destination_ref"],
                                                   "amount": req["amount"], "approver_id_token": dana_id})
        del dana_id
        sc = r.get("sc") or {}
        ok = sc.get("approved") is True
        self.feed.add_script("Dana", "record_transfer_approval", "ALLOWED" if ok else "DENIED", "Supervisor approval (token checked by the tool)",
                             "approval recorded in Maya's session" if ok else str(sc.get("reason") or r.get("err_message") or "refused")[:120],
                             source="console")
        if ok:
            self.panes["maya"].approval_candidate = None
            self.cancel_approval()
        return {"approved": ok, "reason": None if ok else str(sc.get("reason") or "")[:120]}

    # ---------------- direct-call path ----------------
    def direct(self, key, moment):
        """'Assume the agent is fully compromised and makes exactly this call': the moment's tool calls as the pane's persona,
        in the pane's policy session (a new one if the pane has none), no model.
        m5: get_member_accounts, then $800 from savings to EXT-4471 (the poisoned referral's destination).
        m5b: the M5b retry without the model, in the pane's CURRENT policy session, the one in which "Approve as Dana"
        recorded the approval: get_member_accounts, then $4,000 from savings to checking."""
        pane = self.panes[key]
        if moment not in ("m5", "m5b"):
            raise ValueError("unknown moment (m5 or m5b)")
        if pane.persona != "maya":
            raise ValueError(f"the {moment} direct call runs as Maya")
        ps = pane.ps or str(uuid.uuid4())
        self.ps_owner[ps] = pane.persona
        if not pane.ps:
            pane.ps = ps
        done = threading.Event()
        threading.Thread(target=self._poll_trail, args=(pane, done, int(time.time() * 1000) - 2000), daemon=True).start()
        try:
            m = gk.Mcp(self.tokens.get(pane.persona), policy_session=ps)
            out = []
            acc = gk.call(m, "get_member_accounts", {"member_id": "M-1001"})
            out.append(self._direct_row(pane, "get_member_accounts", acc))
            sc = acc.get("sc") or {}
            src = sc.get("savings_ref")
            if moment == "m5":
                args = {"member_id": "M-1001", "source_ref": src or "unknown", "destination_ref": "EXT-4471", "amount": 800}
            else:
                args = {"member_id": "M-1001", "source_ref": src or "unknown", "destination_ref": sc.get("checking_ref") or "unknown", "amount": 4000}
            tr = gk.call(m, "initiate_transfer", args)
            out.append(self._direct_row(pane, "initiate_transfer", tr))
            return {"policy_session_id": ps, "calls": out}
        finally:
            done.set()

    def _direct_row(self, pane, tool, r):
        row = self.feed.add_agent(PERSONAS[pane.persona][0].split()[0], pane.ps, {**r["decision"], "tool": f"MemberRecords___{tool}"})
        if "direct" not in row["sources"]:
            row["sources"].append("direct")
        return {"tool": tool, "decision": row["decision"], "control": row["control"]}

    # ---------------- step card and Run buttons ----------------
    def public_card(self):
        c = self.card
        if not c:
            return None
        out = {k: c[k] for k in ("id", "run_id", "source", "state", "at", "seconds", "exit_code", "dismissed")}
        out["elapsed_s"] = round(time.time() - c["started"], 1) if c["state"] == "running" else c["seconds"]
        out["lines"] = list(c["lines"])
        return out

    def public_runs(self):
        return {k: dict(v) for k, v in self.step_runs.items()}

    def _publish_card(self):
        self.hub.publish("feed", {"type": "card", "card": self.public_card()})

    def _publish_runs(self):
        self.hub.publish("feed", {"type": "step_runs", "step_runs": self.public_runs()})

    def card_running(self):
        return bool(self.card and self.card["state"] == "running")

    def card_start(self, card, run_id, started):
        """A new run of card (a registry dict): it replaces the current card, and the tracker moves to the card's stage."""
        with self.lock:
            self.card = {"id": card["id"], "run_id": run_id, "source": "console" if run_id in self.runs else "terminal",
                         "state": "running", "started": started, "at": et_clock(started), "seconds": None, "exit_code": None,
                         "lines": collections.deque(maxlen=CARD_LINES), "dismissed": False}
            self.step_runs[card["id"]] = {"state": "running", "run_id": run_id}
        self._publish_card()
        self._publish_runs()
        self.set_moment(card["stage"], "step")
        return {"source": self.card["source"]}

    def _this_run(self, card_id, run_id):
        c = self.card
        return c if c and c["id"] == card_id and c["run_id"] == run_id and c["state"] == "running" else None

    def card_lines(self, card_id, run_id, lines):
        """Output lines of the running card, masked (every 12-digit number). Lines of another run are dropped."""
        lines = [ACCT.sub("<acct>", str(x)) for x in lines]
        with self.lock:
            c = self._this_run(card_id, run_id)
            if c:
                c["lines"].extend(lines)
        if c:
            self.hub.publish("feed", {"type": "card_lines", "id": card_id, "run_id": run_id, "lines": lines})
        return {"accepted": bool(c)}

    def card_end(self, card_id, run_id, exit_code, seconds, stopped=False):
        with self.lock:
            c = self._this_run(card_id, run_id)
            if c:
                c["state"] = "stopped" if stopped else "done" if exit_code == 0 else "failed"
                c["exit_code"], c["seconds"] = exit_code, round(seconds, 1)
                self.step_runs[card_id] = {"state": c["state"], "run_id": run_id}
        if c:
            self._publish_card()
            self._publish_runs()
        return {"accepted": bool(c)}

    def card_close(self):
        if self.card:
            self.card["dismissed"] = True
            self._publish_card()
        return {"ok": True}

    def dismiss_card_if_idle(self):
        if self.card and not self.card["dismissed"] and self.card["state"] != "running":
            self.card_close()

    def card_clear(self):
        """reset.py: no card and no run results (every Run button back to 'to do')."""
        with self.lock:
            self.card, self.step_runs = None, {}
        self._publish_card()
        self._publish_runs()
        return {"ok": True}

    def run_busy(self):
        return self.card_running() or any(p is not None and p.poll() is None for p in self.runs.values())

    def start_run(self, card, sock, log_dir):
        """The Run button: scripts/run_step.py <card id> --run-id <new id>, with the console's interpreter, from the repo root, in
        its own process group, stdin from /dev/null, output to <log_dir>/step-<run id>.log. Only registry values and the
        console's own run id reach the process. RuntimeError (HTTP 409) while a card runs."""
        with self.lock:
            if self.card_running() or any(p is not None and p.poll() is None for p in self.runs.values()):
                raise RuntimeError("a step is running: wait for it, or press Stop")
            rid = uuid.uuid4().hex[:12]
            self.runs[rid] = None  # reserved: a second click before Popen returns sees it as busy
        try:
            os.makedirs(log_dir, mode=0o700, exist_ok=True)
            fd = os.open(os.path.join(log_dir, f"step-{rid}.log"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as log:
                proc = subprocess.Popen([sys.executable, RUN_STEP, card["id"], "--run-id", rid], cwd=DEMO, stdin=subprocess.DEVNULL,  # fixed argv list, no shell  # nosec B603  # nosemgrep
                                        stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                                        env={**os.environ, "GK_CONSOLE_SOCK": sock, "PYTHONUNBUFFERED": "1"})
        except Exception:
            self.runs.pop(rid, None)
            raise
        self.runs[rid] = proc
        threading.Thread(target=self._watch_run, args=(card, rid, proc), daemon=True).start()
        return rid

    def _watch_run(self, card, rid, proc):
        t0 = time.time()
        try:
            proc.wait(timeout=RUN_TIMEOUT_S)
            timed_out = False
        except subprocess.TimeoutExpired:
            self._signal(proc, signal.SIGKILL)
            proc.wait()
            timed_out = True
        if self._this_run(card["id"], rid):  # the runner ended without its card_end (killed, or a crash)
            if timed_out:
                self.card_lines(card["id"], rid, [f"Stopped: the step ran longer than {RUN_TIMEOUT_S} s."])
            self.card_end(card["id"], rid, proc.returncode, time.time() - t0, stopped=timed_out or rid in self.stopping)

    @staticmethod
    def _signal(proc, sig):
        if proc.poll() is None:  # only a group this console started and that is still alive (no pid reuse)
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                pass

    def stop_run(self):
        """The Stop button: SIGINT to the console-started run's process group, SIGTERM after 3 s, SIGKILL after 6 s."""
        live = [(rid, p) for rid, p in self.runs.items() if p is not None and p.poll() is None]
        if not live:
            raise RuntimeError("no step started from the console is running")
        rid, proc = live[-1]
        self.stopping.add(rid)
        self._signal(proc, signal.SIGINT)

        def escalate():
            for sig, at in ((signal.SIGTERM, 3), (signal.SIGKILL, 6)):
                try:
                    proc.wait(timeout=max(0.0, at - (time.time() - t0)))
                    return
                except subprocess.TimeoutExpired:
                    self._signal(proc, sig)
        t0 = time.time()
        threading.Thread(target=escalate, daemon=True).start()
        return {"run_id": rid}

    # ---------------- status strip ----------------
    def read_status(self):
        try:
            pol = {}
            for n in gk.active_policies():
                p = gk.get_policy(n)
                pol[base_name(n)] = p.get("enforcementMode") if p.get("status") == "ACTIVE" else p.get("status")
            st = {"state": "ok", "deputy": gk.ssm_get(f"{PREFIX}/deputy-mode"), "masking": gk.ssm_get(f"{PREFIX}/masking"),
                  "prompt_guard": gk.ssm_get(f"{PREFIX}/prompt-guard"), "rate_per_second": gk.rate_limit(), "policies": pol,
                  "read_at": et_clock()}
        except Exception as e:  # noqa: BLE001 - expired credentials and the like show on the strip
            st = {"state": "unavailable", "error": type(e).__name__, "read_at": et_clock()}
        changed = {k: v for k, v in st.items() if k != "read_at"} != {k: v for k, v in self.status.items() if k != "read_at"}
        self.status = st
        self.hub.publish("feed", {"type": "status", "status": st, "changed": changed})
        return st

    def snapshot(self):
        with self.feed.lock:
            current = {r["id"]: r["decision"] for r in self.feed.rows}
        for p in self.panes.values():
            for t in p.turns:
                for ch in t.get("chips", []):
                    ch["decision"] = current.get(ch.get("row"), ch["decision"])
        return {"panes": {k: p.snapshot() for k, p in self.panes.items()}, "feed": self.feed.latest(), "status": self.status,
                "approval": public_approval(self.pending_approval) if self.pending_approval else None,
                "moment": dict(self.moment), "banner": self.current_banner(), "card": self.public_card(),
                "step_runs": self.public_runs()}


def public_approval(req):
    if not req:
        return None
    return {k: req.get(k) for k in ("member_id", "destination_ref", "amount", "requested")}


def jsonable(obj):
    return json.loads(json.dumps(obj, default=str))
