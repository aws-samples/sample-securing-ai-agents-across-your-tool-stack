#!/usr/bin/env python3
"""Copy each demo user's password from Secrets Manager into Cognito (permanent), then verify sign-in.

  .venv/bin/python scripts/sync_cognito_passwords.py

Use after editing the <stack>/<pool>/<user> secrets by hand: the stack copies them into Cognito only when it creates
the users at deploy time, so a changed secret is not seen by Cognito until this runs. Prints no password.
Exit 1 if any user could not be set or verified.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gklib as gk  # noqa: E402

USERS = [("staff", u) for u in ("maya", "leo", "dana", "presenter1", "presenter2")] + [("partner", u) for u in ("maya", "leo")]


def main():
    o = gk.out()
    pools = {"staff": o["StaffPoolId"], "partner": o["PartnerPoolId"]}
    cog = gk.SESSION.client("cognito-idp")
    bad = 0
    for pool, user in USERS:
        try:
            pw = gk.password(pool, user)
            cog.admin_set_user_password(UserPoolId=pools[pool], Username=user, Password=pw, Permanent=True)
            status = cog.admin_get_user(UserPoolId=pools[pool], Username=user)["UserStatus"]
            msg = f"OK (status {status})"
            if pool == "staff":  # a real sign-in with the stored secret, as the console and reset do
                cid = o.get("ApproverClientId") if user.startswith("presenter") else o.get("ConsoleClientId")
                r = cog.admin_initiate_auth(UserPoolId=pools[pool], ClientId=cid, AuthFlow="ADMIN_USER_PASSWORD_AUTH",
                                            AuthParameters={"USERNAME": user, "PASSWORD": pw})
                msg += ", sign-in OK" if (r.get("AuthenticationResult") or {}).get("IdToken") else f", sign-in got {r.get('ChallengeName')}"
                r.clear()
            del pw
        except Exception as e:  # report and carry on with the next user
            bad += 1
            msg = f"FAILED: {type(e).__name__}: {str(e)[:160]}"
        print(f"  {pool:8} {user:11} {msg}")
    print("all 7 set" if not bad else f"{bad} of 7 failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
