"""Offline tests for the script channel (Unix socket), the Run route, the step card's state and close
rules, the runner, the policy-switch READY line, and pre-flight check 7 over the socket. No AWS, no step script runs:
process starts are fakes, and the runner test runs a throwaway script from pytest's tmp_path.

  .venv/bin/python -m pytest -q tests/test_stepcard_console.py
"""
import asyncio
import io
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

import pytest

DEMO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
REAL_SOCK = os.path.join(DEMO, "logs", "run", "console.sock")
os.environ.setdefault("GK_CONSOLE_SOCK", os.path.join(tempfile.mkdtemp(prefix="gk-"), "run", "console.sock"))
sys.path.insert(0, DEMO)
sys.path.insert(0, os.path.join(DEMO, "scripts"))
sys.path.insert(0, os.path.join(DEMO, "scripts", "steps"))

from starlette.testclient import TestClient  # noqa: E402

import console.app as capp  # noqa: E402
import console.channel as channel  # noqa: E402
import console.client as client  # noqa: E402
import console.core as core  # noqa: E402

PORT = 18792
BASE = f"http://127.0.0.1:{PORT}"
ORIGIN = {"Origin": BASE}


def short_sock():
    return os.path.join(tempfile.mkdtemp(prefix="gk-"), "run", "console.sock")


class FakeProc:
    """A Popen stand-in: alive until finish() is called."""
    made = []

    def __init__(self, args, **kw):
        self.args, self.kw, self.pid, self.returncode = args, kw, 999999, None
        self.done = threading.Event()
        FakeProc.made.append(self)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if not self.done.wait(timeout):
            raise subprocess.TimeoutExpired(self.args, timeout)  # a test fake, no process  # nosemgrep
        return self.returncode

    def finish(self, code=0):
        self.returncode = code
        self.done.set()


@pytest.fixture(autouse=True)
def scratch_socket(monkeypatch):
    path = short_sock()
    monkeypatch.setattr(client, "SOCK", path)
    assert os.path.realpath(path) != os.path.realpath(REAL_SOCK)
    return path


