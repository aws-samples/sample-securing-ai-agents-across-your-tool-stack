"""Offline tests for the console's moment tracker, the script banner, /api/health, the step-script effects, the
client's no-op and Leo's renewal in prep.py: no AWS, no network beyond 127.0.0.1 inside the test process.

  .venv/bin/python -m pytest -q tests/test_console_v3.py

The console app runs under Starlette's TestClient with its status reader replaced by a stub, and with its script
socket (console/channel.py) in a fresh temporary directory: GK_CONSOLE_SOCK is set before console.client
is imported, and every test checks that the path is not logs/run/console.sock, so nothing here touches the presenter
console's socket.
"""
import json
import os
import re
import sys
import tempfile
import time

import pytest

DEMO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
REAL_SOCK = os.path.join(DEMO, "logs", "run", "console.sock")
os.environ.setdefault("GK_CONSOLE_SOCK", os.path.join(tempfile.mkdtemp(prefix="gk-"), "run", "console.sock"))
sys.path.insert(0, DEMO)
sys.path.insert(0, os.path.join(DEMO, "scripts", "steps"))

from starlette.testclient import TestClient  # noqa: E402

import console.app as capp  # noqa: E402
import console.client as client  # noqa: E402
import console.core as core  # noqa: E402

PORT = 18790
BASE = f"http://127.0.0.1:{PORT}"
ORIGIN = {"Origin": BASE}


@pytest.fixture()
def sock(monkeypatch):
    """A fresh socket path (short: a Unix socket path has at most 107 bytes), never the presenter console's."""
    path = os.path.join(tempfile.mkdtemp(prefix="gk-"), "run", "console.sock")
    monkeypatch.setattr(client, "SOCK", path)
    assert os.path.realpath(path) != os.path.realpath(REAL_SOCK)
    return path


@pytest.fixture(autouse=True)
def never_the_real_socket(monkeypatch):
    """Another test module may have imported console.client without GK_CONSOLE_SOCK: point it at a scratch path."""
    monkeypatch.setattr(client, "SOCK", os.path.join(tempfile.mkdtemp(prefix="gk-"), "none.sock"))
    yield
    assert os.path.realpath(client.SOCK) != os.path.realpath(REAL_SOCK)


@pytest.fixture()
def app(monkeypatch, sock):
    """(TestClient with a presenter session, the script socket's path, the Console). No AWS: the status read is a stub,
    and the agent turn is a no-op that ends at once. console.client talks to this console's socket."""
    def fake_status(self):
        self.status = {"state": "ok", "deputy": "service", "masking": "off", "prompt_guard": "off", "rate_per_second": 10000,
                       "policies": {}, "read_at": core.et_clock()}
        return self.status
    made, orig_init = [], core.Console.__init__

    def init(self, loop):
        orig_init(self, loop)
        made.append(self)
    monkeypatch.setattr(core.Console, "__init__", init)
    monkeypatch.setattr(core.Console, "read_status", fake_status)
    monkeypatch.setattr(core.Console, "_turn", lambda self, pane, prompt: setattr(pane, "busy", False))
    monkeypatch.setattr(capp, "check_presenter", lambda u, p: u == "presenter1" and p == "test-only")
    monkeypatch.setattr(core.gk, "mask", lambda t: str(t).replace("123456789012", "<acct>"))
    with TestClient(capp.create_app(PORT, sock=sock), base_url=BASE) as tc:
        assert oct(os.stat(sock).st_mode & 0o777) == "0o600" and oct(os.stat(os.path.dirname(sock)).st_mode & 0o777) == "0o700"
        r = tc.post("/login", data={"username": "presenter1", "password": "test-only"}, headers=ORIGIN, follow_redirects=False)
        assert r.status_code == 303
        until(lambda: made[0].status.get("state") == "ok")  # the first status read runs right after the start
        yield tc, sock, made[0]
    assert not os.path.exists(sock)  # the console removes its socket when it stops


def until(cond, timeout=3.0):
    t0 = time.time()
    while not cond():
        assert time.time() - t0 < timeout, "condition not met in time"
        time.sleep(0.02)


