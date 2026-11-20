# SOURCE: scripts/consent_login.py L75-83, L86
connect_btn.click()
# ...
if partner_signin:
    cognito_sign_in(page, user, gk.password("partner", user), ...)
