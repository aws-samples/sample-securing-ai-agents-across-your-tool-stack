# SOURCE: scripts/steps/m1_unauth.py L33-35, L37
for label, tok in CALLERS.items():
    m = gk.Mcp(tok, init=False)
    m.rpc("tools/list")
    m.call("MemberRecords___get_member_profile",
           {"member_id": "M-1001"})