def idle(c):
    until(lambda: not any(p.busy for p in c.panes.values()))


def snapshot(tc):
    r = tc.get("/api/snapshot")
    assert r.status_code == 200
    return r.json()


# ---------------------------------------------------------------- step code to stage
def test_step_codes_map_to_the_on_stage_stages():
    assert core.STAGE_IDS == ("1", "2", "3", "4", "5a", "5b", "5c", "6")
    assert core.stage_for("R1.2") == core.stage_for("M1") == "1"
    assert core.stage_for("M2") == core.stage_for("M2-CT") == "2"
    assert [core.stage_for(s) for s in ("M3", "M4", "M5", "M5c", "M5b", "M6")] == ["3", "4", "5a", "5b", "5c", "6"]  # 5b/5c crossed
    assert core.stage_for("M7") is None and core.stage_for(None) is None and core.stage_for(3) is None


def test_every_chip_and_goto_target_has_a_stage():
    import goto  # noqa: PLC0415
    from console.prompts import PROMPTS  # noqa: PLC0415
    assert all(core.stage_for(p["step"]) for pane in PROMPTS.values() for p in pane)
    assert all(core.stage_for(t) for t in goto.TARGETS)


def test_the_page_draws_the_servers_stage_list():
    html = open(os.path.join(DEMO, "console", "static", "index.html"), encoding="utf-8").read()
    block = re.search(r"const STAGES = \[(.*?)\];", html, re.S).group(1)
    assert re.findall(r'\["([^"]+)", "([^"]+)"\]', block) == core.STAGES


def test_step_scripts_map_to_their_stage_and_label():
    import steplib  # noqa: PLC0415 - imported by a test: SCRIPT is None, so it posts nothing
    assert steplib.SCRIPT is None
    assert steplib.banner_for("m1_unauth.py") == ("1", "front door check")
    assert steplib.banner_for("m2_disconnect.py") == ("2", "disconnect Maya's partner account")
    assert steplib.banner_for("m2_cloudtrail.py", ["24"]) == ("2", "the vault's CloudTrail record")
    assert steplib.banner_for("m3_mode.py", ["act_on_behalf"]) == ("3", "deputy mode: act on behalf")
    assert steplib.banner_for("m4_mask.py") == ("4", "masking on")
    assert steplib.banner_for("m4_guardrail.py") == ("4", "SSN guardrail: enforce")
    assert steplib.banner_for("m5_enforce.py") == ("5a", "destination rule: enforce")
    assert steplib.banner_for("m5c_guardrail.py") == ("5b", "R-2003 published, prompt guard on")
    assert steplib.banner_for("m5b_enforce.py") == ("5c", "approval rule: enforce")
    assert steplib.banner_for("m6_storm.py", ["50", "--leo-delay", "25"]) == ("6", "storm: Maya's script at 8 calls a second")
    assert steplib.banner_for("m6_storm.py", ["12", "--expect", "free"]) == ("6", "storm: Maya's script at 4 calls a second")
    assert steplib.banner_for("m6_storm.py", ["20", "--rps", "6"]) == ("6", "storm: Maya's script at 6 calls a second")
    assert steplib.banner_for("m6_limit.py") == ("6", "rate limit: 2 per second per staff member")
    assert steplib.banner_for("m6_count.py")[0] == "6"
    assert steplib.banner_for("m9_new.py", ["x"]) == (None, "m9_new.py")  # unknown: its file name, no stage
    assert steplib.banner_for("m3_mode.py", ["sideways"]) == (None, "m3_mode.py")
    steps = sorted(f for f in os.listdir(os.path.join(DEMO, "scripts", "steps")) if f.startswith("m") and f.endswith(".py"))
    assert all(steplib.banner_for(f, ["act_on_behalf"])[0] for f in steps), steps  # every step script has a stage


