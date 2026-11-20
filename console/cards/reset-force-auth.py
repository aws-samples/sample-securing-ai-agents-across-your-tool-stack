# SOURCE: lambdas/reset/handler.py L124-126
if force:
    params["_meta"] = {
      "aws.bedrock-agentcore.gateway/credentialProviderConfiguration":
        {"oauthCredentialProvider": {"forceAuthentication": True}}}
st, _, msg = mcp(tok, {"jsonrpc": "2.0", "id": 2,
    "method": "tools/call", "params": params}, ...)
