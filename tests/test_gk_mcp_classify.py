"""classify(): message text counts only for a failed tool call, so tool data cannot change a decision label.

Offline: no AWS call. A successful result is data. Data that contains a Gateway or Strands error phrase stays ALLOWED,
and every real error (a recorded JSON-RPC error, or a result with status "error") keeps its label. The Gateway
messages below are its own wording, as recorded in tests/fixtures/rehearse-run-01.json.
"""
import json

import pytest

import gklib

G = gklib.gk_mcp()

DENY = ("Tool Execution Denied: Tool call not allowed due to policy enforcement [Policy evaluation denied due to "
        "gk_DestinationSeenInSession_v2-jbpmjbb_ns]")
SUPPRESS = "Output blocked by policy: Policy evaluation denied due to gk_SuppressSsnInNotes_v2-7pde6vtlf7"
GUARD = "Output blocked by response interceptor: prompt-attack check (Bedrock Guardrails gk-prompt-guard)"
RATE = "Rate limit exceeded"
ENGINE = "Tool Execution Denied: Policy Evaluation Throttled"
STALE = "Tool Execution Denied: Policy session is stale"
CONSENT = ('MCP Elicitation required: [This request requires more information.] with data '
           '[{"mode": "url", "url": "https://partner.example.com/consent"}]')

# Text that tool data can contain, for example in a partner referral or a member note.
PHRASES = [DENY, SUPPRESS, GUARD, RATE, ENGINE, STALE, CONSENT, "Throttled"]


def ok(text):
    return {"status": "success", "content": [{"text": text}]}


def failed(text):
    return {"status": "error", "content": [{"text": text}]}


def raw(text, is_error):
    return {"jsonrpc": "2.0", "id": 1, "result": {"isError": is_error, "content": [{"type": "text", "text": text}]}}


@pytest.mark.parametrize("phrase", PHRASES)
def test_phrase_in_successful_result_stays_allowed(phrase):
    assert G.classify(ok(phrase)) == {"decision": "ALLOWED"}
    assert G.classify(ok(json.dumps({"member_id": "M-1001", "note": phrase}))) == {"decision": "ALLOWED"}
    assert gklib.classify(raw(phrase, False)) == {"decision": "ALLOWED"}  # the direct-call path


def test_tool_level_refusal_still_counts():
    refusal = G.classify(ok(json.dumps({"status": "DENIED", "reason": "not entitled"})))
    assert refusal == {"decision": "DENIED_TOOL", "reason": "not entitled"}
    assert G.classify(ok(json.dumps({"status": "EXECUTED"}))) == {"decision": "ALLOWED", "status": "EXECUTED"}


RECORDED = [
    ({"code": -32002, "message": DENY},
     {"decision": "DENIED", "layer": "policy", "policies": ["gk_DestinationSeenInSession_v2"]}),
    ({"code": -32002, "message": SUPPRESS},
     {"decision": "SUPPRESSED", "layer": "policy", "policies": ["gk_SuppressSsnInNotes_v2"]}),
    ({"code": -32002, "message": GUARD}, {"decision": "SUPPRESSED", "layer": "interceptor", "policies": ["prompt-guard"]}),
    ({"code": -32003, "message": RATE, "data": {"limitKey": "per-staff-member-records", "retryAfter": 1}},
     {"decision": "THROTTLED", "limit": "rate-limit", "limit_key": "per-staff-member-records", "retry_after": 1}),
    ({"code": -32003, "message": ENGINE}, {"decision": "THROTTLED", "limit": "policy-evaluation"}),
    ({"code": -32005, "message": STALE}, {"decision": "SESSION_STALE"}),
    ({"code": -32042, "message": "This request requires more information.",
      "data": {"elicitations": [{"mode": "url", "url": "https://partner.example.com/consent"}]}},
     {"decision": "CONSENT_REQUIRED"}),
]


@pytest.mark.parametrize("err, want", RECORDED)
def test_recorded_error_keeps_its_label(err, want):
    # The agent hook passes the error result with the recorded error; the direct-call path passes only the error.
    for result in (failed("Tool execution failed: " + err["message"]), None):
        d = G.classify(result, err)
        assert {k: d.get(k) for k in want} == want


TEXT_ONLY = [(DENY, "DENIED"), (SUPPRESS, "SUPPRESSED"), (GUARD, "SUPPRESSED"), (RATE, "THROTTLED"),
             (ENGINE, "THROTTLED"), (STALE, "SESSION_STALE"), (CONSENT, "CONSENT_REQUIRED")]


@pytest.mark.parametrize("text, want", TEXT_ONLY)
def test_failed_call_without_recorded_error_still_reads_its_text(text, want):
    assert G.classify(failed(text))["decision"] == want
    assert gklib.classify(raw(text, True))["decision"] == want
