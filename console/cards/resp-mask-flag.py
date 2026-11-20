# SOURCE: lambdas/response_interceptor/handler.py L173-175
if param(MASK_PARAM, "on") == "on":
    sc = res.get("structuredContent")
    changed = mask(sc) if isinstance(sc, dict) else []
