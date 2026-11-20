"""M4 step 1: masking on (response interceptor, 5-second cache). READY when Sam's profile comes back masked."""
from steplib import PREFIX, gk, ready

gk.ssm_put(f"{PREFIX}/masking", "on")
maya = gk.access("maya")


def masked():
    r = gk.call(gk.Mcp(maya), "get_member_profile", {"member_id": "M-1001"})
    ssn = (r["sc"] or {}).get("ssn", "")
    return f"profile ssn {ssn}" if ssn.startswith("***-**-") else None


ready("masking on", masked, every=3)
