# SOURCE: scripts/consent_login.py L64-67
page.goto(portal, wait_until="networkidle", timeout=60000)
# ...
if "amazoncognito.com" in page.url:
    cognito_sign_in(page, user, gk.password("staff", user), ...)
