"""M6: the per-staff rate limit to 2 per second, then a 35-second countdown (changes take up to 30 s).
Rate limits fail open on service timeouts and unresolvable dimensions: a cost and availability control, not authorization."""
import time

from steplib import gk, ready

gk.rate_limit(2)
for left in range(35, 0, -5):
    print(f"limit propagating: {left} s")
    time.sleep(5)
ready("rate limit 2 per second", lambda: "per-staff-member-records = 2 per second" if gk.rate_limit() == 2 else None)
