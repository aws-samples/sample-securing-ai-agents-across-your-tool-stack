"""RESPONSE interceptor (gk-response-interceptor).

- tools/list: hides `_credential` from every tool's inputSchema.
- tools/call on MemberRecords: masks `ssn` and `accounts[].full_number` in the result when SSM /gk/masking is `on`
  (never touches account_ref, checking_ref, savings_ref).
- -32042 elicitations: rewrites the authorization URL to the consent portal URL (SSM /gk/portal-url).
- Prompt guard (M5c): when SSM /gk/prompt-guard is `on`, the text of every `Partner___partner_list_referrals`
  result goes through Bedrock ApplyGuardrail (guardrail gk-prompt-guard, PROMPT_ATTACK filter, as model input). If the
  guardrail intervenes, the result is withheld the way a guardrail policy withholds one: HTTP 403 with JSON-RPC -32002,
  and a message naming this layer ("Output blocked by response interceptor: ...") so the console shows which layer
  acted. It fails closed: if the check cannot run, the result is withheld too.
  The demo runs this check here, not as a guardrail policy on the policy engine.
- Trail: one row per tools/call in the events table, TRAIL#<policy session ID>, with the decision, the
  acting layer, policy IDs from the error message and masked field names. Never values, arguments or headers.
Logs only the shape of what it saw: method, tool, status code, error code, result keys, decision.
"""
import json
import os
import re
import time
import uuid

import boto3

SSM = boto3.client("ssm")
BEDROCK = boto3.client("bedrock-runtime")
EVENTS = boto3.resource("dynamodb").Table(os.environ["EVENTS_TABLE"])
MASK_PARAM = os.environ["MASK_PARAM"]
PORTAL_PARAM = os.environ["PORTAL_PARAM"]
GUARD_PARAM = os.environ["GUARD_PARAM"]
GUARDRAIL_ID = os.environ["GUARDRAIL_ID"]
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")
PARTNER_TOOL = "Partner___partner_list_referrals"
PS_HEADER = "x-amzn-bedrock-agentcore-policy-session-id"
GUARD_MESSAGE = "Output blocked by response interceptor: prompt-attack check (Bedrock Guardrails gk-prompt-guard)"
_cache = {}


def param(name, default):
    hit = _cache.get(name)
    if hit and time.time() - hit[1] < 5:
        return hit[0]
    try:
        value = SSM.get_parameter(Name=name)["Parameter"]["Value"]
    except Exception:  # noqa: BLE001
        value = default
    _cache[name] = (value, time.time())
    return value


def last4(v, pat):
    s = str(v)
    return pat + s[-4:] if len(s) >= 4 else pat


def mask(obj):
    changed = []
    if isinstance(obj, dict):
        if "ssn" in obj:
            obj["ssn"] = last4(obj["ssn"], "***-**-")
            changed.append("ssn")
        for a in obj.get("accounts") or []:
            if isinstance(a, dict) and "full_number" in a:
                a["full_number"] = last4(a["full_number"], "****")
                if "accounts[].full_number" not in changed:
                    changed.append("accounts[].full_number")
    return changed


def result_text(res):
    return " ".join(c.get("text", "") for c in res.get("content") or [] if isinstance(c, dict) and c.get("type") == "text")


def referral_text(res):
    """The text the model would read from a partner result: every referral title and body (or the digest)."""
    sc = res.get("structuredContent")
    if not isinstance(sc, dict):
        try:
            sc = json.loads(result_text(res))
        except (TypeError, ValueError):
            sc = None
    if isinstance(sc, dict):
        parts = [f"{r.get('title', '')}\n{r.get('body', '')}" for r in sc.get("referrals") or [] if isinstance(r, dict)]
        return "\n\n".join(parts) or str(sc.get("digest") or "")
    return result_text(res)


def prompt_guard(res):
    """(withhold, detail). Fails closed: if the check cannot run, the result is withheld."""
    text = referral_text(res)
    if not text.strip():
        return False, {"guard": "empty"}
    try:
        r = BEDROCK.apply_guardrail(guardrailIdentifier=GUARDRAIL_ID, guardrailVersion=GUARDRAIL_VERSION, source="INPUT",
                                    content=[{"text": {"text": text[:24000]}}])
    except Exception as e:  # noqa: BLE001
        return True, {"guard": "error", "error_type": type(e).__name__}
    filters = [f for a in r.get("assessments") or [] for f in (a.get("contentPolicy") or {}).get("filters") or []]
    hits = [{"type": f.get("type"), "confidence": f.get("confidence"), "action": f.get("action")} for f in filters]
    return r.get("action") == "GUARDRAIL_INTERVENED", {"guard": "checked", "filters": hits}


def header(req, name):
    for k, v in ((req.get("headers") or {}).items()):
        if k.lower() == name:
            return v
    return None


