# SOURCE: lambdas/request_interceptor/handler.py L103-110
m = mode()
if m == "impersonation":
    # ...
elif m == "act_on_behalf":
    # ...
    args["_credential"] = mint(staff, tool, args, actor)
