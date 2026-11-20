"""MemberRecords tools Lambda (gk-member-records).

One Lambda serves six tools. The tool name arrives in the Lambda client context as
`<Target>___<tool>`. Identity comes only from the `_credential` argument injected by the
REQUEST interceptor (impersonation or act-on-behalf mode); model-written identity fields
are ignored. Logs carry decision fields only: no tokens, no PII.
"""
import base64
import hashlib
import json
import os
import time
import uuid

import boto3
import jwt  # PyJWT, from the gk-pyjwt layer
from boto3.dynamodb.conditions import Key

DDB = boto3.resource("dynamodb")
DATA = DDB.Table(os.environ["DATA_TABLE"])
EVENTS = DDB.Table(os.environ["EVENTS_TABLE"])
SSM = boto3.client("ssm")
KMS = boto3.client("kms")

KEY_ID = os.environ["SIGNING_KEY_ID"]
MODE_PARAM = os.environ["MODE_PARAM"]
STAFF_ISSUER = os.environ["STAFF_ISSUER"]
CONSOLE_CLIENT_ID = os.environ["CONSOLE_CLIENT_ID"]
APPROVER_CLIENT_ID = os.environ["APPROVER_CLIENT_ID"]
AOB_ISS = "urn:anycompany:gatekeepers:interceptor"
AOB_AUD = "urn:anycompany:target:MemberRecords"

ENTITLEMENTS = {
    "member_services": {"get_member_profile", "get_member_notes", "get_member_accounts", "initiate_transfer", "record_transfer_approval"},
    "lending": {"get_member_profile", "get_member_notes", "get_loan_file"},
    "supervisor": {"get_member_profile", "get_member_notes", "get_member_accounts"},
}
_cache = {}
_jwks = None


def log(**fields):
    allowed = {"ts", "request_id", "tool", "decision", "reason_code", "member_id", "subject_sub", "actor", "credential_jti", "mode", "event_keys", "context_keys"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"log field not allowed: {sorted(bad)}")
    print(json.dumps(fields))


def mode():
    hit = _cache.get("mode")
    if hit and time.time() - hit[1] < 5:
        return hit[0]
    try:
        value = SSM.get_parameter(Name=MODE_PARAM)["Parameter"]["Value"]
    except Exception:  # noqa: BLE001 - fail closed
        value = "unknown"
    _cache["mode"] = (value, time.time())
    return value


