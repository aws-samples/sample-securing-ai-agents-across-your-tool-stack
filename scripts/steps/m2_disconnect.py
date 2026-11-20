"""M2 Disconnect: remove Maya's stored partner token with one Gateway tools/call carrying
forceAuthentication, then check that her next normal call asks for consent again (-32042 with the portal URL).
Leo's grant is untouched. READY when both hold. Reconnect Maya with the portal (or scripts/consent_login.py maya)."""
import urllib.parse

from steplib import gk, ready

r = gk.invoke_reset({"action": "revoke", "user": "maya"})
portal = urllib.parse.urlparse(gk.out()["PortalUrl"]).netloc


def disconnected():
    nxt = gk.invoke_reset({"action": "probe", "user": "maya"})
    leo = gk.invoke_reset({"action": "probe", "user": "leo"})
    leo_ok = not leo.get("error_code") and '"owner":"leo"' in (leo.get("text_prefix") or "").replace(" ", "")
    if nxt.get("error_code") == -32042 and portal in (nxt.get("elicitation_hosts") or []) and leo_ok:
        return f"Maya's grant removed (revoke call HTTP {r['revoke_call'].get('http_status')}); her next call asks for consent on the portal; Leo still connected"
    return None


ready("M2 Maya disconnected", disconnected, every=3)
