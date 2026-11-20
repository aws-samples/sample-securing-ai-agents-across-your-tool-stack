"""M3: deputy mode.  python scripts/steps/m3_mode.py <service|impersonation|act_on_behalf>"""
import sys
import time

from steplib import PREFIX, gk, ready

mode = sys.argv[1]
if mode not in ("service", "impersonation", "act_on_behalf"):
    sys.exit("mode must be service, impersonation or act_on_behalf")
gk.ssm_put(f"{PREFIX}/deputy-mode", mode)
t0 = time.time()
ready(f"deputy mode {mode}", lambda: f"{PREFIX}/deputy-mode={mode}, caches refreshed" if gk.ssm_get(f"{PREFIX}/deputy-mode") == mode and time.time() - t0 > 6 else None)
