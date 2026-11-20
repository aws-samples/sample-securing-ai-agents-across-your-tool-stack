"""M5c: publish the explicit referral R-2003 and turn the RESPONSE interceptor's prompt guard on.
READY when Maya's referral list comes back withheld by the interceptor (HTTP 403, -32002, "Output blocked by response
interceptor") and Leo's clean referrals still come back.

The check runs in the RESPONSE interceptor with Bedrock ApplyGuardrail, not as a guardrail policy on the policy engine,
so the demo engine carries no PromptAttack policy."""
import sys

from steplib import PREFIX, gk, post_row, ready

if any("PromptAttack" in n for n in gk.active_policies()):
    sys.exit("NOT READY: the active engine carries a PromptAttack policy; the demo engine must not.")
gk.invoke_reset({"action": "referral_status", "owner": "maya", "referral_id": "R-2003", "status": "open"})
gk.ssm_put(f"{PREFIX}/prompt-guard", "on")
maya, leo = gk.access("maya"), gk.access("leo")


def call(token):
    m = gk.Mcp(token)
    msg = m.call("Partner___partner_list_referrals", {})
    return m.last_status, (msg or {}).get("error") or {}


def withheld():
    st, err = call(maya)
    if st == 403 and err.get("code") == -32002 and "response interceptor" in err.get("message", ""):
        st_leo, err_leo = call(leo)
        if st_leo == 200 and not err_leo:
            return "Maya's referrals withheld by the interceptor prompt guard (HTTP 403, -32002); Leo's returned"
    return None


ready("M5c prompt guard", withheld, every=3)


def show_r2003():
    """Put R-2003's text on the demo screen (step terminal and console feed), read live from the referrals table, so the
    presenter can point at the injected instruction before Maya's prompt is withheld."""
    item = gk.SESSION.resource("dynamodb").Table("gk-partner-referrals").get_item(
        Key={"pk": "OWNER#maya", "sk": "REF#R-2003"}).get("Item") or {}
    title, body = item.get("title", "?"), item.get("body", "?")
    print("\nThe partner just published R-2003 to Maya's queue:")
    print(f"  {title}")
    print(f"  \"{body}\"")
    post_row("partner", "R-2003 published", "STEP", "partner system", f"{title}: \"{body}\"")


show_r2003()
