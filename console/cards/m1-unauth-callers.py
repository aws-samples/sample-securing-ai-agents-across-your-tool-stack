# SOURCE: scripts/steps/m1_unauth.py L27
CALLERS = {"no token": None, "approver client (not allowed)":
    gk.staff_auth("dana", "approver")["AccessToken"],
    "foreign issuer": foreign_token()}
