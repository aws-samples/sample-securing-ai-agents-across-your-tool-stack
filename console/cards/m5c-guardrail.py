# SOURCE: scripts/steps/m5c_guardrail.py L13-14
gk.invoke_reset({"action": "referral_status", "owner": "maya",
                 "referral_id": "R-2003", "status": "open"})
gk.ssm_put(f"{PREFIX}/prompt-guard", "on")