def test_ready_posts_done_and_failed_banners_once(monkeypatch):
    import steplib  # noqa: PLC0415
    posts = []
    monkeypatch.setattr(steplib, "post_banner", lambda text, state: posts.append((text, state)) or True)
    monkeypatch.setattr(steplib, "post_row", lambda *a, **k: True)
    monkeypatch.setattr(steplib.gk, "mask", lambda t: str(t))
    monkeypatch.setattr(steplib, "LABEL", "masking on")
    monkeypatch.setattr(steplib, "_final", [])
    steplib.ready("masking on", lambda: "profile ssn ***-**-3456", every=0)
    steplib.finish(False)  # a second final banner is not posted
    assert posts == [("masking on", "done")]
    monkeypatch.setattr(steplib, "_final", [])
    posts.clear()
    with pytest.raises(SystemExit):
        steplib.ready("burst rule ACTIVE", lambda: None, timeout=-1, every=0)
    steplib._at_exit()
    assert posts == [("masking on", "failed")]
    monkeypatch.setattr(steplib, "_final", [])
    posts.clear()
    steplib.ready("a check in the middle", lambda: "ok", every=0, final=False)
    assert posts == []
    steplib._at_exit()  # the script ended without a final banner (an early exit or a crash)
    assert posts == [("masking on", "failed")]


# ---------------------------------------------------------------- /api/moment
def test_moment_input_checks():
    assert capp.moment_request({"stage": "4", "source": "click"}) == ("4", "click")
    assert capp.moment_request({"stage": None}) == (None, "api")
    assert capp.moment_request({"step": "M5c", "source": "script"}) == ("5b", "script")
    for bad in ({}, {"stage": "7"}, {"stage": 4}, {"stage": "5"}, {"step": "M9"}, {"step": None}, {"stage": "4", "step": "M4"},
                {"stage": "4", "extra": 1}, {"stage": "4", "source": "x" * 17}, {"stage": "4", "source": "a b"}, {"stage": "4", "source": 3},
                [], "4", None):
        with pytest.raises(ValueError):
            capp.moment_request(bad)


def test_moment_endpoint_auth_and_snapshot(app):
    tc, _, _ = app
    assert snapshot(tc)["moment"] == {"current": None, "source": None, "at": None}
    r = tc.post("/api/moment", json={"stage": "4", "source": "click"}, headers=ORIGIN)
    assert r.status_code == 200 and r.json()["current"] == "4" and r.json()["source"] == "click"
    assert re.fullmatch(r"\d\d:\d\d:\d\d", r.json()["at"])
    assert snapshot(tc)["moment"]["current"] == "4"
    assert client.post_moment(step="M5b", source="goto")  # the script channel (the socket)
    assert snapshot(tc)["moment"]["current"] == "5c" and snapshot(tc)["moment"]["source"] == "goto"
    assert client.post_moment(None, source="reset") and snapshot(tc)["moment"]["current"] is None
    assert client.send({"type": "moment", "stage": "9"})["ok"] is False
    assert tc.post("/api/moment", json={"stage": "9"}, headers=ORIGIN).status_code == 400
    assert tc.post("/api/moment", content=b"not json", headers={**ORIGIN, "Content-Type": "application/json"}).status_code == 400
    assert tc.post("/api/moment", json={"stage": "1"}, headers={"Authorization": "Bearer x"}).status_code == 403  # no Origin
    anon = TestClient(tc.app, base_url=BASE)
    assert anon.post("/api/moment", json={"stage": "1"}, headers=ORIGIN).status_code == 401  # no session, no token
    assert anon.post("/api/moment", json={"stage": "1"}, headers={"Origin": "http://evil.example"}).status_code == 403


def test_moment_is_published_on_the_feed_channel():
    published = []

    class Hub:
        def publish(self, channel, ev):
            published.append((channel, ev))
    c = core.Console.__new__(core.Console)
    c.hub, c.moment = Hub(), {"current": None, "source": None, "at": None}
    c.set_moment("5a", "chip")
    assert published[-1][0] == "feed" and published[-1][1]["type"] == "moment"
    assert published[-1][1]["moment"]["current"] == "5a" and published[-1][1]["moment"]["source"] == "chip"
    with pytest.raises(ValueError):
        c.set_moment("7", "chip")


