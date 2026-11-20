"""Offline tests for the console's direct-call path (console/core.py Console.direct): no AWS, no network.

  .venv/bin/python -m pytest -q tests/test_console_direct.py

gklib's Mcp and call are replaced by fakes that record the policy session and the arguments of every call, the persona
tokens by a stub, and the trail poller by a no-op. M5b (the "Direct call (M5b)" button) must reuse the pane's existing
policy session, the one in which "Approve as Dana" recorded the approval, and send the savings and checking refs with
amount 4000; M5 must be unchanged.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))
import console.core as core  # noqa: E402

ACCOUNTS = {"member_id": "M-1001", "savings_ref": "acct_SAV", "checking_ref": "acct_CHK", "accounts": []}


class FakeLoop:
    def call_soon_threadsafe(self, *args):
        pass


class FakeMcp:
    def __init__(self, bearer=None, policy_session="new", **kw):
        self.bearer, self.ps = bearer, policy_session


@pytest.fixture()
def console(monkeypatch):
    calls = []

    def fake_call(m, tool, args):
        calls.append((m.ps, tool, dict(args), m.bearer))
        sc = ACCOUNTS if tool == "get_member_accounts" else {"status": "EXECUTED"}
        return {"http": 200, "sc": sc, "err": None, "err_code": None, "err_message": "", "text": "", "decision": {"decision": "ALLOWED"}}

    monkeypatch.setattr(core.gk, "Mcp", FakeMcp)
    monkeypatch.setattr(core.gk, "call", fake_call)
    c = core.Console(FakeLoop())
    monkeypatch.setattr(c.tokens, "get", lambda persona: f"stub-{persona}")
    monkeypatch.setattr(c, "_poll_trail", lambda pane, done, since: None)
    c.calls = calls
    return c


def test_m5b_reuses_the_panes_policy_session_and_moves_4000_from_savings_to_checking(console):
    console.panes["maya"].ps = "ps-with-danas-approval"
    r = console.direct("maya", "m5b")
    assert r["policy_session_id"] == "ps-with-danas-approval"
    assert console.panes["maya"].ps == "ps-with-danas-approval"
    assert [(ps, tool, args) for ps, tool, args, _ in console.calls] == [
        ("ps-with-danas-approval", "get_member_accounts", {"member_id": "M-1001"}),
        ("ps-with-danas-approval", "initiate_transfer", {"member_id": "M-1001", "source_ref": "acct_SAV", "destination_ref": "acct_CHK", "amount": 4000}),
    ]
    assert all(tok == "stub-maya" for *_, tok in console.calls)
    assert [c["tool"] for c in r["calls"]] == ["get_member_accounts", "initiate_transfer"]
    rows = console.feed.rows[-2:]
    assert [x["tool"] for x in rows] == ["get_member_accounts", "initiate_transfer"] and all("direct" in x["sources"] for x in rows)


def test_m5_is_unchanged(console):
    r = console.direct("maya", "m5")
    ps = r["policy_session_id"]
    assert ps and console.panes["maya"].ps == ps  # a new session when the pane has none, kept on the pane
    assert [(p, tool, args) for p, tool, args, _ in console.calls] == [
        (ps, "get_member_accounts", {"member_id": "M-1001"}),
        (ps, "initiate_transfer", {"member_id": "M-1001", "source_ref": "acct_SAV", "destination_ref": "EXT-4471", "amount": 800}),
    ]


def test_m5b_without_a_session_creates_one_and_only_maya_and_known_moments_are_accepted(console):
    r = console.direct("maya", "m5b")
    assert r["policy_session_id"] and console.panes["maya"].ps == r["policy_session_id"]
    with pytest.raises(ValueError):
        console.direct("right", "m5b")
    with pytest.raises(ValueError):
        console.direct("maya", "m6")


class FakeConversation:
    """Replays a scripted list of agent events for one turn."""

    def __init__(self, events):
        self.events, self.token = events, None

    def turn(self, prompt, on_event):
        for ev in self.events:
            on_event(ev)
        return {"http": 200, "latency_s": 0.1, "consent": [e["url"] for e in self.events if e.get("type") == "consent"]}


def test_connect_button_hides_once_the_partner_call_is_allowed(console):
    pane = console.panes["maya"]
    pane.conv = FakeConversation([{"type": "decision", "tool": "Partner___partner_list_referrals", "decision": "CONSENT_REQUIRED"},
                                  {"type": "consent", "tool": "Partner___partner_list_referrals", "url": "https://portal.example/connect"}])
    console._turn(pane, "Show my open partner referrals.")
    assert pane.consent_url == "https://portal.example/connect"
    pane.conv = FakeConversation([{"type": "decision", "tool": "Partner___partner_list_referrals", "decision": "ALLOWED"}])
    console._turn(pane, "Show my open partner referrals again.")
    assert pane.consent_url is None
    assert pane.snapshot()["consent_url"] is None