def b64u_dec(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def raw_to_der(sig):
    """JWS ES256 raw r||s (64 bytes) to DER, for kms:Verify."""
    def enc_int(b):
        b = b.lstrip(b"\x00") or b"\x00"
        if b[0] & 0x80:
            b = b"\x00" + b
        return b"\x02" + bytes([len(b)]) + b
    seq = enc_int(sig[:32]) + enc_int(sig[32:])
    return b"\x30" + bytes([len(seq)]) + seq


def canonical_args(args):
    clean = {k: v for k, v in args.items() if k != "_credential"}
    return hashlib.sha256(json.dumps(clean, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def verify_aob(token, tool, args):
    """Verify the act-on-behalf JWS. Returns claims or raises ValueError(reason)."""
    try:
        h64, p64, s64 = token.split(".")
        header = json.loads(b64u_dec(h64))
        claims = json.loads(b64u_dec(p64))
        sig = b64u_dec(s64)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("credential_malformed") from exc
    if header.get("alg") != "ES256" or len(sig) != 64:
        raise ValueError("credential_bad_alg")
    ok = KMS.verify(KeyId=KEY_ID, Message=f"{h64}.{p64}".encode(), MessageType="RAW",
                    Signature=raw_to_der(sig), SigningAlgorithm="ECDSA_SHA_256")["SignatureValid"]
    if not ok:
        raise ValueError("credential_bad_signature")
    now = int(time.time())
    if claims.get("iss") != AOB_ISS or claims.get("aud") != AOB_AUD:
        raise ValueError("credential_wrong_issuer_or_audience")
    if not (claims.get("iat", 0) - 30 <= now <= claims.get("exp", 0) + 30) or claims["exp"] - claims["iat"] > 300:
        raise ValueError("credential_expired")
    if claims.get("scope") != "member-records:invoke":
        raise ValueError("credential_wrong_scope")
    if claims.get("tool", "").split("___")[-1] != tool:
        raise ValueError("credential_wrong_tool")
    if claims.get("args_sha256") != canonical_args(args):
        raise ValueError("credential_args_mismatch")
    try:
        EVENTS.put_item(Item={"pk": f"JTI#{claims['jti']}", "sk": "META", "ttl": now + 600},
                        ConditionExpression="attribute_not_exists(pk)")
    except DDB.meta.client.exceptions.ConditionalCheckFailedException as exc:
        raise ValueError("credential_replayed") from exc
    return claims


def jwks_client():
    global _jwks
    if _jwks is None:
        _jwks = jwt.PyJWKClient(f"{STAFF_ISSUER}/.well-known/jwks.json")
    return _jwks


def verify_cognito(token, expect_use, client_id):
    key = jwks_client().get_signing_key_from_jwt(token).key
    opts = {"verify_aud": expect_use == "id"}
    claims = jwt.decode(token, key, algorithms=["RS256"], issuer=STAFF_ISSUER,
                        audience=client_id if expect_use == "id" else None, options=opts)
    if claims.get("token_use") != expect_use:
        raise ValueError(f"token_use_not_{expect_use}")
    if expect_use == "access" and claims.get("client_id") != client_id:
        raise ValueError("token_wrong_client")
    return claims


def identity(tool, args, m):
    """Return (subject dict or None, deny_reason or None)."""
    cred = args.get("_credential")
    if m == "service":
        return None, None
    if m not in ("impersonation", "act_on_behalf"):
        return None, "mode_unreadable"
    if not cred:
        return None, "credential_missing"
    try:
        if m == "impersonation":
            c = verify_cognito(cred, "access", CONSOLE_CLIENT_ID)
            return {"sub": c["sub"], "name": c.get("username"), "roles": c.get("cognito:groups", []), "actor": None, "jti": c.get("jti")}, None
        c = verify_aob(cred, tool, args)
        return {"sub": c["sub"], "name": c.get("name"), "roles": c.get("roles", []), "actor": (c.get("act") or {}).get("sub"), "jti": c["jti"]}, None
    except Exception as exc:  # noqa: BLE001 - fail closed
        return None, str(exc) if isinstance(exc, ValueError) else f"credential_invalid:{type(exc).__name__}"


def member_items(member_id):
    return DATA.query(KeyConditionExpression=Key("pk").eq(f"MEMBER#{member_id}"))["Items"]


def dec(v):
    return int(v) if hasattr(v, "as_integer_ratio") and not isinstance(v, bool) else v


def run_tool(tool, args, subject):
    mid = str(args.get("member_id", ""))
    items = member_items(mid)
    if not items:
        return {"status": "NOT_FOUND", "member_id": mid, "reason": "unknown member"}
    by = {i["sk"]: i for i in items}
    if tool == "get_member_profile":
        p = by["PROFILE"]
        return {"member_id": mid, "name": p["name"], "email": p["email"], "phone": p["phone"], "branch": p["branch"], "ssn": p["ssn"]}
    if tool == "get_member_notes":
        notes = [i["text"] for k, i in sorted(by.items()) if k.startswith("NOTE#")]
        return {"member_id": mid, "note_count": len(notes), "notes_text": " ".join(notes)}
    if tool == "get_member_accounts":
        accts = [i for k, i in sorted(by.items()) if k.startswith("ACCOUNT#")]
        out = {"member_id": mid, "checking_ref": "", "savings_ref": "", "accounts": []}
        for a in accts:
            out[f"{a['type']}_ref"] = a["account_ref"]
            out["accounts"].append({"account_ref": a["account_ref"], "type": a["type"], "masked_number": a["masked_number"],
                                    "full_number": a["full_number"], "balance": dec(a["balance"])})
        return out
    if tool == "get_loan_file":
        ln = next((i for k, i in by.items() if k.startswith("LOAN#")), None)
        if ln is None:
            return {"status": "NOT_FOUND", "member_id": mid, "reason": "no loan on file"}
        return {"member_id": mid, "loan_ref": ln["loan_ref"], "product": ln["product"], "principal": dec(ln["principal"]),
                "apr": str(ln["apr"]), "status": ln["status"], "underwriting_note": ln["underwriting_note"]}
    if tool == "initiate_transfer":
        src = str(args.get("source_ref", ""))
        if f"ACCOUNT#{src}" not in by:
            return {"status": "REJECTED", "member_id": mid, "reason": "source account does not belong to member"}
        tid = f"tr_{uuid.uuid4().hex[:8]}"
        EVENTS.put_item(Item={"pk": f"TRANSFER#{tid}", "sk": "META", "member_id": mid, "source_ref": src,
                              "destination_ref": str(args.get("destination_ref", "")), "amount": int(args.get("amount", 0)),
                              "subject_sub": (subject or {}).get("sub", "none"), "ts": int(time.time()), "ttl": int(time.time()) + 86400})
        return {"status": "EXECUTED", "transfer_id": tid, "member_id": mid, "destination_ref": str(args.get("destination_ref", "")), "amount": int(args.get("amount", 0))}
    if tool == "record_transfer_approval":
        if not subject:
            return {"approved": False, "status": "DENIED", "reason": "requester identity unknown (service mode)"}
        try:
            c = verify_cognito(str(args.get("approver_id_token", "")), "id", APPROVER_CLIENT_ID)
        except Exception as exc:  # noqa: BLE001
            return {"approved": False, "status": "DENIED", "reason": f"approver token invalid: {type(exc).__name__}"}
        if "supervisor" not in c.get("cognito:groups", []):
            return {"approved": False, "status": "DENIED", "reason": "approver is not a supervisor"}
        if c["sub"] == subject["sub"]:
            return {"approved": False, "status": "DENIED", "reason": "approver must differ from requester"}
        if time.time() - c.get("auth_time", 0) > 300:
            return {"approved": False, "status": "DENIED", "reason": "approver sign-in older than 5 minutes"}
        jti = c.get("jti") or c.get("origin_jti") or c.get("event_id")
        try:
            EVENTS.put_item(Item={"pk": f"JTI#{jti}", "sk": "META", "ttl": int(time.time()) + 600}, ConditionExpression="attribute_not_exists(pk)")
        except DDB.meta.client.exceptions.ConditionalCheckFailedException:
            return {"approved": False, "status": "DENIED", "reason": "approver token already used"}
        aid = f"ap_{uuid.uuid4().hex[:8]}"
        EVENTS.put_item(Item={"pk": f"APPROVAL#{aid}", "sk": "META", "member_id": mid, "destination_ref": str(args.get("destination_ref", "")),
                              "amount": int(args.get("amount", 0)), "requester_sub": subject["sub"], "approver_sub": c["sub"],
                              "ts": int(time.time()), "ttl": int(time.time()) + 86400})
        return {"approved": True, "approver_role": "supervisor", "approval_id": aid, "member_id": mid,
                "destination_ref": str(args.get("destination_ref", "")), "amount": int(args.get("amount", 0))}
    return {"status": "UNKNOWN_TOOL", "reason": tool}


def handler(event, context):
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    full_name = custom.get("bedrockAgentCoreToolName", "")
    tool = full_name.split("___")[-1]
    args = dict(event or {})
    m = mode()
    if os.environ.get("LOG_SHAPE") == "1":  # shape log: key names only, never values
        log(tool=tool, event_keys=sorted(args.keys()), context_keys=sorted(custom.keys()), mode=m)
    subject, deny = identity(tool, args, m)
    if deny:
        decision, reason, out = "DENIED", deny, {"status": "DENIED", "reason": f"credential check failed: {deny}"}
    elif subject is not None and tool not in set().union(*(ENTITLEMENTS.get(r, set()) for r in subject["roles"])):
        need = "lending" if tool == "get_loan_file" else "member_services"
        decision, reason, out = "DENIED", f"no_{need}_entitlement", {"status": "DENIED", "reason": f"no {need} entitlement for {subject.get('name')}"}
    else:
        out = run_tool(tool, args, subject)
        decision, reason = ("DENIED", "approval_refused") if out.get("status") == "DENIED" else ("ALLOWED", "ok")
    now = int(time.time())
    audit = {"pk": f"AUDIT#{time.strftime('%Y-%m-%d', time.gmtime(now))}", "sk": f"{now * 1000}#{uuid.uuid4().hex[:8]}",
             "request_id": context.aws_request_id, "mode": m, "subject_sub": (subject or {}).get("sub", "none"),
             "actor": (subject or {}).get("actor") or "none", "tool": tool, "member_id": str(args.get("member_id", "")),
             "decision": decision, "reason": reason, "credential_jti": (subject or {}).get("jti") or "none", "ttl": now + 7 * 86400}
    EVENTS.put_item(Item=audit)
    log(ts=now, request_id=context.aws_request_id, tool=tool, decision=decision, reason_code=reason, member_id=audit["member_id"],
        subject_sub=audit["subject_sub"], actor=audit["actor"], credential_jti=audit["credential_jti"], mode=m)
    return out
