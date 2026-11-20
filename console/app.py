"""Demo Console BFF: FastAPI on 127.0.0.1 only, one HTML page, server-sent events.

Security:
- Bound to 127.0.0.1 by console/__main__.py (a literal, no option to change it). Every request must carry
  `Host: 127.0.0.1:<port>`; a request with an `Origin` must come from `http://127.0.0.1:<port>`, and every state-changing
  request must carry that Origin (blocks DNS rebinding and cross-site requests). Step scripts, the runner, reset.py and
  the pre-flight do not use HTTP: they talk to the console's Unix socket (console/channel.py: a 0700 directory, a 0600
  socket and a peer-uid check; no token). No socket message can start a step.
- Run buttons: POST /api/steps/{id}/run needs the presenter session and the Origin check; the id must be
  a loaded card (console/stepcard.py), and only the registry's id and the console's own run id reach the process.
- Console sign-in is a form posted here: AdminInitiateAuth for a user in the staff pool group `presenter`, made with the
  `approver` app client, whose tokens neither the Runtime nor the Gateway accepts; the tokens are dropped after the group
  check. The browser gets one opaque session cookie: HttpOnly, SameSite=Strict, 8 hours.
- Persona tokens stay in the BFF (console/core.py). Responses carry no token, secret or raw JWT, and nothing is logged
  per request (uvicorn's access log is off).
"""
import asyncio
import base64
import contextlib
import glob
import json
import os
import re
import secrets
import time

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse

from . import channel, core, prompts, stepcard

HOST = "127.0.0.1"
SESSION_COOKIE = "gk_console"
SESSION_SECONDS = 8 * 3600
STATUS_EVERY_S = 5
HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
USERNAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
SOURCE = re.compile(r"^[A-Za-z0-9_.-]{1,16}$")


def source_files():
    """The files a running console has loaded: console/*.py and the page. /api/health's version is their newest mtime."""
    return sorted(glob.glob(os.path.join(HERE, "*.py"))) + [os.path.join(STATIC, "index.html")]


def newest_mtime(paths=None):
    return max(os.path.getmtime(p) for p in (paths or source_files()) if os.path.exists(p))


def moment_request(body):
    """(stage or None, source) from a POST /api/moment body: {"stage": <stage id>|null} or {"step": <step code>}, plus an
    optional "source". Anything else raises ValueError (HTTP 400)."""
    if not isinstance(body, dict) or set(body) - {"stage", "step", "source"} or ("stage" in body) == ("step" in body):
        raise ValueError('send {"stage": "1".."6"|"5a"|"5b"|"5c"|null} or {"step": "M3"}, plus an optional "source"')
    source = body.get("source", "api")
    if not isinstance(source, str) or not SOURCE.match(source):
        raise ValueError("source must be 1 to 16 letters, digits, '.', '_' or '-'")
    if "step" in body:
        stage = core.stage_for(body["step"])
        if not stage:
            raise ValueError(f"unknown step code (one of {', '.join(core.STEP_STAGE)})")
        return stage, source
    stage = body["stage"]
    if stage is not None and (not isinstance(stage, str) or stage not in core.STAGE_IDS):
        raise ValueError(f"stage must be one of {', '.join(core.STAGE_IDS)} or null")
    return stage, source


def banner_request(body):
    """(text, state) from a POST /api/banner body: {"text": "...", "state": "running"|"done"|"failed"}; else ValueError."""
    if not isinstance(body, dict) or set(body) - {"text", "state"} or not isinstance(body.get("text"), str) \
            or not body["text"].strip() or body.get("state") not in core.BANNER_STATES:
        raise ValueError('send {"text": "...", "state": "running"|"done"|"failed"}')
    return body["text"], body["state"]


def check_presenter(username, password):
    """True if the staff pool accepts the password and the user is in group `presenter`. Tokens are not kept."""
    gk = core.gk
    if not USERNAME.match(username or "") or not password:
        return False
    c = gk.SESSION.client("cognito-idp")
    try:
        r = c.admin_initiate_auth(UserPoolId=gk.out()["StaffPoolId"], ClientId=gk.out()["ApproverClientId"], AuthFlow="ADMIN_USER_PASSWORD_AUTH",
                                  AuthParameters={"USERNAME": username, "PASSWORD": password})
    except (c.exceptions.NotAuthorizedException, c.exceptions.UserNotFoundException, c.exceptions.InvalidParameterException):
        return False
    res = r.get("AuthenticationResult") or {}
    if r.get("ChallengeName") or not res.get("IdToken"):
        return False
    payload = res["IdToken"].split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    res.clear()
    return "presenter" in (claims.get("cognito:groups") or [])


