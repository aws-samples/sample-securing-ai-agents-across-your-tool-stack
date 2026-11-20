"""M1 opener: calls without a valid staff token are refused at the Gateway's front door.

Three callers, each sending tools/list and a tools/call: no token; a real, signed staff-pool token from the `approver`
client, which the Gateway does not allow; and a token claiming another issuer (a different pool) with a signature
nobody holds. The partner pool cannot mint a token outside the authorization code flow, so the foreign-pool case uses
a claimed issuer. READY when every call gets HTTP 401 or 403 and no audit or trail row was written meanwhile."""
import base64
import json
import time

from boto3.dynamodb.conditions import Key

from steplib import gk, ready


def b64(d):
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


def foreign_token():
    now = int(time.time())
    claims = {"iss": f"https://cognito-idp.{gk.REGION}.amazonaws.com/{gk.REGION}_NotOurPool1", "client_id": "unknown-client",
              "token_use": "access", "sub": "00000000-0000-0000-0000-000000000000", "iat": now, "exp": now + 600}
    return f"{b64({'alg': 'RS256', 'kid': 'not-a-real-key'})}.{b64(claims)}.{b64({'sig': 'none'})}"


CALLERS = {"no token": None, "approver client (not allowed)": gk.staff_auth("dana", "approver")["AccessToken"], "foreign issuer": foreign_token()}
t0 = int(time.time() * 1000)


def probe():
    res = {}
    for label, tok in CALLERS.items():
        m = gk.Mcp(tok, init=False)
        m.rpc("tools/list")
        a = m.last_status
        m.call("MemberRecords___get_member_profile", {"member_id": "M-1001"})
        res[label] = (a, m.last_status)
    return res


def refused():
    res = probe()
    if not all(s in (401, 403) for pair in res.values() for s in pair):
        return None
    today = time.strftime("%Y-%m-%d", time.gmtime())
    rows = gk.SESSION.resource("dynamodb").Table("gk-events").query(KeyConditionExpression=Key("pk").eq(f"AUDIT#{today}") & Key("sk").gte(str(t0)))["Items"]
    rows = [r for r in rows if r.get("tool") == "get_member_profile"]
    return None if rows else "; ".join(f"{k}: tools/list {a}, tools/call {c}" for k, (a, c) in res.items()) + "; no audit row"


ready("M1 unauthenticated calls refused", refused, every=3)
