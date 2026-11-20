"""Offline tests for scripts/goto.py's plan: no AWS, no network.

  .venv/bin/python -m pytest -q tests/test_goto_plan.py

For every target: the plan starts with Leo's grant and reset.py, its steps follow the talk order (scripts/moments.py
ORDER and AFTER_STEPS), it never passes impersonation, and simulating its steps ends in the target's before state.
The expected before states below are written out by hand, independently of goto.EFFECTS.
"""
import goto
import moments as mo

RESET_STATE = {"deputy-mode": "service", "masking": "off", "prompt-guard": "off", "PermitAll": "ACTIVE", "SuppressSsnInNotes": "LOG_ONLY",
               "DestinationSeenInSession": "LOG_ONLY", "ApprovalOver2500": "LOG_ONLY", "rate": 10000, "R-2003": "held",
               "maya": "consent needed", "leo": "connected"}
CHANGES = {  # what each moment's after state adds
    "M1": {"maya": "connected"},
    "M2": {},
    "M3": {"deputy-mode": "act_on_behalf"},
    "M4": {"masking": "on", "SuppressSsnInNotes": "ACTIVE"},
    "M5": {"DestinationSeenInSession": "ACTIVE"},
    "M5c": {"prompt-guard": "on", "R-2003": "open"},
    "M5b": {"ApprovalOver2500": "ACTIVE"},
}
TALK = ["M1", "M2", "M3", "M4", "M5", "M5c", "M5b", "M6"]


def before_state(target):
    st = dict(RESET_STATE)
    for mid in TALK[:TALK.index(target)]:
        st.update(CHANGES[mid])
    return st


def test_targets_are_the_talk_moments():
    assert goto.TARGETS == TALK
    assert [m for m in mo.ORDER if m in TALK] == TALK


def test_every_plan_is_in_talk_order_and_never_impersonation():
    for t in goto.TARGETS:
        p = goto.plan(t)
        assert [s["step"] for s in p[:2]] == [goto.START, goto.RESET], t
        idx = [mo.ORDER.index(s["moment"]) for s in p[2:]]
        assert idx == sorted(idx), (t, idx)
        assert all(i < mo.ORDER.index(t) for i in idx), t
        want = [tuple(s) for mid in mo.ORDER[:mo.ORDER.index(t)] for s in mo.AFTER_STEPS[mid]]
        assert [s["step"] for s in p[2:]] == want, t
        assert not any("impersonation" in str(x) for s in p for x in s["step"]), t


def test_every_plan_ends_in_the_targets_before_state():
    for t in goto.TARGETS:
        assert goto.expected_state(goto.plan(t)) == before_state(t), t


def test_plan_contents_for_the_middle_and_last_moments():
    p = [" ".join(s["step"]) for s in goto.plan("M5b")]
    assert p == ["grants leo", "reset.py", "consent maya", "steps/m3_mode.py act_on_behalf", "steps/m4_mask.py", "steps/m4_guardrail.py",
                 "steps/m5_enforce.py", "steps/m5c_guardrail.py"]
    assert goto.plan("M6")[-1]["step"] == ("steps/m5b_enforce.py",)
    assert "steps/m6_limit.py" not in [s["step"][0] for t in goto.TARGETS for s in goto.plan(t)]  # M6 is last: its limit is never pre-applied


def test_after_steps_never_carry_impersonation_and_have_effects():
    for mid, steps in mo.AFTER_STEPS.items():
        for s in steps:
            assert "impersonation" not in s, mid
            assert tuple(s) in goto.EFFECTS, (mid, s)
    assert len(mo.AFTER_STEPS["M6"]) == 1  # run_m6 takes AFTER_STEPS["M6"][0]


def test_unknown_target_and_impersonation_are_refused(monkeypatch):
    import pytest
    with pytest.raises(ValueError):
        goto.plan("R1.2")
    monkeypatch.setitem(mo.AFTER_STEPS, "M3", [("steps/m3_mode.py", "impersonation")])
    with pytest.raises(ValueError):
        goto.plan("M4")


def test_next_actions_name_the_targets_prompts():
    lines = goto.next_actions("M5b", True)
    assert lines[0].startswith("Start new conversations in both panes")
    assert any("'M5b $4,000 transfer', then 'M5b Retry after approval'" in ln for ln in lines)
    assert any("scripts/steps/m5b_enforce.py" in ln for ln in lines)
    assert any("'M2 Referrals'" in ln for ln in goto.next_actions("M2", False))