# ---------------------------------------------------------------- send with a step
def test_send_with_a_step_sets_the_stage_free_text_does_not(app):
    tc, _, c = app
    assert tc.post("/api/panes/maya/send", json={"prompt": "Summarize Sam Rivera's auto loan (member M-1001).", "step": "M3"},
                   headers=ORIGIN).status_code == 202
    m = snapshot(tc)["moment"]
    assert m["current"] == "3" and m["source"] == "chip"
    assert tc.post("/api/panes/right/send", json={"prompt": "Show my open partner referrals.", "step": "M2"}, headers=ORIGIN).status_code == 202
    assert snapshot(tc)["moment"]["current"] == "2"  # backwards is allowed (an earlier chip again)
    idle(c)
    assert tc.post("/api/panes/maya/send", json={"prompt": "free text"}, headers=ORIGIN).status_code == 202  # the old page's body
    idle(c)
    assert tc.post("/api/panes/maya/send", json={"prompt": "free text", "step": "M99"}, headers=ORIGIN).status_code == 202
    idle(c)
    assert tc.post("/api/panes/maya/send", json={"prompt": "odd", "step": ["M4"]}, headers=ORIGIN).status_code == 202
    assert snapshot(tc)["moment"]["current"] == "2"


def test_a_refused_send_does_not_move_the_stage(app, monkeypatch):
    tc, _, _ = app
    monkeypatch.setattr(core.Console, "_turn", lambda self, pane, prompt: None)  # the turn never ends: the pane stays busy
    assert tc.post("/api/panes/maya/send", json={"prompt": "one", "step": "M1"}, headers=ORIGIN).status_code == 202
    assert tc.post("/api/panes/maya/send", json={"prompt": "two", "step": "M6"}, headers=ORIGIN).status_code == 409
    assert snapshot(tc)["moment"]["current"] == "1"


# ---------------------------------------------------------------- /api/banner
def test_banner_input_checks():
    assert capp.banner_request({"text": "masking on", "state": "running"}) == ("masking on", "running")
    for bad in ({}, {"text": "x"}, {"text": "", "state": "done"}, {"text": "  ", "state": "done"}, {"text": "x", "state": "ok"},
                {"text": 3, "state": "done"}, {"text": "x", "state": "done", "stage": "4"}, [], None):
        with pytest.raises(ValueError):
            capp.banner_request(bad)


def test_banner_endpoint_masks_cuts_and_ages_out(app):
    tc, _, c = app
    assert snapshot(tc)["banner"] is None
    r = client.send({"type": "banner", "text": "masking on for 123456789012", "state": "running"})
    assert r["ok"] and r["banner"]["text"] == "masking on for <acct>" and r["banner"]["state"] == "running"
    b = snapshot(tc)["banner"]
    assert b["text"] == "masking on for <acct>" and b["state"] == "running" and b["age_s"] < 5 and "ts" not in b
    r = client.send({"type": "banner", "text": "x" * 300, "state": "done"})
    assert r["ok"] and len(r["banner"]["text"]) == core.BANNER_MAX
    assert client.post_banner("x", "maybe") is False
    assert tc.post("/api/banner", json={"text": "x", "state": "done"}, headers=ORIGIN).status_code == 404  # scripts use the socket
    c.banner["ts"] -= core.BANNER_SNAPSHOT_S - 1
    assert snapshot(tc)["banner"]["age_s"] >= core.BANNER_SNAPSHOT_S - 1
    c.banner["ts"] -= 2
    assert snapshot(tc)["banner"] is None  # older than 60 s: not in the snapshot


def test_banner_is_published_on_the_feed_channel_without_its_epoch():
    published = []

    class Hub:
        def publish(self, channel, ev):
            published.append((channel, ev))
    c = core.Console.__new__(core.Console)
    c.hub, c.banner = Hub(), None
    c.set_banner("  masking   on ", "done")
    channel, ev = published[-1]
    assert channel == "feed" and ev["type"] == "banner" and ev["banner"]["text"] == "masking on" and set(ev["banner"]) == {"text", "state", "at"}


