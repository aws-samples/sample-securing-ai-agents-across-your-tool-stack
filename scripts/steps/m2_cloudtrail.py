"""M2 CloudTrail view: the token vault's record of partner token use, from CloudTrail event history:
GetResourceOauth2Token and CompleteResourceTokenAuth calls in the last N hours (default 24), newest first.

  python scripts/steps/m2_cloudtrail.py [hours]

Prints time, event name, credential provider, OAuth flow, forceAuthentication (true for a Disconnect) and outcome. It
never prints the request's workload identity token or the response's access token fields. CloudTrail names no staff user
in plain fields (the user is inside the workload identity token), so this is the vault's record for the partner
provider, not a per-user view. Delivery lags by several minutes, so the warm-up consent's screenshot is the prepared
view and this is the live bonus. READY when at least one event is found."""
import json
import sys
from datetime import datetime, timedelta, timezone

from steplib import gk, ready

HOURS = float(sys.argv[1]) if len(sys.argv) > 1 else 24.0
NAMES = ("GetResourceOauth2Token", "CompleteResourceTokenAuth")


def events():
    ct = gk.SESSION.client("cloudtrail")
    start = datetime.now(timezone.utc) - timedelta(hours=HOURS)
    rows = []
    for name in NAMES:
        for page in ct.get_paginator("lookup_events").paginate(LookupAttributes=[{"AttributeKey": "EventName", "AttributeValue": name}],
                                                              StartTime=start, PaginationConfig={"MaxItems": 50}):
            for e in page["Events"]:
                d = json.loads(e.get("CloudTrailEvent") or "{}")
                rp = d.get("requestParameters") or {}
                rows.append((e["EventTime"], name, str(rp.get("resourceCredentialProviderName", "")), str(rp.get("oauth2Flow", "")),
                             bool(rp.get("forceAuthentication")), d.get("errorCode") or "ok"))
    return sorted(rows, reverse=True)


def found():
    rows = events()
    for t, n, prov, flow, force, outcome in rows[:10]:
        print(f"  {t:%Y-%m-%d %H:%M:%S} UTC  {n:26} provider {prov:24} {flow:16} force={str(force):5} {outcome}")
    return f"{len(rows)} vault events in the last {HOURS:g} h" if rows else None


ready("M2 CloudTrail vault events", found, every=10)
