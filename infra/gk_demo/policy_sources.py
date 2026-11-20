"""Policy statements of generation 2, the demo's policy set (gk_demo/generations.py).

Any change to a deployed statement is a static modification, and CloudFormation would call UpdatePolicy on it.
policies/ holds the same rules in readable form; generation 3 loads its statements from there instead.
"""


def act(t):
    return f'AgentCore::Action::"MemberRecords___{t}"'


def demo_statements(res):
    """The four demo policies, scoped to the gateway (res = 'resource == AgentCore::Gateway::"<arn>"')."""
    return {
        "gk_PermitAll": ("cedar", "ACTIVE", f"permit (principal, action, {res});"),
        "gk_SuppressSsnInNotes": ("policy", "LOG_ONLY",
                                  f"suppressOutput (principal, action == {act('get_member_notes')}, {res}) when guardrails {{ "
                                  'BedrockGuardrails::SensitiveInformation(["US_SOCIAL_SECURITY_NUMBER"], [context.output.notes_text])'
                                  '.maxConfidenceScore().greaterThan(decimal("0.2")) };'),
        "gk_DestinationSeenInSession": ("policy", "LOG_ONLY",
                                        # Two unless-temporal clauses, which Cedar ANDs: the destination must be the member's
                                        # own checking or savings account, returned earlier in this session.
                                        f"forbid (principal, action == {act('initiate_transfer')}, {res}) "
                                        f"unless temporal {{ formerly within 1h {act('get_member_accounts')}::response{{ eventResource: resource, input.member_id: context.input.member_id, output.checking_ref: context.input.destination_ref }} }} "
                                        f"unless temporal {{ formerly within 1h {act('get_member_accounts')}::response{{ eventResource: resource, input.member_id: context.input.member_id, output.savings_ref: context.input.destination_ref }} }};"),
        "gk_ApprovalOver2500": ("policy", "LOG_ONLY",
                                f"forbid (principal, action == {act('initiate_transfer')}, {res}) when {{ context.input.amount > 2500 }} unless temporal {{ "
                                f"formerly within 1h {act('record_transfer_approval')}::response{{ eventResource: resource, input.member_id: context.input.member_id, "
                                "input.destination_ref: context.input.destination_ref, input.amount: context.input.amount, output.approved: true } };"),
    }