def policy_ids(message):
    m = re.search(r"denied due to ([^\]]+)", message or "")
    return [t for t in re.split(r"[,\s]+", m.group(1)) if re.fullmatch(r"[A-Za-z][\w-]*-[\w]+", t)] if m else []


def trail(ps, rid, tool, decision, layer, status, code=None, pids=(), fields=(), control=None):
    now = time.time()
    item = {"pk": f"TRAIL#{ps or 'none'}", "sk": f"{int(now * 1000)}#{rid}#{uuid.uuid4().hex[:4]}", "tool": tool,
            "decision": decision, "layer": layer, "http_status": int(status), "ts": int(now), "ttl": int(now) + 86400}
    if code is not None:
        item["error_code"] = int(code)
    if pids:
        item["policy_ids"] = list(pids)
    if fields:
        item["masked_fields"] = list(fields)
    if control:
        item["control"] = control
    try:
        EVENTS.put_item(Item=item)
    except Exception as e:  # noqa: BLE001 - the trail never breaks a response
        print(json.dumps({"point": "RESPONSE", "trail_error": type(e).__name__}))


def handler(event, context):
    mcp = event.get("mcp") or {}
    req = mcp.get("gatewayRequest") or {}
    req_body = req.get("body") or {}
    resp = mcp.get("gatewayResponse") or {}
    body = resp.get("body")
    status = resp.get("statusCode") or 200
    method = req_body.get("method")
    tool = ((req_body.get("params") or {}).get("name")) or ""
    note = {"point": "RESPONSE", "method": method, "tool": tool, "status": status, "request_id": context.aws_request_id,
            "body_type": type(body).__name__, "streaming": bool(resp.get("isStreamingResponse"))}
    decision = None
    if isinstance(body, dict):
        err = body.get("error")
        res = body.get("result")
        note["error_code"] = (err or {}).get("code")
        note["result_keys"] = sorted(res.keys()) if isinstance(res, dict) else None
        if method == "tools/list" and isinstance(res, dict):
            for t in res.get("tools") or []:
                props = ((t.get("inputSchema") or {}).get("properties")) or {}
                props.pop("_credential", None)
        if err and err.get("code") == -32042:
            portal = param(PORTAL_PARAM, "")
            n = 0
            for e in ((err.get("data") or {}).get("elicitations") or []):
                if portal.startswith("https://") and isinstance(e, dict) and e.get("url"):
                    e["url"] = portal
                    n += 1
            note["elicitations_rewritten"] = n
        if method == "tools/call" and tool == PARTNER_TOOL and isinstance(res, dict) and not res.get("isError") \
                and param(GUARD_PARAM, "on") == "on":
            withhold, detail = prompt_guard(res)
            note["prompt_guard"] = detail
            if withhold:
                status = 403
                body = {"jsonrpc": "2.0", "id": req_body.get("id"), "error": {"code": -32002, "message": GUARD_MESSAGE}}
                decision = ("SUPPRESSED", "interceptor", -32002, (), (), "prompt-guard")
        if method == "tools/call" and tool.startswith("MemberRecords___") and isinstance(res, dict):
            note["is_error"] = res.get("isError")
            changed = []
            if param(MASK_PARAM, "on") == "on":
                sc = res.get("structuredContent")
                changed = mask(sc) if isinstance(sc, dict) else []
                if changed:
                    res["content"] = [{"type": "text", "text": json.dumps(sc)}]
                else:  # no structured copy: try the text copy
                    for c in res.get("content") or []:
                        if c.get("type") == "text":
                            try:
                                obj = json.loads(c["text"])
                            except (ValueError, TypeError):
                                continue
                            ch = mask(obj)
                            if ch:
                                c["text"] = json.dumps(obj)
                                changed += ch
            note["masked"] = changed
            if changed:
                decision = ("REDACTED", "interceptor", None, (), changed, None)
            elif '"status": "DENIED"' in result_text(res) or '"status":"DENIED"' in result_text(res):
                decision = ("DENIED", "tool", None, (), (), None)
        if method == "tools/call" and decision is None:
            code = (err or {}).get("code") if err else None
            msg = (err or {}).get("message", "") if err else ""
            if code == -32042:
                decision = ("CONSENT_REQUIRED", "identity", code, (), (), None)
            elif code == -32005:
                decision = ("SESSION_STALE", "policy", code, (), (), None)
            elif code == -32002:
                decision = ("SUPPRESSED" if status == 403 or "Output blocked" in msg else "DENIED", "policy", code, policy_ids(msg), (), None)
            elif err:
                decision = ("ERROR", "gateway", code, (), (), None)
            else:
                decision = ("ALLOWED", "none", None, (), (), None)
    if method == "tools/call" and decision:
        note["decision"] = decision[0]
        trail(header(req, PS_HEADER), req_body.get("id"), tool, decision[0], decision[1], status, decision[2], decision[3], decision[4], decision[5])
    print(json.dumps(note))
    return {"interceptorOutputVersion": "1.0", "mcp": {"transformedGatewayResponse": {"statusCode": status, "body": body}}}