def test_banner_masks_without_sts(monkeypatch):
    def no_sts(_t):
        raise RuntimeError("NoCredentialsError")
    monkeypatch.setattr(core.gk, "mask", no_sts)
    assert core.mask_text("account 123456789012 here") == "account <acct> here"


# ---------------------------------------------------------------- /api/health
def test_health(app):
    tc, _, _ = app
    h = client.health()  # over the socket
    assert h and set(h) >= {"started_utc", "pid", "port", "status", "moment", "version", "cards", "card_errors"}
    assert h["pid"] == os.getpid() and h["port"] == PORT and set(h["status"]) == {"state", "error", "read_at"} and h["status"]["state"] == "ok"
    assert h["version"] == int(capp.newest_mtime()) and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", h["started_utc"])
    assert tc.get("/api/health").status_code == 404  # not on HTTP any more
    assert TestClient(tc.app, base_url=BASE).get("/api/health", headers={"Host": "localhost"}).status_code == 403


def test_version_covers_the_console_files():
    files = [os.path.relpath(p, DEMO) for p in capp.source_files()]
    assert "console/static/index.html" in files and "console/core.py" in files and "console/app.py" in files


def test_socket_path_follows_the_env_var():
    assert os.environ["GK_CONSOLE_SOCK"] != REAL_SOCK
    import importlib  # noqa: PLC0415
    fresh = importlib.reload(client)
    try:
        assert fresh.SOCK == os.environ["GK_CONSOLE_SOCK"]
    finally:
        importlib.reload(client)


# ---------------------------------------------------------------- the old page against the new server
def test_snapshot_keeps_every_field_the_old_page_reads(app):
    tc, _, _ = app
    s = snapshot(tc)
    assert {"panes", "feed", "status", "approval"} <= set(s) and {"maya", "right"} == set(s["panes"])
    assert tc.get("/api/prompts").status_code == 200


# ---------------------------------------------------------------- client: a no-op when the console is down
def test_client_is_a_quiet_no_op_without_a_console(sock):
    assert not os.path.exists(sock)
    assert client.post_row("-", "t", "READY", "c", "r") is False
    assert client.post_moment("4") is False and client.post_banner("masking on", "running") is False
    assert client.post_card("clear") is False and client.health() is None
    import socket  # noqa: PLC0415
    os.makedirs(os.path.dirname(sock), exist_ok=True)
    s = socket.socket(socket.AF_UNIX)
    s.bind(sock)  # a stale socket file: nothing listens on it
    s.close()
    assert client.post_moment(step="M3") is False and client.health() is None
    assert client.post_card("sideways") is False


def test_client_sends_one_line_per_message(app):
    _, _, c = app
    assert client.post_moment(None, source="reset") and client.post_moment(step="M5c", source="goto")
    assert c.moment["current"] == "5b" and c.moment["source"] == "goto"
    assert client.post_banner("x", "failed") and c.banner["state"] == "failed"
    assert client.post_row("-", "masking on", "READY", "step script", "account 123456789012") and c.feed.rows[-1]["reason"] == "account <acct>"


# ---------------------------------------------------------------- Leo's renewal in prep.py
def test_leo_renewal_decision(monkeypatch):
    import prep  # noqa: PLC0415
    now = 1_800_000_000
    assert prep.GRANT_RENEW_H == 12
    assert prep.renewal_due(None, now) and prep.renewal_due({}, now) and prep.renewal_due({"utc": "x"}, now)
    assert prep.renewal_due({"epoch": now - 13 * 3600}, now)
    assert not prep.renewal_due({"epoch": now - 11 * 3600}, now)
    calls = []
    monkeypatch.setattr(prep.consent_login, "ensure_grants", lambda users, force=False: calls.append((users, force)) or
                        {"leo": {"before": "connected", "reconsented": force, "partner_signin_shown": None, "after": "connected"}})
    for rec, want in ((None, True), ({"utc": "2026-11-04T18:28:00Z", "epoch": now - 22.8 * 3600}, True),
                      ({"utc": "2026-11-05T17:15:00Z", "epoch": now - 3 * 3600}, False)):
        monkeypatch.setattr(prep.consent_login, "last_refresh", lambda user=None, rec=rec: rec)
        g, forced = prep.ensure_leo(lambda s: None, now=now)
        assert forced is want and calls[-1] == (("leo",), want) and g["leo"]["after"] == "connected"


