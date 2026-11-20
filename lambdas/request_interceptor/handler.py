"""REQUEST interceptor (gk-request-interceptor).

For MemberRecords tools: strips identity-like arguments the model wrote, then, by mode
(SSM /gk/deputy-mode, 5-second cache): service = forward arguments only; impersonation =
put the raw inbound access token in `_credential`; act_on_behalf = mint a one-time,
call-bound ES256 JWS (KMS) naming the staff member as subject and the agent as actor.
Never logs headers, arguments or tokens.
"""
import base64
import hashlib
import json
import os
import time
import uuid

import boto3

SSM = boto3.client("ssm")
KMS = boto3.client("kms")
KEY_ID = os.environ["SIGNING_KEY_ID"]
MODE_PARAM = os.environ["MODE_PARAM"]
TARGET_PREFIX = "MemberRecords___"
STRIP = {"_credential", "credential", "token", "staff_id", "user_id", "sub", "subject", "role", "roles",
         "requested_by", "acting_for", "on_behalf_of", "actor"}
_cache = {}


def mode():
    hit = _cache.get("mode")
    if hit and time.time() - hit[1] < 5:
        return hit[0]
    try:
        value = SSM.get_parameter(Name=MODE_PARAM)["Parameter"]["Value"]
    except Exception:  # noqa: BLE001 - interceptor defaults to the safe mode
        value = "act_on_behalf"
    _cache["mode"] = (value, time.time())
    return value


def strip(obj):
    if isinstance(obj, dict):
        return {k: strip(v) for k, v in obj.items() if k not in STRIP}
    if isinstance(obj, list):
        return [strip(v) for v in obj]
    return obj


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def der_to_raw(der):
    """DER ECDSA signature from KMS to JWS raw r||s."""
    i = 2
    out = b""
    for _ in range(2):
        assert der[i] == 0x02
        ln = der[i + 1]
        val = der[i + 2:i + 2 + ln].lstrip(b"\x00")
        out += val.rjust(32, b"\x00")
        i += 2 + ln
    return out


def bearer(headers):
    for k, v in (headers or {}).items():
        if k.lower() == "authorization" and isinstance(v, str) and v.lower().startswith("bearer "):
            return v[7:]
    return ""


def unverified_claims(token):
    try:
        p = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    except Exception:  # noqa: BLE001
        return {}


def mint(staff, tool, args, actor):
    now = int(time.time())
    clean = {k: v for k, v in args.items() if k != "_credential"}
    claims = {"iss": "urn:anycompany:gatekeepers:interceptor", "sub": staff.get("sub"), "name": staff.get("username"),
              "roles": staff.get("cognito:groups", []), "act": {"sub": actor}, "aud": "urn:anycompany:target:MemberRecords",
              "scope": "member-records:invoke", "tool": tool,
              "args_sha256": hashlib.sha256(json.dumps(clean, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
              "jti": uuid.uuid4().hex, "iat": now, "nbf": now, "exp": now + 300}
    signing_input = b64u(json.dumps({"alg": "ES256", "typ": "JWT"}).encode()) + "." + b64u(json.dumps(claims).encode())
    der = KMS.sign(KeyId=KEY_ID, Message=signing_input.encode(), MessageType="RAW", SigningAlgorithm="ECDSA_SHA_256")["Signature"]
    return signing_input + "." + b64u(der_to_raw(der))


def handler(event, context):
    mcp = event.get("mcp") or {}
    req = mcp.get("gatewayRequest") or {}
    body = req.get("body") or {}
    method = body.get("method")
    tool = ((body.get("params") or {}).get("name")) or ""
    injected = "none"
    if method == "tools/call" and tool.startswith(TARGET_PREFIX):
        params = body.setdefault("params", {})
        args = strip(params.get("arguments") or {})
        m = mode()
        if m == "impersonation":
            args["_credential"] = bearer(req.get("headers"))
            injected = "raw_token"
        elif m == "act_on_behalf":
            staff = unverified_claims(bearer(req.get("headers")))  # the Gateway already validated this JWT
            actor = "urn:anycompany:app:approval-console" if tool.endswith("record_transfer_approval") else "urn:anycompany:agent:member-navigator"
            args["_credential"] = mint(staff, tool, args, actor)
            injected = "aob_jws"
        params["arguments"] = args
    print(json.dumps({"point": "REQUEST", "method": method, "tool": tool, "injected": injected,
                      "has_headers": bool(req.get("headers")), "request_id": context.aws_request_id}))
    return {"interceptorOutputVersion": "1.0", "mcp": {"transformedGatewayRequest": {"body": body}}}
