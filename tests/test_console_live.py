"""Live check: the Demo Console BFF against the live stack.

  .venv/bin/python -m pytest -q tests/test_console_live.py      (about 3 minutes; start state required)

Starts its own console on 127.0.0.1:8766, raises the rate limit to 10,000 per second a minute ahead (agent turns)
and puts it back to the template value at the end. Checks: Host and Origin rejection, presenter-only sign-in, the
cookie flags, the status strip against live reads, a live M3 turn as Maya (DENIED by the tool entitlement, with the
Gateway trail row merged) and as Leo (ALLOWED) in parallel, the direct-call path (DENIED naming the destination policy),
a script row, the declared color pairs (7:1), and a 1920x1080 screenshot. Evidence: logs/console-<epoch>.json and .png.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time

import httpx
import pytest

import gklib as gk
from consent_login import launch
from state import DEMO_RATE

DEMO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
PORT = 8766
BASE = f"http://127.0.0.1:{PORT}"
# Its own script socket, never the presenter console's logs/run/console.sock.
SOCK = os.path.join(tempfile.mkdtemp(prefix="gk-live-"), "run", "console.sock")
EVID = {}


@pytest.fixture(scope="module")
def console():
    gk.rate_limit(10000)
    raised = time.time()
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "GK_CONSOLE_SOCK": SOCK}
    proc = subprocess.Popen([sys.executable, "-m", "console", "--port", str(PORT)], cwd=DEMO, env=env,  # fixed argv list, no shell  # nosec B603  # nosemgrep
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(60):
        try:
            if httpx.get(BASE + "/", timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.5)
    else:
        proc.terminate()
        pytest.fail("console did not start")
    time.sleep(max(0.0, 60 - (time.time() - raised)))  # stray throttles continue for about 45 s after a raise
    yield proc
    proc.terminate()
    proc.wait(timeout=20)
    gk.rate_limit(DEMO_RATE)
    EVID["rate_limit_restored"] = gk.rate_limit()
    print("evidence", gk.save("console", EVID))


def signed_in():
    c = httpx.Client(base_url=BASE, follow_redirects=False, timeout=60)
    r = c.post("/login", data={"username": "presenter1", "password": gk.password("staff", "presenter1")}, headers={"Origin": BASE})
    assert r.status_code == 303, r.status_code
    return c


def post(c, path, body):
    return c.post(path, json=body, headers={"Origin": BASE})


def test_security(console):
    c = httpx.Client(base_url=BASE, follow_redirects=False, timeout=30)
    res = {"wrong_host": c.get("/", headers={"Host": f"localhost:{PORT}"}).status_code,
           "rebinding_host": c.get("/api/snapshot", headers={"Host": f"attacker.example:{PORT}"}).status_code,
           "cross_origin_post": c.post("/login", data={"username": "a", "password": "b"}, headers={"Origin": "http://evil.example"}).status_code,
           "post_without_origin": c.post("/login", data={"username": "a", "password": "b"}).status_code,
           "api_without_session": c.get("/api/snapshot").status_code,
           "stream_without_session": c.get("/api/feed/stream").status_code}
    r = c.post("/login", data={"username": "maya", "password": gk.password("staff", "maya")}, headers={"Origin": BASE})
    res["non_presenter_login"] = r.status_code
    assert "set-cookie" not in r.headers
    r = c.post("/login", data={"username": "presenter1", "password": "wrong-password-1A!"}, headers={"Origin": BASE})
    res["wrong_password"] = r.status_code
    r = c.post("/login", data={"username": "presenter1", "password": gk.password("staff", "presenter1")}, headers={"Origin": BASE})
    cookie = r.headers.get("set-cookie", "")
    res["presenter_login"] = r.status_code
    res["cookie_flags"] = sorted(x.strip().split("=")[0].lower() for x in cookie.split(";")[1:])
    assert res["wrong_host"] == 403 and res["rebinding_host"] == 403 and res["cross_origin_post"] == 403 and res["post_without_origin"] == 403
    assert res["api_without_session"] == 401 and res["stream_without_session"] == 401
    assert res["non_presenter_login"] == 401 and res["wrong_password"] == 401 and res["presenter_login"] == 303
    assert "httponly" in res["cookie_flags"] and "samesite=strict" in cookie.lower() and "max-age=28800" in cookie.lower()
    page = c.get("/")
    csp = page.headers.get("content-security-policy", "")
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp
    body = page.text
    assert "eyJ" not in body and "AccessToken" not in body
    EVID["security"] = res


def test_status_strip_matches_live(console):
    c = signed_in()
    st = c.get("/api/snapshot").json()["status"]
    for _ in range(10):
        if st.get("state") == "ok" and st.get("rate_per_second") == 10000:
            break
        time.sleep(1)
        st = c.get("/api/snapshot").json()["status"]
    dest = next(n for n in gk.active_policies() if "DestinationSeenInSession" in n)
    assert st["state"] == "ok"
    assert st["deputy"] == gk.ssm_get("/gk/deputy-mode") and st["masking"] == gk.ssm_get("/gk/masking")
    assert st["policies"]["DestinationSeenInSession"] == gk.get_policy(dest)["enforcementMode"]
    assert st["rate_per_second"] == gk.rate_limit()
    EVID["status"] = st


def wait_idle(c, keys, timeout=150):
    t0 = time.time()
    while time.time() - t0 < timeout:
        snap = c.get("/api/snapshot").json()
        if all(not snap["panes"][k]["busy"] for k in keys):
            return snap
        time.sleep(1)
    pytest.fail(f"panes {keys} still busy after {timeout} s")


def test_live_m3_turns_and_feed(console):
    c = signed_in()
    for k in ("maya", "right"):
        assert post(c, f"/api/panes/{k}/new", {}).status_code == 200
    prompt = "Summarize Sam Rivera's auto loan (member M-1001)."
    assert post(c, "/api/panes/maya/send", {"prompt": prompt}).status_code == 202
    assert post(c, "/api/panes/right/send", {"prompt": prompt}).status_code == 202
    assert post(c, "/api/panes/maya/send", {"prompt": prompt}).status_code == 409  # one turn per pane at a time
    wait_idle(c, ["maya", "right"])
    time.sleep(8)  # the trail poll runs a few seconds past the turn
    snap = c.get("/api/snapshot").json()
    rows = snap["feed"]
    maya = [r for r in rows if r["persona"] == "Maya" and r["tool"] == "get_loan_file"]
    leo = [r for r in rows if r["persona"] == "Leo" and r["tool"] == "get_loan_file"]
    assert maya and maya[-1]["decision"] == "DENIED" and maya[-1]["control"] == "Tool entitlement (act on behalf)", rows
    assert "lending" in maya[-1]["reason"]
    assert "agent" in maya[-1]["sources"] and "gateway" in maya[-1]["sources"], maya[-1]  # agent stream and trail row merged
    assert leo and leo[-1]["decision"] == "ALLOWED", rows
    t = snap["panes"]["maya"]["turns"][-1]
    assert t["status"] == "done" and t["text"] and any(ch["decision"] == "DENIED" for ch in t["chips"])
    assert snap["panes"]["right"]["turns"][-1]["status"] == "done"
    by_id = {r["id"]: r["decision"] for r in rows}
    for pane_snap in snap["panes"].values():  # every chip shows its feed row's current decision (trail upgrades included)
        for turn in pane_snap["turns"]:
            assert all(ch["decision"] == by_id[ch["row"]] for ch in turn["chips"] if ch.get("row") in by_id), turn["chips"]
    assert not re.search(r"eyJ[\w-]+\.[\w-]+", json.dumps(snap))
    EVID["m3"] = {"maya_row": maya[-1], "leo_row": leo[-1], "maya_chips": t["chips"], "latency_s": t.get("latency_s"),
                  "leo_latency_s": snap["panes"]["right"]["turns"][-1].get("latency_s")}


def test_direct_call_m5_denied_by_policy(console):
    c = signed_in()
    r = post(c, "/api/panes/maya/direct", {"moment": "m5"})
    assert r.status_code == 200, r.text
    calls = r.json()["calls"]
    tr = next(x for x in calls if x["tool"] == "initiate_transfer")
    assert tr["decision"] == "DENIED" and tr["control"] == "Policy DestinationSeenInSession", calls
    time.sleep(8)
    rows = c.get("/api/snapshot").json()["feed"]
    row = [x for x in rows if x["tool"] == "initiate_transfer"][-1]
    assert row["decision"] == "DENIED" and "DestinationSeenInSession" in row["control"] and "direct" in row["sources"]
    EVID["direct_m5"] = {"calls": calls, "row": row}


def test_script_row(console):
    sys.path.insert(0, DEMO)
    from console import client  # noqa: PLC0415
    client.SOCK = SOCK  # the script channel of the console this test started
    assert oct(os.stat(SOCK).st_mode & 0o777) == "0o600" and oct(os.stat(os.path.dirname(SOCK)).st_mode & 0o777) == "0o700"
    assert client.post_row("-", "m6_limit", "READY", "step script", "per-staff-member-records = 2 per second")
    assert httpx.post(f"{BASE}/api/rows", json={"persona": "x"}).status_code == 403  # no Origin; scripts no longer use HTTP
    rows = signed_in().get("/api/snapshot").json()["feed"]
    assert any(r["tool"] == "m6_limit" and r["decision"] == "READY" for r in rows)
    EVID["script_row"] = True


def lum(h):
    def f(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def ratio(a, b):
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_contrast_and_screenshot(console):
    html = open(os.path.join(DEMO, "console", "static", "index.html"), encoding="utf-8").read()
    # Every :root block: the step card's colors (--str, --keybg) sit in a second one.
    var = dict(re.findall(r"--([a-z0-9]+):\s*(#[0-9a-fA-F]{6})", " ".join(re.findall(r":root\s*\{([^}]*)\}", html))))
    pairs = [("text", "bg"), ("text", "panel"), ("text", "panel2"), ("muted", "panel"), ("muted", "panel2"), ("muted", "bg"),
             ("accent", "panel"), ("accent", "panel2"), ("on", "panel"), ("off", "panel"), ("redact", "panel"), ("denytext", "panel"), ("denytext", "panel2")]
    pairs += [("chiptext", k) for k in ("ok", "deny", "redact", "supp", "throt", "consent", "other", "accent")]
    # The moment tracker and the script banner sit on --bg (grey, done and current pills; running, done and
    # failed banners); the status strip's CHANGED tag is --chiptext on --supp (declared above).
    pairs += [("todo", "bg"), ("ok", "bg"), ("accent", "bg"), ("denytext", "bg"), ("muted", "bg")]
    # The step card's code colors on --bg and on the key line (--keybg), its header on --panel, the Run
    # buttons on --bg (running: --chiptext on --accent) and the Fallbacks list on --panel2.
    pairs += [("str", "bg"), ("text", "bg"), ("text", "keybg"), ("str", "keybg"), ("denytext", "keybg"), ("accent", "keybg"), ("muted", "keybg"),
              ("ok", "panel"), ("muted", "panel"), ("denytext", "panel")]
    contrast = {f"{a}/{b}": round(ratio(var[a], var[b]), 1) for a, b in pairs}
    assert min(contrast.values()) >= 7.0, contrast
    assert re.search(r"\.chat \{[^}]*font-size: 26px", html) and re.search(r"#feed table \{[^}]*font-size: 24px", html)
    from playwright.sync_api import sync_playwright  # noqa: PLC0415
    path = os.path.join(gk.LOGS, f"console-{int(time.time())}.png")
    with sync_playwright() as p:
        browser = launch(p)
        page = browser.new_context(viewport={"width": 1920, "height": 1080}).new_page()
        page.goto(BASE + "/")
        page.fill("#username", "presenter1")
        page.fill("#password", gk.password("staff", "presenter1"))
        page.click("button[type=submit]")
        page.wait_for_selector("#feedrows tr td.d .dec", timeout=30000)
        page.wait_for_selector("#status .it", timeout=15000)
        page.wait_for_timeout(2000)
        sizes = page.evaluate("""() => ({chat: getComputedStyle(document.querySelector('.chat')).fontSize,
                                        feed: getComputedStyle(document.querySelector('#feed table')).fontSize,
                                        overflowX: document.documentElement.scrollWidth > window.innerWidth,
                                        overflowY: document.documentElement.scrollHeight > window.innerHeight})""")
        page.screenshot(path=path)
        browser.close()
    assert sizes["chat"] == "26px" and sizes["feed"] == "24px" and not sizes["overflowX"] and not sizes["overflowY"], sizes
    EVID["contrast"] = contrast
    EVID["sizes"] = sizes
    EVID["screenshot"] = os.path.basename(path)
    print("screenshot", path)