def test_a_failed_renewal_falls_back_to_a_probe(monkeypatch):
    import prep  # noqa: PLC0415
    import state  # noqa: PLC0415
    now = 1_800_000_000

    def boom(users, force=False):
        raise TimeoutError("portal page did not load")
    monkeypatch.setattr(prep.consent_login, "ensure_grants", boom)
    monkeypatch.setattr(prep.consent_login, "last_refresh", lambda user=None: {"utc": "x", "epoch": now - 13 * 3600})
    monkeypatch.setattr(state, "grant", lambda user: "connected")
    logged = []
    g, forced = prep.ensure_leo(logged.append, now=now)
    assert forced and g["leo"]["after"] == "connected" and g["leo"]["renewal_error"] == "TimeoutError"
    assert any("the renewal failed (TimeoutError)" in ln for ln in logged)
    monkeypatch.setattr(prep.consent_login, "last_refresh", lambda user=None: {"utc": "x", "epoch": now - 3600})
    with pytest.raises(TimeoutError):  # no renewal on age: an error in the needed re-consent stays an error
        prep.ensure_leo(logged.append, now=now)


def test_leo_expiry_line_in_eastern_time():
    import prep  # noqa: PLC0415
    rec = {"utc": "2026-11-04T18:28:00Z", "epoch": 1793816880}  # 2026-11-04 18:28 UTC
    line = prep.grant_expiry_line("Leo", rec, now=rec["epoch"] + 23 * 3600)
    assert "Thu 05 Nov 13:28 EST" in line and "in 1.0 h" in line  # 24 h later, 18:28 UTC is 13:28 EST
    assert "ago" in prep.grant_expiry_line("Leo", rec, now=rec["epoch"] + 25 * 3600)
    assert "unknown" in prep.grant_expiry_line("Leo", None)


# ---------------------------------------------------------------- the page: 7:1 for the new color pairs (offline copy of the live check)
def lum(h):
    def f(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def ratio(a, b):
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_tracker_banner_and_changed_tag_colors_are_7_to_1():
    html = open(os.path.join(DEMO, "console", "static", "index.html"), encoding="utf-8").read()
    var = dict(re.findall(r"--([a-z0-9]+):\s*(#[0-9a-fA-F]{6})", html.split(":root", 1)[1].split("}", 1)[0]))
    pairs = [("todo", "bg"), ("ok", "bg"), ("accent", "bg"), ("denytext", "bg"), ("muted", "bg"), ("chiptext", "supp")]
    contrast = {f"{a}/{b}": round(ratio(var[a], var[b]), 2) for a, b in pairs}
    assert min(contrast.values()) >= 7.0, contrast
    css = html.split("<style", 1)[1].split("</style>", 1)[0]
    assert re.search(r"#moments \{[^}]*background: var\(--bg\)", css)  # the pairs above assume the tracker row is on --bg
    assert re.search(r"#moments button\.mp \{[^}]*background: transparent[^}]*color: var\(--todo\)", css)
    assert re.search(r"#moments button\.mp\.cur \{[^}]*background: var\(--supp\)[^}]*color: var\(--chiptext\)", css)
    assert re.search(r"#status \.tag \{[^}]*background: var\(--supp\)[^}]*color: var\(--chiptext\)", css)
    live = open(os.path.join(DEMO, "tests", "test_console_live.py"), encoding="utf-8").read()
    assert all(f'("{a}", "{b}")' in live or b == "supp" for a, b in pairs)  # the live test declares them too
