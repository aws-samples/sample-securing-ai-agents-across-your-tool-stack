"""Drive the consent portal for one persona in a fresh headless browser context.

  .venv/bin/python scripts/consent_login.py <maya|leo> [--screens]
  .venv/bin/python scripts/consent_login.py --all [--force]     check Maya's and Leo's grants, re-consent what needs it

Steps: open the portal, sign in with the STAFF pool account, choose Connect on the partner
connection, sign in with the PARTNER pool account, land back on the portal.
Passwords are read from Secrets Manager inside this process and never printed. Printed URLs are
reduced to host and path (query strings can carry codes and state).
"""
import glob
import json
import os
import sys
import time
import urllib.parse

from playwright.sync_api import sync_playwright

import gklib as gk

SHOTS = os.path.join(gk.LOGS, "consent")
GRANTS = os.path.join(gk.LOGS, "grants.json")  # re-consent times on this machine (git-ignored, like all of logs/)


def where(page):
    u = urllib.parse.urlparse(page.url)
    return f"{u.netloc}{u.path}"


def cognito_sign_in(page, username, secret, label, trail):
    page.wait_for_load_state("domcontentloaded")
    user = page.locator('input[name="username"]:visible').first
    user.wait_for(timeout=30000)
    user.fill(username)
    page.locator('input[name="password"]:visible').first.fill(secret)
    trail.append(f"{label}: sign-in form at {where(page)}")
    btn = page.locator('input[name="signInSubmitButton"]:visible, button[type="submit"]:visible').first
    btn.click()
    page.wait_for_load_state("networkidle", timeout=45000)
    trail.append(f"{label}: after sign-in at {where(page)}")


def buttons(page):
    return [t.strip() for t in page.locator("button:visible, a:visible").all_inner_texts() if t.strip()][:25]


def launch(p):
    try:
        return p.chromium.launch(headless=True)
    except Exception:  # noqa: BLE001 - fall back to the cached headless shell
        exe = sorted(glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium_headless_shell-*/chrome-*/headless_shell")))[-1]
        return p.chromium.launch(headless=True, executable_path=exe)


def connect(user, screens=False):
    portal = gk.out()["PortalUrl"]
    trail = []
    os.makedirs(SHOTS, exist_ok=True)
    with sync_playwright() as p:
        browser = launch(p)
        ctx = browser.new_context()  # fresh context: no Cognito session cookies
        page = ctx.new_page()
        page.goto(portal, wait_until="networkidle", timeout=60000)
        trail.append(f"opened portal, now at {where(page)}")
        if "amazoncognito.com" in page.url:
            cognito_sign_in(page, user, gk.password("staff", user), "staff pool", trail)
        page.wait_for_timeout(2500)
        if screens:
            page.screenshot(path=f"{SHOTS}/{user}-1-portal.png", full_page=True)
        trail.append(f"portal page {where(page)} buttons={buttons(page)}")
        connect_btn = page.get_by_role("button", name="Connect").first
        if connect_btn.count() == 0:
            connect_btn = page.locator("text=Connect").first
        connect_btn.click()
        try:
            page.wait_for_url(lambda u: "amazoncognito.com" in u or "/connect/callback" in u, timeout=30000)
        except Exception:  # noqa: BLE001 - record where we ended up
            pass
        page.wait_for_load_state("networkidle", timeout=45000)
        trail.append(f"after Connect at {where(page)}")
        partner_signin = "amazoncognito.com" in page.url
        if partner_signin:
            if screens:
                page.screenshot(path=f"{SHOTS}/{user}-2-partner-signin.png", full_page=True)
            cognito_sign_in(page, user, gk.password("partner", user), "partner pool", trail)
            try:
                page.wait_for_url(lambda u: "consent-portal" in u, timeout=45000)
            except Exception:  # noqa: BLE001
                pass
        page.wait_for_timeout(4000)
        if screens:
            page.screenshot(path=f"{SHOTS}/{user}-3-back.png", full_page=True)
        body = page.locator("body").inner_text()[:600]
        trail.append(f"final page {where(page)} buttons={buttons(page)}")
        browser.close()
    return {"user": user, "partner_signin_page_shown": partner_signin, "trail": trail,
            "final_text": " ".join(body.split())[:400], "ts": int(time.time())}


def last_refresh(user=None):
    """Recorded re-consents on this machine (logs/grants.json, written by ensure_grants): {user: {"utc", "epoch"}}."""
    try:
        rec = json.load(open(GRANTS))
    except (OSError, ValueError):
        rec = {}
    return rec.get(user) if user else rec


def ensure_grants(users=("maya", "leo"), force=False):
    """The harness's re-consent step: check each persona's partner grant through the Gateway and run the portal
    consent only where it is missing (or for every user with force). Partner grants last at most one
    day. Returns {user: {"before", "reconsented", "partner_signin_shown", "after"}}; prints no secrets.
    A re-consent that ends connected is recorded in logs/grants.json (scripts/prep.py prints it)."""
    from state import grant  # noqa: PLC0415 - state imports this module lazily as well
    out = {}
    for user in users:
        before = grant(user)
        row = {"before": before, "reconsented": False, "partner_signin_shown": None}
        if force or before != "connected":
            r = connect(user)
            row.update({"reconsented": True, "partner_signin_shown": r.get("partner_signin_page_shown")})
        row["after"] = grant(user)
        if row["reconsented"] and row["after"] == "connected":
            rec = last_refresh()
            rec[user] = {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "epoch": int(time.time())}
            os.makedirs(gk.LOGS, exist_ok=True)
            with open(GRANTS, "w") as f:
                json.dump(rec, f, indent=1)
        out[user] = row
    return out


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--all" in sys.argv or len(args) != 1:
        # .venv/bin/python scripts/consent_login.py --all [--force]      or      ... maya leo [--force]
        res = ensure_grants(tuple(args) or ("maya", "leo"), force="--force" in sys.argv)
        for u, row in res.items():
            print(f"{u:5s} before {row['before']:16s} reconsented {str(row['reconsented']):5s} after {row['after']}")
        sys.exit(0 if all(row["after"] == "connected" for row in res.values()) else 1)
    r = connect(args[0], "--screens" in sys.argv)
    print(gk.mask(r))
