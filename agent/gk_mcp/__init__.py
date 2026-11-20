"""The agent's MCP client module for the Gateway, shared by the agent and the direct-call path.

- `client(bearer, policy_session_id)` builds a Strands MCP client whose transport sends two default headers on every
  request: the staff Bearer token and the policy session ID. MCP headers are fixed per connection, so the agent opens
  one client per turn. `record_transfer_approval` is filtered out (hygiene, not a control).
- `GkMCPClient` keeps the JSON-RPC error (code, message, data) of each failed tool call, keyed by tool use ID, so the
  agent's hook classifies by the exact code instead of by message text. The model-visible text is unchanged.
- `classify()` maps a tool result to the console's decision labels. It reads message text only for a failed call, never
  inside a successful result, and it never returns tokens or result content.
"""
import json
import os
import re

import httpx2
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from strands.tools.mcp import MCPClient

POLICY_SESSION_HEADER = "x-amzn-bedrock-agentcore-policy-session-id"
APPROVAL_TOOL = "MemberRecords___record_transfer_approval"
CONSENT_TEXT = "The user must connect the partner system; a link was shown."

# JSON-RPC codes on the wire.
CONSENT, DENIED, THROTTLED, STALE = -32042, -32002, -32003, -32005
# The response interceptor's prompt-attack fallback answers like a policy suppression (HTTP 403, -32002, "Output blocked
# by ...") and names itself in the message, so the console can show which layer acted.
INTERCEPTOR_MARK = "response interceptor"
PROMPT_GUARD = "prompt-guard"


class GkMCPClient(MCPClient):
    """Strands MCP client that remembers the JSON-RPC error of each failed call (by tool use ID)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.errors = {}

    def _handle_tool_execution_error(self, tool_use_id, exception):
        if isinstance(exception, MCPError):
            self.errors[tool_use_id] = {"code": exception.error.code, "message": exception.error.message,
                                        "data": exception.error.data}
        return super()._handle_tool_execution_error(tool_use_id, exception)


def client(bearer, policy_session_id, url=None, timeout=60.0):
    """A Gateway MCP client for one turn. `bearer` is the staff access token, forwarded unchanged."""
    url = url or os.environ["GK_GATEWAY_URL"]
    headers = {"Authorization": f"Bearer {bearer}", POLICY_SESSION_HEADER: policy_session_id}

    def transport():
        return streamable_http_client(url, http_client=httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(timeout, read=300.0)))

    return GkMCPClient(transport, tool_filters={"rejected": [APPROVAL_TOOL]})


def _text(result):
    return " ".join(c.get("text", "") for c in (result or {}).get("content") or [] if isinstance(c, dict))


def policy_ids(message):
    """Policy IDs named in a deny or suppression message ("... denied due to <id>[, <id>]")."""
    m = re.search(r"denied due to ([^\]]+)", message or "")
    if not m:
        return []
    return [t for t in re.split(r"[,\s]+", m.group(1)) if re.fullmatch(r"[A-Za-z][\w-]*-[\w]+", t)]


def policy_name(pid):
    return pid.rsplit("-", 1)[0] if "-" in pid else pid


def consent_urls(err, text):
    urls = [e.get("url") for e in ((err or {}).get("data") or {}).get("elicitations") or [] if isinstance(e, dict) and e.get("url")]
    if not urls:
        urls = re.findall(r"https://[^\s\"'\]\}]+", text or "")
    return urls


def classify(result, err=None):
    """Decision for one tool result: {decision, policy_ids?, policies?, limit_key?, retry_after?, reason?}.

    err is the recorded JSON-RPC error for the call, if any. Message text counts only for a failed call (a recorded error,
    or a result with status "error", which Strands sets for every failed call): a successful result is tool data, and
    data can contain any phrase. Order matters: a stale session (-32005) also begins with "Tool Execution Denied", so it
    is checked first when only the text is available.
    """
    text = _text(result)
    code = (err or {}).get("code")
    failed = err is not None or (result or {}).get("status") == "error"
    msg = ((err or {}).get("message") or text) if failed else ""
    if code == CONSENT or (failed and text.startswith("MCP Elicitation required")):
        return {"decision": "CONSENT_REQUIRED", "code": CONSENT}
    if code == STALE or "Policy session is stale" in msg:
        return {"decision": "SESSION_STALE", "code": STALE}
    if code == THROTTLED or "Rate limit exceeded" in msg or "Throttled" in msg:
        if "Policy Evaluation Throttled" in msg:  # the policy engine's own protection, not per identity
            return {"decision": "THROTTLED", "code": THROTTLED, "limit": "policy-evaluation"}
        data = (err or {}).get("data") or {}
        return {"decision": "THROTTLED", "code": THROTTLED, "limit": "rate-limit", "limit_key": data.get("limitKey"),
                "retry_after": data.get("retryAfter")}
    if code == DENIED or "Tool Execution Denied" in msg or "Output blocked by" in msg:
        if "Output blocked by" in msg and INTERCEPTOR_MARK in msg:  # the response interceptor's prompt guard
            return {"decision": "SUPPRESSED", "code": DENIED, "layer": "interceptor", "policies": [PROMPT_GUARD]}
        ids = policy_ids(msg)
        kind = "SUPPRESSED" if "Output blocked by" in msg else "DENIED"
        return {"decision": kind, "code": DENIED, "layer": "policy", "policy_ids": ids, "policies": [policy_name(i) for i in ids]}
    if (result or {}).get("status") == "error":
        return {"decision": "ERROR", "code": code}
    # A successful result can still carry a tool-level refusal (entitlement, approval checks).
    try:
        body = json.loads(text)
    except (TypeError, ValueError):
        body = None
    if isinstance(body, dict):
        if body.get("status") == "DENIED":
            return {"decision": "DENIED_TOOL", "reason": str(body.get("reason", ""))[:120]}
        if body.get("status") == "EXECUTED":
            return {"decision": "ALLOWED", "status": "EXECUTED"}
    return {"decision": "ALLOWED"}