@pytest.fixture()
def app(monkeypatch, scratch_socket):
    """(TestClient signed in, the socket path, the Console, the Popen calls). No AWS and no real process."""
    def fake_status(self):
        self.status = {"state": "ok", "deputy": "service", "masking": "off", "prompt_guard": "off", "rate_per_second": 10000,
                       "policies": {}, "read_at": core.et_clock()}
        return self.status
    made, orig_init = [], core.Console.__init__

    def init(self, loop):
        orig_init(self, loop)
        made.append(self)
    FakeProc.made = []
    monkeypatch.setattr(core.Console, "__init__", init)
    monkeypatch.setattr(core.Console, "read_status", fake_status)
    monkeypatch.setattr(core.Console, "_turn", lambda self, pane, prompt: setattr(pane, "busy", False))
    monkeypatch.setattr(core.subprocess, "Popen", FakeProc)
    signals = []
    monkeypatch.setattr(core.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(capp, "check_presenter", lambda u, p: u == "presenter1" and p == "test-only")
    monkeypatch.setattr(core.gk, "mask", lambda t: str(t).replace("123456789012", "<acct>"))
    with TestClient(capp.create_app(PORT, sock=scratch_socket), base_url=BASE) as tc:
        assert tc.post("/login", data={"username": "presenter1", "password": "test-only"}, headers=ORIGIN, follow_redirects=False).status_code == 303  # pragma: allowlist secret (a fake)
        t0 = time.time()
        while made[0].status.get("state") != "ok":
            assert time.time() - t0 < 3
            time.sleep(0.02)
        yield tc, scratch_socket, made[0], signals
    for p in FakeProc.made:
        p.finish(0)


def card(reg_id="m4-mask"):
    return capp.stepcard.load().get(reg_id)


def start_msg(cid="m4-mask", rid="aaaa1111bbbb"):
    return {"type": "card_start", "id": cid, "run_id": rid, "command": card(cid)["command"], "started": time.time()}


def snapshot(tc):
    return tc.get("/api/snapshot").json()


# ---------------------------------------------------------------- the socket
def test_socket_modes_and_cleanup(app):
    _, path, _, _ = app
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert oct(os.stat(os.path.dirname(path)).st_mode & 0o777) == "0o700"
    assert os.stat(path).st_uid == os.getuid()


def test_peer_uid_check_refuses_another_uid():
    path = short_sock()
    channel.prepare(path)
    seen = []

    async def go():
        server = await channel.serve(path, lambda b: seen.append(b) or {}, own_uid=os.getuid() + 1)  # as if we were someone else
        try:
            client.SOCK = path
            return await asyncio.to_thread(client.send, {"type": "health"})
        finally:
            server.close()
    assert asyncio.run(go()) is None and seen == []  # closed without a reply, and nothing handled


def test_prepare_clears_a_stale_socket_and_refuses_a_live_one_or_a_file(tmp_path):
    path = short_sock()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    s = socket.socket(socket.AF_UNIX)
    s.bind(path)
    s.close()  # a socket file nobody answers on
    channel.prepare(path)
    assert not os.path.exists(path)

    async def go():
        server = await channel.serve(path, lambda b: {})
        try:
            with pytest.raises(RuntimeError, match="another console already answers"):
                await asyncio.to_thread(channel.prepare, path)
        finally:
            server.close()
    asyncio.run(go())
    plain = os.path.join(os.path.dirname(path), "plain.sock")
    open(plain, "w").close()
    with pytest.raises(RuntimeError, match="not a socket"):
        channel.prepare(plain)


def test_message_validation(app):
    tc, _, c, _ = app
    bad = [
        {"type": "run", "id": "m4-mask"}, {"type": "exec"}, {"no": "type"}, [1], "x",
        {"type": "row", "persona": "-", "tool": "t", "decision": "HACKED", "control": "c", "reason": "r"},
        {"type": "row", "persona": "x" * 17, "tool": "t", "decision": "READY", "control": "c", "reason": "r"},
        {"type": "row", "persona": "-", "tool": "t", "decision": "READY", "control": "c", "reason": "r", "extra": 1},
        {"type": "banner", "text": "x", "state": "maybe"},
        {"type": "moment", "stage": "9"},
        {**start_msg(), "command": "rm -rf /"}, {**start_msg(), "id": "nope"}, {**start_msg(), "run_id": "../x"},
        {"type": "card_lines", "id": "m4-mask", "run_id": "aaaa1111bbbb", "lines": "one"},
        {"type": "card_lines", "id": "m4-mask", "run_id": "aaaa1111bbbb", "lines": ["x"] * 51},
        {"type": "card_end", "id": "m4-mask", "run_id": "aaaa1111bbbb", "exit_code": "0", "seconds": 1},
        {"type": "card_clear", "id": "m4-mask"}, {"type": "health", "x": 1},
    ]
    for msg in bad:
        r = client.send(msg)
        assert r is not None and r["ok"] is False, msg
    assert client.send({"type": "banner", "text": "x" * (channel.MAX_BYTES + 10), "state": "done"}) == {"ok": False, "error": "message longer than 64 KB"}
    assert snapshot(tc)["card"] is None and FakeProc.made == []


def test_no_message_starts_a_step(app):
    _, _, c, _ = app
    for kind in channel.TYPES + ("run", "start", "step"):
        client.send({"type": kind, "id": "m4-mask", "argv": ["scripts/steps/m4_mask.py"]})
    assert client.send(start_msg())["ok"]  # a card_start shows a card, it starts nothing
    assert FakeProc.made == [] and c.runs == {}


# ---------------------------------------------------------------- card state over the socket
def test_card_lifecycle_masking_and_snapshot(app):
    tc, _, c, _ = app
    r = client.send(start_msg())
    assert r["ok"] and r["source"] == "terminal"
    s = snapshot(tc)
    assert s["card"]["state"] == "running" and s["card"]["id"] == "m4-mask" and s["step_runs"]["m4-mask"]["state"] == "running"
    assert s["moment"]["current"] == "4"  # the tracker moves to the card's stage
    assert set(s["card"]) == {"id", "run_id", "source", "state", "at", "seconds", "exit_code", "dismissed", "elapsed_s", "lines"}
    assert client.post_card("lines", id="m4-mask", run_id="aaaa1111bbbb", lines=["account 123456789012 ok"])
    assert client.send({"type": "card_lines", "id": "m4-mask", "run_id": "cccc2222dddd", "lines": ["other run"]})["accepted"] is False
    for i in range(320):
        client.post_card("lines", id="m4-mask", run_id="aaaa1111bbbb", lines=[f"line {i}"])
    s = snapshot(tc)["card"]
    assert len(s["lines"]) == core.CARD_LINES and s["lines"][-1] == "line 319"
    assert "123456789012" not in json.dumps(snapshot(tc))
    assert client.post_card("end", id="m4-mask", run_id="aaaa1111bbbb", exit_code=0, seconds=2.4)
    s = snapshot(tc)
    assert s["card"]["state"] == "done" and s["card"]["seconds"] == 2.4 and s["step_runs"]["m4-mask"]["state"] == "done"
    client.send(start_msg("m5-enforce", "eeee3333ffff"))
    client.post_card("end", id="m5-enforce", run_id="eeee3333ffff", exit_code=1, seconds=9)
    assert snapshot(tc)["step_runs"]["m5-enforce"]["state"] == "failed" and snapshot(tc)["card"]["id"] == "m5-enforce"  # replaced
    assert client.post_card("clear")
    s = snapshot(tc)
    assert s["card"] is None and s["step_runs"] == {}


def test_close_rules(app):
    tc, _, c, _ = app
    client.send(start_msg())
    assert tc.post("/api/panes/maya/send", json={"prompt": "storm chip"}, headers=ORIGIN).status_code == 202
    assert snapshot(tc)["card"]["dismissed"] is False  # running: a send does not close it (the Moment 6 storm)
    client.post_card("end", id="m4-mask", run_id="aaaa1111bbbb", exit_code=0, seconds=1)
    for _ in range(100):
        if not c.panes["maya"].busy:
            break
        time.sleep(0.02)
    assert tc.post("/api/panes/maya/new", json={}, headers=ORIGIN).status_code == 200
    assert snapshot(tc)["card"]["dismissed"] is False  # New conversation does not close it
    assert tc.post("/api/panes/right/send", json={"prompt": "hi"}, headers=ORIGIN).status_code == 202
    assert snapshot(tc)["card"]["dismissed"] is True  # finished: a send in either pane closes it
    client.send(start_msg(rid="1111aaaa2222"))
    assert snapshot(tc)["card"]["dismissed"] is False  # a new card_start replaces it
    assert tc.post("/api/card/close", json={}, headers=ORIGIN).status_code == 200
    assert snapshot(tc)["card"]["dismissed"] is True
    assert TestClient(tc.app, base_url=BASE).post("/api/card/close", json={}, headers=ORIGIN).status_code == 401


# ---------------------------------------------------------------- the Run route
def test_steps_route_needs_a_session(app):
    tc, _, _, _ = app
    r = tc.get("/api/steps")
    assert r.status_code == 200 and {c["id"] for c in r.json()["cards"]} >= {"m4-mask", "m5-enforce"} and r.json()["errors"] == []
    anon = TestClient(tc.app, base_url=BASE)
    assert anon.get("/api/steps").status_code == 401
    assert anon.post("/api/steps/m4-mask/run", json={}, headers=ORIGIN).status_code == 401
    assert tc.post("/api/steps/m4-mask/run", json={}).status_code == 403  # no Origin
    assert tc.post("/api/steps/m4-mask/run", json={}, headers={"Origin": "http://evil.example"}).status_code == 403
    assert FakeProc.made == []


def test_run_route_unknown_busy_and_nothing_from_the_request(app):
    tc, sock, c, _ = app
    for bad in ("nope", "m4-mask;id", "..%2Fapp", "M4-MASK"):
        assert tc.post(f"/api/steps/{bad}/run", json={}, headers=ORIGIN).status_code == 404
    r = tc.post("/api/steps/m4-mask/run?argv=evil", json={"argv": ["rm"], "run_id": "deadbeef00", "env": {"X": "1"}}, headers=ORIGIN)
    assert r.status_code == 202
    rid = r.json()["run_id"]
    assert re.fullmatch(r"[a-f0-9]{12}", rid) and rid != "deadbeef00"
    (p,) = FakeProc.made
    assert p.args == [sys.executable, core.RUN_STEP, "m4-mask", "--run-id", rid]
    assert p.kw["cwd"] == core.DEMO and p.kw["stdin"] is subprocess.DEVNULL and p.kw["start_new_session"] is True
    assert p.kw["env"]["GK_CONSOLE_SOCK"] == sock and "X" not in p.kw["env"]
    assert os.path.exists(os.path.join(os.path.dirname(sock), f"step-{rid}.log"))
    assert oct(os.stat(os.path.join(os.path.dirname(sock), f"step-{rid}.log")).st_mode & 0o777) == "0o600"
    assert tc.post("/api/steps/m5-enforce/run", json={}, headers=ORIGIN).status_code == 409  # the first is still running
    assert client.send(start_msg(rid=rid))["source"] == "console"
    p.finish(0)  # the runner ended without its card_end: the console ends the card itself
    for _ in range(100):
        if snapshot(tc)["card"]["state"] != "running":
            break
        time.sleep(0.02)
    assert snapshot(tc)["card"]["state"] == "done"
    client.send(start_msg("m5-enforce", "abcdef012345"))  # a run started in the terminal is running
    assert tc.post("/api/steps/m4-mask/run", json={}, headers=ORIGIN).status_code == 409


def test_stop_signals_the_console_started_group(app, monkeypatch):
    tc, _, c, signals = app
    assert tc.post("/api/steps/stop", json={}, headers=ORIGIN).status_code == 409  # nothing running
    rid = tc.post("/api/steps/m4-mask/run", json={}, headers=ORIGIN).json()["run_id"]
    client.send(start_msg(rid=rid))
    r = tc.post("/api/steps/stop", json={}, headers=ORIGIN)
    assert r.status_code == 200 and r.json()["run_id"] == rid and signals == [(999999, signal.SIGINT)]
    FakeProc.made[0].finish(-2)
    for _ in range(100):
        if snapshot(tc)["card"]["state"] != "running":
            break
        time.sleep(0.02)
    assert snapshot(tc)["card"]["state"] == "stopped"


# ---------------------------------------------------------------- health and pre-flight check 7 over the socket
def test_health_and_preflight_check_7(app):
    import preflight as pf  # noqa: PLC0415
    _, sock, _, _ = app
    h = client.health()
    assert h["port"] == PORT and h["cards"] >= 2 and h["card_errors"] == [] and h["status"]["state"] == "ok"
    r = pf.Report(out=lambda s: None)
    pf.check_console(r, sock=sock, wait_s=0)
    assert r.rows[-1]["status"] == "OK" and "card registry OK" in r.rows[-1]["text"], r.rows[-1]
    assert client.SOCK == sock  # check 7 put the client back as it was


def test_client_is_a_no_op_without_a_console():
    assert client.post_card("start", id="m4-mask", run_id="aaaa1111bbbb", command="x", started=1.0) is False
    assert client.post_card("lines", id="m4-mask", run_id="aaaa1111bbbb", lines=["x"]) is False
    assert client.health() is None and client.post_row("-", "t", "READY", "c", "r") is False


# ---------------------------------------------------------------- the runner
def test_runner_copies_lines_masks_and_keeps_the_exit_code(monkeypatch, tmp_path):
    import run_step  # noqa: PLC0415
    step = tmp_path / "fake_step.py"
    step.write_text("import sys\nprint('hello 123456789012')\nprint('READY fake in 0.0 s: done')\nsys.exit(3)\n")
    fake = {"id": "m4-mask", "command": ".venv/bin/python scripts/steps/m4_mask.py", "argv": [str(step)]}

    class Reg:
        errors = []
        by_id = {"m4-mask": fake}

        def get(self, cid):
            return self.by_id.get(cid)
    posts = []
    monkeypatch.setattr(run_step.stepcard, "load", lambda: Reg())
    monkeypatch.setattr(run_step, "post_card", lambda ev, **f: posts.append((ev, f)) or False)
    out = io.StringIO()
    assert run_step.main(["m4-mask"], out=out) == 3
    assert out.getvalue().splitlines() == ["$ .venv/bin/python scripts/steps/m4_mask.py", "hello 123456789012", "READY fake in 0.0 s: done"]
    assert [e for e, _ in posts] == ["start", "lines", "lines", "end"]
    assert posts[1][1]["lines"] == ["hello <acct>"] and posts[3][1]["exit_code"] == 3 and posts[3][1]["stopped"] is False
    rid = posts[0][1]["run_id"]
    assert re.fullmatch(r"[a-f0-9]{12}", rid) and all(f["run_id"] == rid for _, f in posts)
    assert run_step.main(["no-such-card"], out=io.StringIO()) == 2
    assert run_step.main(["m4-mask", "--run-id", "x; rm"], out=io.StringIO()) == 2


# ---------------------------------------------------------------- the policy-switch READY line
def test_switch_ready_line(monkeypatch, capsys):
    import moments  # noqa: PLC0415
    import steplib  # noqa: PLC0415
    clock = iter([100.0])  # the switch starts at 100 s; every later reading is 110.5 s (the whole switch took 10.5 s)
    monkeypatch.setattr(steplib.time, "time", lambda: next(clock, 110.5))
    monkeypatch.setattr(steplib, "policy", lambda base: f"gk_{base}_v2")
    monkeypatch.setattr(steplib.gk, "set_policy_mode", lambda name, mode, timeout=60: 10.4)
    monkeypatch.setattr(steplib.gk, "get_policy", lambda name: {"status": "ACTIVE", "enforcementMode": "ACTIVE"})
    monkeypatch.setattr(steplib.gk, "mask", lambda t: str(t))
    monkeypatch.setattr(steplib, "post_row", lambda *a, **k: True)
    monkeypatch.setattr(steplib, "finish", lambda ok: None)
    steplib.switch("DestinationSeenInSession", "ACTIVE", "destination rule ACTIVE")
    line = capsys.readouterr().out.strip()
    assert line == "READY destination rule ACTIVE in 10.5 s: gk_DestinationSeenInSession_v2 mode ACTIVE, status ACTIVE"
    assert line.startswith("READY")  # what scripts/moments.py run_step() and goto.py read
    assert moments.step_ready({"rc": 0, "ready": line}, "m5") == []


# ---------------------------------------------------------------- the page: new color pairs at 7:1
def lum(h):
    def f(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def ratio(a, b):
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


STEPCARD_PAIRS = [("str", "bg"), ("text", "keybg"), ("str", "keybg"), ("denytext", "keybg"), ("accent", "keybg"), ("muted", "keybg"),
                  ("ok", "panel"), ("accent", "bg"), ("todo", "bg"), ("ok", "bg"), ("denytext", "bg"), ("muted", "bg"), ("text", "bg"),
                  ("chiptext", "accent"), ("chiptext", "redact"), ("chiptext", "throt"), ("text", "panel2"), ("muted", "panel"), ("denytext", "panel")]


def test_step_card_colors_are_7_to_1_and_declared_in_the_live_test():
    html = open(os.path.join(DEMO, "console", "static", "index.html"), encoding="utf-8").read()
    css = html.split("<style", 1)[1].split("</style>", 1)[0]
    var = dict(re.findall(r"--([a-z0-9]+):\s*(#[0-9a-fA-F]{6})", css))
    contrast = {f"{a}/{b}": round(ratio(var[a], var[b]), 2) for a, b in STEPCARD_PAIRS}
    assert min(contrast.values()) >= 7.0, contrast
    live = open(os.path.join(DEMO, "tests", "test_console_live.py"), encoding="utf-8").read()
    for a, b in STEPCARD_PAIRS:
        assert f'("{a}", "{b}")' in live or (a == "chiptext" and f'"{b}"' in live), (a, b)


def test_page_has_no_inner_html_and_keeps_the_csp_nonce():
    html = open(os.path.join(DEMO, "console", "static", "index.html"), encoding="utf-8").read()
    assert "innerHTML" not in html and "insertAdjacentHTML" not in html and "outerHTML" not in html
    assert re.search(r"<script nonce=\"__NONCE__\">", html) and " onclick=" not in html and "javascript:" not in html
    assert re.search(r"#stepcard \.code \{[^}]*font-size: min\(26px, 1\.36vw\)", html)
    assert "font-variant-ligatures: none" in html
