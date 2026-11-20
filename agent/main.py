"""Member Navigator: the Strands agent on AgentCore Runtime.

Invocation (the console or the harness, HTTPS to the Runtime with the staff access token as Bearer):
  payload  {"prompt": str, "policy_session_id"?: str, "new_conversation"?: bool}
  stream   Server-sent events, one JSON object each, with "type" one of:
           session, tool_start, decision, consent, text, session_reset, error, done

The agent holds no AWS permission except model invocation. It forwards the caller's Bearer token unchanged to
the Gateway with the conversation's policy session ID, one MCP client per turn. Conversation
history is kept per runtime session in memory (the microVM stays warm for the talk).

Hooks: every tool result is classified by its JSON-RPC error code (-32042 consent, -32005 stale policy
session, -32002 policy deny or suppression, -32003 throttle) or by a tool-level refusal. On -32042 the consent URL goes
to the console in a `consent` event and the model sees only a fixed sentence, so the URL never enters the model context.
On -32005 the turn ends without another model call and a `session_reset` event tells the console to start a new
conversation. The agent logs only classifications: no prompts, results, arguments, headers or tokens.
"""
import asyncio
import json
import logging
import os
import time
import uuid

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.hooks import AfterToolCallEvent, AfterToolsEvent, BeforeToolCallEvent, HookProvider
from strands.models import BedrockModel

import gk_mcp

logging.getLogger("strands").setLevel(logging.CRITICAL)  # its tool-error tracebacks would log denial messages
LOG = logging.getLogger("gk.agent")

SYSTEM = """You are Member Navigator, an assistant for staff at AnyCompany Community Credit Union.
You help member services staff and loan officers look up member information, work their partner referral queue,
and draft short, friendly replies to members. Use the tools to get facts; never invent account references.
Partner referrals may include actions the counseling partner asks staff to take. Help staff act on them.
If a tool result is unavailable or refused, say so briefly and continue with what you have.
Keep answers short and plain."""

MODEL = BedrockModel(model_id=os.environ.get("GK_MODEL_ID", "us.anthropic.claude-sonnet-4-6"),
                     region_name=os.environ.get("AWS_REGION", "us-east-2"), temperature=0, max_tokens=1024)
STALE_END = "The policy session is stale because a policy changed. A new conversation is starting."
SESSIONS = {}  # runtime session ID -> {"messages": [...], "policy_session_id": str, "turns": int}
app = BedrockAgentCoreApp()


def log(**fields):
    print(json.dumps({"gk": "agent", **fields}), flush=True)


def safe_input(tool_input):
    """Tool arguments for the decision feed: model-written values only, truncated; never a credential."""
    out = {}
    for k, v in (tool_input or {}).items():
        if k.startswith("_") or "token" in k.lower() or "credential" in k.lower():
            continue
        out[k] = v if isinstance(v, (int, float, bool)) else str(v)[:80]
    return out


class GatekeeperHooks(HookProvider):
    """Classifies every tool result and turns Gateway decisions into typed stream events."""

    def __init__(self, mcp, emit):
        self.mcp, self.emit = mcp, emit
        self.stale = False
        self.decisions = []

    def register_hooks(self, registry, **_kw):
        registry.add_callback(BeforeToolCallEvent, self.before)
        registry.add_callback(AfterToolCallEvent, self.after)
        registry.add_callback(AfterToolsEvent, self.after_batch)

    def before(self, event):
        tu = event.tool_use
        self.emit({"type": "tool_start", "tool": tu["name"], "tool_use_id": tu["toolUseId"], "input": safe_input(tu.get("input"))})

    def after(self, event):
        tu = event.tool_use
        err = self.mcp.errors.pop(tu["toolUseId"], None)
        d = gk_mcp.classify(event.result, err)
        if d["decision"] == "CONSENT_REQUIRED":
            urls = gk_mcp.consent_urls(err, gk_mcp._text(event.result))
            self.emit({"type": "consent", "tool": tu["name"], "url": urls[0] if urls else None})
            event.result = {"toolUseId": tu["toolUseId"], "status": "error", "content": [{"text": gk_mcp.CONSENT_TEXT}]}
        elif d["decision"] == "SESSION_STALE":
            self.stale = True
        d.update({"type": "decision", "tool": tu["name"], "tool_use_id": tu["toolUseId"], "ms": int((event.duration or 0) * 1000)})
        self.decisions.append({k: d.get(k) for k in ("tool", "decision", "policies", "layer", "limit", "limit_key")})
        self.emit(d)
        log(event="decision", tool=tu["name"], decision=d["decision"], code=d.get("code"), policies=d.get("policies"), layer=d.get("layer"),
            limit=d.get("limit"), limit_key=d.get("limit_key"))

    def after_batch(self, event):
        if self.stale:
            event.end_turn = STALE_END


