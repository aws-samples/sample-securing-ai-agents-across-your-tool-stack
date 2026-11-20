"""Shared helpers for the demo scripts and tests.

Passwords are read from Secrets Manager inside these functions and never printed. Tokens stay in memory.
Output helpers mask the account ID, which comes from STS (no account ID in any file).

Environment: GK_STACK (default gk-demo), GK_REGION (default us-east-2).
"""
import json
import os
import re
import sys
import time
import uuid

import boto3
import requests

REGION = os.environ.get("GK_REGION", "us-east-2")
STACK = os.environ.get("GK_STACK", "gk-demo")
SECRET_PREFIX = STACK  # the stack names its generated user secrets <stack>/<pool>/<user> (stack.py, from stack_name)
SESSION = boto3.session.Session(region_name=REGION)
LOGS = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs"))
# Generation 2 (engine 2) policy names, used when the stack has no ActivePolicies output yet.
GEN2_POLICIES = ["gk_PermitAll_v2", "gk_SuppressSsnInNotes_v2", "gk_DestinationSeenInSession_v2", "gk_ApprovalOver2500_v2"]
_outputs = None
_account = None


def account():
    global _account
    if _account is None:
        _account = SESSION.client("sts").get_caller_identity()["Account"]
    return _account


def mask(text):
    return re.sub(account(), "<acct>", str(text))


def out(refresh=False):
    global _outputs
    if _outputs is None or refresh:
        o = SESSION.client("cloudformation").describe_stacks(StackName=STACK)["Stacks"][0]["Outputs"]
        _outputs = {x["OutputKey"]: x["OutputValue"] for x in o}
    return _outputs


def save(name, data):
    """Write masked JSON evidence to logs/<name>-<epoch>.json and return the path."""
    os.makedirs(LOGS, exist_ok=True)
    path = os.path.join(LOGS, f"{name}-{int(time.time())}.json")
    with open(path, "w") as f:
        f.write(mask(json.dumps(data, indent=1, default=str)))
    return path


def password(pool, user):
    return SESSION.client("secretsmanager").get_secret_value(SecretId=f"{SECRET_PREFIX}/{pool}/{user}")["SecretString"]


def staff_auth(user, client="console"):
    cid = out()["ConsoleClientId"] if client == "console" else out()["ApproverClientId"]
    r = SESSION.client("cognito-idp").admin_initiate_auth(UserPoolId=out()["StaffPoolId"], ClientId=cid, AuthFlow="ADMIN_USER_PASSWORD_AUTH",
                                                          AuthParameters={"USERNAME": user, "PASSWORD": password("staff", user)})
    return r["AuthenticationResult"]


def access(user):
    return staff_auth(user)["AccessToken"]


def parse(resp, want_id):
    ct = resp.headers.get("content-type", "")
    txt = resp.text
    if "event-stream" in ct or txt.lstrip().startswith(("event:", "data:", "id:")):
        msgs = []
        for line in txt.splitlines():
            if line.startswith("data:"):
                try:
                    msgs.append(json.loads(line[5:].strip()))
                except ValueError:
                    pass
        return next((m for m in msgs if m.get("id") == want_id), msgs[-1] if msgs else None)
    try:
        return resp.json()
    except ValueError:
        return {"_raw": txt[:300]}


class Mcp:
    """Minimal MCP streamable-HTTP client for the gateway (one connection per instance)."""

    def __init__(self, bearer=None, policy_session="new", url=None, protocol="2025-11-25", init=True):
        self.url = url or out()["GatewayUrl"]
        self.bearer = bearer
        self.ps = str(uuid.uuid4()) if policy_session == "new" else policy_session
        self.protocol = protocol
        self.sid = None
        self.n = 0
        self.last_status = None
        if init:
            self.init_result = self.rpc("initialize", {"protocolVersion": protocol, "capabilities": {"elicitation": {"url": {}, "form": {}}},
                                                       "clientInfo": {"name": "gk-demo-tests", "version": "0.1"}})
            self.notify("notifications/initialized")

    def headers(self):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": self.protocol}
        if self.bearer:
            h["Authorization"] = f"Bearer {self.bearer}"
        if self.ps:
            h["x-amzn-bedrock-agentcore-policy-session-id"] = self.ps
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        return h

    def rpc(self, method, params=None):
        self.n += 1
        body = {"jsonrpc": "2.0", "id": self.n, "method": method}
        if params is not None:
            body["params"] = params
        r = requests.post(self.url, headers=self.headers(), json=body, timeout=60)
        self.last_status = r.status_code
        self.sid = r.headers.get("Mcp-Session-Id", self.sid)
        return parse(r, self.n)

    def notify(self, method):
        requests.post(self.url, headers=self.headers(), json={"jsonrpc": "2.0", "method": method}, timeout=30)

    def tools(self):
        return (self.rpc("tools/list") or {}).get("result", {}).get("tools", [])

    def call(self, name, args=None, meta=None):
        params = {"name": name, "arguments": args or {}}
        if meta:
            params["_meta"] = meta
        return self.rpc("tools/call", params)


