# SOURCE: scripts/gklib.py L248-250
c.update_gateway_rate_limit(gatewayIdentifier=gid,
    rateLimitId="per-staff-member-records",
    entries=[{
        "dimensions": {"targetName": "MemberRecords",
                       "$.context.jwt.sub": "*"},
        "requests": [{"rate": set_to, "period": "second"}]}])