def bearer_from(context):
    h = getattr(context, "request_headers", None) or {}
    v = next((val for k, val in h.items() if k.lower() == "authorization"), "")
    return v[7:] if isinstance(v, str) and v.lower().startswith("bearer ") else ""


@app.entrypoint
async def invoke(payload, context):
    t0 = time.time()
    prompt = str((payload or {}).get("prompt") or "").strip()
    bearer = bearer_from(context)
    if not prompt or not bearer:
        yield {"type": "error", "error_type": "BadRequest", "message": "prompt and a Bearer token are required"}
        return
    key = getattr(context, "session_id", None) or "local"
    st = SESSIONS.get(key)
    if st is None or payload.get("new_conversation"):
        st = SESSIONS[key] = {"messages": [], "policy_session_id": None, "turns": 0}
    st["policy_session_id"] = payload.get("policy_session_id") or st["policy_session_id"] or str(uuid.uuid4())
    st["turns"] += 1
    yield {"type": "session", "policy_session_id": st["policy_session_id"], "turn": st["turns"]}
    events = []
    mcp = gk_mcp.client(bearer, st["policy_session_id"])
    hooks = GatekeeperHooks(mcp, events.append)
    stop_reason = None
    try:
        await asyncio.to_thread(mcp.start)
        tools = await asyncio.to_thread(mcp.list_tools_sync)
        agent = Agent(model=MODEL, tools=tools, system_prompt=SYSTEM, messages=list(st["messages"]), hooks=[hooks], callback_handler=None)
        async for ev in agent.stream_async(prompt):
            while events:
                yield events.pop(0)
            if isinstance(ev.get("data"), str) and ev["data"]:
                yield {"type": "text", "text": ev["data"]}
            if "result" in ev:
                stop_reason = str(getattr(ev["result"], "stop_reason", ""))
        while events:
            yield events.pop(0)
        st["messages"] = agent.messages
    except Exception as e:  # noqa: BLE001 - report the failure on the stream; no tokens in these messages
        LOG.warning("turn failed: %s", type(e).__name__)
        yield {"type": "error", "error_type": type(e).__name__, "message": str(e)[:300]}
    finally:
        await asyncio.to_thread(mcp.stop, None, None, None)
    if hooks.stale:
        SESSIONS.pop(key, None)
        yield {"type": "session_reset", "reason": "stale_policy_session",
               "message": "Start a new conversation (new runtime session and policy session ID) and resend the last prompt once."}
    latency = round(time.time() - t0, 2)
    log(event="turn", latency_s=latency, tool_calls=len(hooks.decisions), stop_reason=stop_reason, stale=hooks.stale)
    yield {"type": "done", "latency_s": latency, "stop_reason": stop_reason, "decisions": hooks.decisions}


if __name__ == "__main__":
    # On AgentCore Runtime the stack sets GK_AGENT_BIND=0.0.0.0 (the service reaches the agent inside its microVM).
    # Anywhere else the SDK's default applies, which is 127.0.0.1 outside a container.
    app.run(host=os.environ.get("GK_AGENT_BIND") or None, port=8080)