def page(name, nonce, **subs):
    with open(os.path.join(STATIC, name), encoding="utf-8") as f:
        html = f.read().replace("__NONCE__", nonce)
    for k, v in subs.items():
        html = html.replace(f"__{k}__", v)
    return html


def create_app(port, sock=None):
    allowed_host = f"{HOST}:{port}"
    allowed_origin = f"http://{HOST}:{port}"
    sock = sock or channel.sock_path()
    sessions = {}
    state = {"registry": stepcard.load()}
    started = {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "version": int(newest_mtime())}

    def health():
        st = console().status
        reg = state["registry"]
        return {"started_utc": started["utc"], "pid": os.getpid(), "port": port,
                "status": {"state": st.get("state"), "error": st.get("error"), "read_at": st.get("read_at")},
                "moment": dict(console().moment), "version": started["version"],
                "version_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started["version"])),
                "cards": len(reg.cards), "card_errors": list(reg.errors)}

    def on_message(body):
        """One script-channel message (already uid-checked by channel.serve) -> the reply's fields."""
        body = dict(body)
        kind, c, reg = body.pop("type"), console(), state["registry"]
        if kind == "row":
            r = channel.check_row(body)
            row = c.feed.add_script(r["persona"], r["tool"], r["decision"], r["control"], core.mask_text(r["reason"]), source=r["source"])
            return {"id": row["id"]}
        if kind == "moment":
            return {"moment": c.set_moment(*moment_request(body))}
        if kind == "banner":
            return {"banner": c.set_banner(*banner_request(body))}
        if kind == "card_start":
            cid, rid, t0 = channel.check_card_start(body, reg)
            return c.card_start(reg.get(cid), rid, t0)
        if kind == "card_lines":
            return c.card_lines(*channel.check_card_lines(body, reg))
        if kind == "card_end":
            return c.card_end(*channel.check_card_end(body, reg))
        if kind == "card_clear":
            channel._fields(body, ())
            return c.card_clear()
        channel._fields(body, ())  # health
        return health()

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        state["console"] = core.Console(asyncio.get_running_loop())
        channel.prepare(sock)  # RuntimeError (the console does not start) when another console answers there
        server = await channel.serve(sock, on_message)
        ino = os.stat(sock).st_ino

        async def status_loop():
            while True:
                await asyncio.to_thread(state["console"].read_status)
                await asyncio.sleep(STATUS_EVERY_S)
        task = asyncio.create_task(status_loop())
        try:
            yield
        finally:
            task.cancel()
            server.close()
            with contextlib.suppress(OSError):
                if os.stat(sock).st_ino == ino:  # still our socket file, not a newer console's
                    os.unlink(sock)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def console():
        return state["console"]

    def session_user(request):
        sid = request.cookies.get(SESSION_COOKIE)
        s = sessions.get(sid) if sid else None
        if not s or s["exp"] < time.time():
            sessions.pop(sid, None)
            return None
        return s["user"]

    @app.middleware("http")
    async def guard(request, call_next):
        if request.headers.get("host") != allowed_host:
            return Response("host not allowed", status_code=403)
        origin = request.headers.get("origin")
        if origin is not None and origin != allowed_origin:
            return Response("origin not allowed", status_code=403)
        if request.method not in ("GET", "HEAD") and origin is None:
            return Response("origin required", status_code=403)
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        # same-origin, not no-referrer: with no-referrer a browser sends "Origin: null" on a form POST, which the check refuses
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    def html(content, nonce, status=200):
        csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; img-src 'self'; "
               "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        return HTMLResponse(content, status_code=status, headers={"Content-Security-Policy": csp})

    def need_user(request):
        user = session_user(request)
        if not user:
            raise PermissionError
        return user

    @app.exception_handler(PermissionError)
    async def _denied(_request, _exc):
        return JSONResponse({"error": "sign in first"}, status_code=401)

    @app.exception_handler(RuntimeError)
    async def _conflict(_request, exc):
        return JSONResponse({"error": str(exc)[:200]}, status_code=409)

    @app.exception_handler(ValueError)
    async def _bad(_request, exc):
        return JSONResponse({"error": str(exc)[:200]}, status_code=400)

    @app.get("/")
    async def index(request: Request):
        nonce = secrets.token_urlsafe(16)
        if not session_user(request):
            return html(page("login.html", nonce, MESSAGE=""), nonce)
        return html(page("index.html", nonce), nonce)

    @app.post("/login")
    async def login(request: Request):
        form = await request.form()
        user, pw = str(form.get("username", "")).strip(), str(form.get("password", ""))
        ok = await asyncio.to_thread(check_presenter, user, pw)
        del pw
        if not ok:
            await asyncio.sleep(1)
            nonce = secrets.token_urlsafe(16)
            return html(page("login.html", nonce, MESSAGE="Sign-in failed: presenter accounts only."), nonce, status=401)
        sid = secrets.token_urlsafe(32)
        sessions[sid] = {"user": user, "exp": time.time() + SESSION_SECONDS}
        r = RedirectResponse("/", status_code=303)
        r.set_cookie(SESSION_COOKIE, sid, max_age=SESSION_SECONDS, httponly=True, samesite="strict", path="/")
        return r

    @app.post("/logout")
    async def logout(request: Request):
        sessions.pop(request.cookies.get(SESSION_COOKIE), None)
        r = RedirectResponse("/", status_code=303)
        r.delete_cookie(SESSION_COOKIE, path="/")
        return r

    @app.get("/api/me")
    async def me(request: Request):
        return {"user": need_user(request)}

    @app.get("/api/snapshot")
    async def snapshot(request: Request):
        need_user(request)
        return core.jsonable(console().snapshot())

    @app.get("/api/prompts")
    async def get_prompts(request: Request):
        need_user(request)
        return prompts.PROMPTS

    @app.post("/api/panes/{key}/send")
    async def send(key: str, request: Request):
        need_user(request)
        body = await request.json()
        if key not in console().panes:
            raise ValueError("unknown pane")
        console().send(key, str(body.get("prompt", "")), step=body.get("step"))  # step: the chip's code, when the chip was sent
        return JSONResponse({"started": True}, status_code=202)

    @app.post("/api/panes/{key}/new")
    async def new(key: str, request: Request):
        need_user(request)
        if key not in console().panes:
            raise ValueError("unknown pane")
        console().new_conversation(key)
        return {"ok": True}

    @app.post("/api/panes/{key}/disconnect")
    async def disconnect(key: str, request: Request):
        need_user(request)
        if key != "maya":
            raise ValueError("Disconnect is Maya's (M2)")
        return await asyncio.to_thread(console().disconnect, key)

    @app.post("/api/panes/{key}/direct")
    async def direct(key: str, request: Request):
        need_user(request)
        body = await request.json()
        if key not in console().panes:
            raise ValueError("unknown pane")
        return await asyncio.to_thread(console().direct, key, str(body.get("moment", "")))

    @app.post("/api/approval/{action}")
    async def approval(action: str, request: Request):
        need_user(request)
        c = console()
        if action == "request":
            return c.request_approval()
        if action == "approve":
            return await asyncio.to_thread(c.approve)
        if action == "cancel":
            c.cancel_approval()
            return {"ok": True}
        raise ValueError("unknown action")

    @app.post("/api/moment")
    async def moment(request: Request):
        need_user(request)
        stage, source = moment_request(await request.json())
        return console().set_moment(stage, source)

    # ---------------- step card and Run buttons ----------------
    @app.get("/api/steps")
    async def steps(request: Request):
        need_user(request)
        reg = state["registry"]
        return {"cards": reg.cards, "errors": reg.errors}

    @app.post("/api/steps/stop")
    async def stop_step(request: Request):
        need_user(request)
        return console().stop_run()

    @app.post("/api/steps/{card_id}/run")
    async def run_step(card_id: str, request: Request):
        need_user(request)
        card = state["registry"].get(card_id)
        if not card:
            return JSONResponse({"error": "unknown step"}, status_code=404)
        rid = await asyncio.to_thread(console().start_run, card, sock, os.path.dirname(sock))
        return JSONResponse({"run_id": rid}, status_code=202)

    @app.post("/api/card/close")
    async def close_card(request: Request):
        need_user(request)
        return console().card_close()

    async def sse(channel_name, request):
        q = console().hub.subscribe(channel_name)

        async def gen():
            try:
                yield "retry: 2000\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15)
                        yield f"data: {json.dumps(core.jsonable(ev))}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                console().hub.unsubscribe(channel_name, q)
        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.get("/api/panes/{key}/stream")
    async def pane_stream(key: str, request: Request):
        need_user(request)
        if key not in console().panes:
            raise ValueError("unknown pane")
        return await sse(key, request)

    @app.get("/api/feed/stream")
    async def feed_stream(request: Request):
        need_user(request)
        return await sse("feed", request)

    return app