def result_obj(msg):
    """Return (object parsed from the result text or None, text, error dict or None)."""
    if not msg:
        return None, "", {"message": "no message"}
    if msg.get("error"):
        return None, "", msg["error"]
    res = msg.get("result") or {}
    text = " ".join(c.get("text", "") for c in res.get("content") or [] if isinstance(c, dict))
    sc = res.get("structuredContent")
    if sc is None:
        try:
            sc = json.loads(text)
        except (ValueError, TypeError):
            sc = None
    return sc, text, ({"isError": True, "text": text[:300]} if res.get("isError") else None)


_GK_MCP = None


def gk_mcp():
    """The agent's MCP module (agent/gk_mcp), loaded on first use: its classify() is the one classifier for the agent and
    the direct-call path. Importing it pulls in Strands, about 1.5 s, so it is lazy."""
    global _GK_MCP
    if _GK_MCP is None:
        agent_dir = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "agent"))
        if agent_dir not in sys.path:
            sys.path.insert(0, agent_dir)
        import gk_mcp as mod  # noqa: PLC0415
        _GK_MCP = mod
    return _GK_MCP


def classify(msg):
    """Decision labels for one raw MCP tools/call response, exactly as the agent's hook computes them."""
    g = gk_mcp()
    if msg and msg.get("error"):
        e = msg["error"]
        return g.classify(None, {"code": e.get("code"), "message": e.get("message"), "data": e.get("data")})
    res = (msg or {}).get("result") or {}
    return g.classify({"status": "error" if res.get("isError") else "success", "content": res.get("content") or []})


def call(m, tool, args):
    """Call a MemberRecords tool; return a compact dict (no raw text in "decision"), with the shared classifier's decision."""
    msg = m.call(f"MemberRecords___{tool}", args)
    sc, text, err = result_obj(msg)
    code = (msg or {}).get("error", {}).get("code") if msg and msg.get("error") else None
    emsg = (msg or {}).get("error", {}).get("message", "") if code else ""
    return {"http": m.last_status, "sc": sc, "err": err, "err_code": code, "err_message": emsg[:300], "text": text, "decision": classify(msg)}


def ctl():
    return SESSION.client("bedrock-agentcore-control")


def engine_id():
    return out()["EngineId"]


def active_policies():
    v = out().get("ActivePolicies")
    return v.split(",") if v else list(GEN2_POLICIES)


def policy_id(name):
    return out()[f"Policy{name.replace('_', '')}"]


def get_policy(name):
    return ctl().get_policy(policyEngineId=engine_id(), policyId=policy_id(name))


def set_policy_mode(name, mode, wait=True, timeout=120):
    """UpdatePolicy (IGNORE_ALL_FINDINGS) if needed, then poll until status ACTIVE in that mode. Returns seconds."""
    pid, eng, c = policy_id(name), engine_id(), ctl()
    cur = c.get_policy(policyEngineId=eng, policyId=pid)
    t0 = time.time()
    if cur.get("enforcementMode") != mode or "FAIL" in str(cur.get("status")):
        c.update_policy(policyEngineId=eng, policyId=pid, enforcementMode=mode, validationMode="IGNORE_ALL_FINDINGS")
    while wait:
        g = c.get_policy(policyEngineId=eng, policyId=pid)
        if g.get("status") == "ACTIVE" and g.get("enforcementMode") == mode:
            break
        if "FAIL" in str(g.get("status")):
            raise RuntimeError(f"policy {name} {g.get('status')}: {g.get('statusReasons')}")
        if time.time() - t0 > timeout:
            raise RuntimeError(f"policy {name} did not reach ACTIVE {mode} in {timeout} s: {g.get('status')} {g.get('enforcementMode')}")
        time.sleep(2)
    return round(time.time() - t0, 1)


def ssm_put(name, value):
    SESSION.client("ssm").put_parameter(Name=name, Value=value, Type="String", Overwrite=True)


def ssm_get(name):
    return SESSION.client("ssm").get_parameter(Name=name)["Parameter"]["Value"]


def invoke_reset(payload):
    r = SESSION.client("lambda").invoke(FunctionName=out()["ResetFunction"], Payload=json.dumps(payload).encode())
    return json.loads(r["Payload"].read())


def rate_limit(set_to=None):
    c, gid = ctl(), out()["GatewayId"]
    cur = c.get_gateway_rate_limit(gatewayIdentifier=gid, rateLimitId="per-staff-member-records")["entries"][0]["requests"][0]["rate"]
    if set_to is not None and cur != set_to:
        c.update_gateway_rate_limit(gatewayIdentifier=gid, rateLimitId="per-staff-member-records",
                                    entries=[{"dimensions": {"targetName": "MemberRecords", "$.context.jwt.sub": "*"},
                                              "requests": [{"rate": set_to, "period": "second"}]}])
        cur = set_to
    return cur


def logs_since(group, since_ms, pattern="", with_ts=False):
    """Messages (or (timestamp ms, message) pairs with with_ts) of a log group since since_ms."""
    c = SESSION.client("logs")
    ev, tok = [], None
    while True:
        kw = {"logGroupName": group, "startTime": since_ms, "limit": 1000}
        if pattern:
            kw["filterPattern"] = pattern
        if tok:
            kw["nextToken"] = tok
        r = c.filter_log_events(**kw)
        ev += [(e["timestamp"], e["message"]) if with_ts else e["message"] for e in r.get("events", [])]
        tok = r.get("nextToken")
        if not tok:
            return ev
