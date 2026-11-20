# SOURCE: lambdas/response_interceptor/handler.py L162-164
if method == "tools/call" and tool == PARTNER_TOOL \
        and ... param(GUARD_PARAM, "on") == "on":
    withhold, detail = prompt_guard(res)
