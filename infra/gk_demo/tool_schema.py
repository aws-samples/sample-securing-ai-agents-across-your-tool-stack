"""MemberRecords tool schemas.

Every tool declares an output schema, so policies can read `context.output.*`. The Gateway does not pass these
schemas or `structuredContent` to MCP clients; results reach the agent as JSON text.
`get_member_accounts` returns `checking_ref` and `savings_ref` as flat scalars because temporal predicates cannot
correlate list elements.
"""
from aws_cdk import aws_bedrockagentcore as ac

GT = ac.CfnGatewayTarget


def sd(t, props=None, required=None, items=None, desc=None):
    """Tool schema node (CfnGatewayTarget.SchemaDefinitionProperty)."""
    return GT.SchemaDefinitionProperty(type=t, description=desc, properties=props, required=required, items=items)


S = lambda d=None: sd("string", desc=d)  # noqa: E731
I = lambda d=None: sd("integer", desc=d)  # noqa: E731
B = lambda d=None: sd("boolean", desc=d)  # noqa: E731
CRED = {"_credential": S("Set by the gateway. Leave empty.")}
DENY = {"status": S("Set when the call was refused"), "reason": S("Why the call was refused")}


def tools():
    member = {"member_id": S("Member ID, for example M-1001")}
    return [
        GT.ToolDefinitionProperty(name="get_member_profile", description="Get a credit union member's profile: name, contact, SSN, branch.",
                                  input_schema=sd("object", {**member, **CRED}, ["member_id"]),
                                  output_schema=sd("object", {"member_id": S(), "name": S(), "email": S(), "phone": S(), "branch": S(), "ssn": S(), **DENY})),
        GT.ToolDefinitionProperty(name="get_member_notes", description="Get a member's free-text service notes, joined into one string.",
                                  input_schema=sd("object", {**member, **CRED}, ["member_id"]),
                                  output_schema=sd("object", {"member_id": S(), "note_count": I(), "notes_text": S(), **DENY})),
        GT.ToolDefinitionProperty(name="get_member_accounts", description="List a member's deposit accounts with opaque account references and balances.",
                                  input_schema=sd("object", {**member, **CRED}, ["member_id"]),
                                  output_schema=sd("object", {"member_id": S(), "checking_ref": S(), "savings_ref": S(),
                                                              "accounts": sd("array", items=sd("object", {"account_ref": S(), "type": S(), "masked_number": S(),
                                                                                                           "full_number": S(), "balance": I()})), **DENY})),
        GT.ToolDefinitionProperty(name="get_loan_file", description="Get a member's loan file (lending detail).",
                                  input_schema=sd("object", {**member, **CRED}, ["member_id"]),
                                  output_schema=sd("object", {"member_id": S(), "loan_ref": S(), "product": S(), "principal": I(), "apr": S(),
                                                              "status": S(), "underwriting_note": S(), "reason": S()})),
        GT.ToolDefinitionProperty(name="initiate_transfer", description="Move funds for one member from source_ref to destination_ref. Amount in whole dollars.",
                                  input_schema=sd("object", {**member, "source_ref": S(), "destination_ref": S(), "amount": I(), **CRED},
                                                  ["member_id", "source_ref", "destination_ref", "amount"]),
                                  output_schema=sd("object", {"status": S(), "transfer_id": S(), "member_id": S(), "destination_ref": S(), "amount": I(), "reason": S()})),
        GT.ToolDefinitionProperty(name="record_transfer_approval", description="Record a supervisor's approval for a transfer (maker-checker).",
                                  input_schema=sd("object", {**member, "destination_ref": S(), "amount": I(), "approver_id_token": S(), **CRED},
                                                  ["member_id", "destination_ref", "amount", "approver_id_token"]),
                                  output_schema=sd("object", {"approved": B(), "approver_role": S(), "approval_id": S(), "member_id": S(),
                                                              "destination_ref": S(), "amount": I(), **DENY})),
    ]
