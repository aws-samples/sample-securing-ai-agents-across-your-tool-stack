"""Partner referral API Lambda (gk-partner-referrals), behind an HTTP API JWT authorizer.

Identity comes only from the validated partner-pool token claims that API Gateway passes in
requestContext.authorizer.jwt.claims. Query strings, headers and body are ignored. Only referrals with status
"open" are returned (a "held" referral, R-2003, waits for the M5c step).
Logs only the request ID, owner and count.
"""
import json
import os

import boto3
from boto3.dynamodb.conditions import Key

TABLE = boto3.resource("dynamodb").Table(os.environ["REFERRALS_TABLE"])


def handler(event, context):
    claims = (((event.get("requestContext") or {}).get("authorizer") or {}).get("jwt") or {}).get("claims") or {}
    owner = claims.get("username")
    scopes = str(claims.get("scope", "")).split()
    if not owner or "referrals/read" not in scopes:
        print(json.dumps({"request_id": context.aws_request_id, "owner": None, "decision": "DENIED"}))
        return {"statusCode": 403, "headers": {"content-type": "application/json"}, "body": json.dumps({"message": "forbidden"})}
    items = TABLE.query(KeyConditionExpression=Key("pk").eq(f"OWNER#{owner}"))["Items"]
    refs = [{"referral_id": i["referral_id"], "member_id": i["member_id"], "title": i["title"], "body": i["body"],
             "status": i["status"], "created": i["created"]} for i in sorted(items, key=lambda x: x["sk"]) if i.get("status") == "open"]
    out = {"owner": owner, "count": len(refs), "referrals": refs,
           "digest": " | ".join(f"{r['referral_id']} {r['title']}: {r['body']}" for r in refs)}
    print(json.dumps({"request_id": context.aws_request_id, "owner": owner, "count": len(refs), "decision": "ALLOWED"}))
    return {"statusCode": 200, "headers": {"content-type": "application/json"}, "body": json.dumps(out)}
