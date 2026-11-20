"""Offline tests for scripts/preflight.py: every AWS read, the portal consent and the console are fakes;
the last two tests run check 7 against tests/console_mock.py on a free 127.0.0.1 port (never 8765), with the mock's
socket in a fresh temporary directory. Nothing here touches the stack or the presenter console's logs/run/console.sock.

  .venv/bin/python -m pytest -q tests/test_preflight.py
"""
import json
import json
import os
import sys
import tempfile
import time

import pytest

DEMO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
REAL_SOCK = os.path.join(DEMO, "logs", "run", "console.sock")
sys.path.insert(0, DEMO)

import goto  # noqa: E402
import preflight as pf  # noqa: E402

ACCOUNT = "111122223333"  # AWS documentation example ID  # nosemgrep
NOW = time.time()


class FakeSts:
    def __init__(self, account=ACCOUNT, error=None):
        self.account, self.error = account, error

    def get_caller_identity(self):
        if self.error:
            raise self.error
        return {"Account": self.account, "Arn": f"arn:aws:sts::{self.account}:assumed-role/Admin/demo-session"}


class FakeSession:
    def __init__(self, sts):
        self.sts = sts

    def client(self, name, config=None):
        assert name == "sts"
        return self.sts


def before_live(**changes):
    live = dict(goto.expected_state(goto.plan("M1")))
    live.update(changes)
    return live


GW = {"gateway_id": "gw-1", "gateway_status": "READY", "gateway_engine": "eng-2", "gateway_engine_mode": "ENFORCE", "output_engine": "eng-2"}


@pytest.fixture()
def fakes(monkeypatch, tmp_path):
    """Everything check 1 to 6 reads, as fakes; check 7 asks a socket path in tmp_path that no console serves."""
    st = {"sts": FakeSts(), "live": before_live(), "gw": dict(GW), "problems": [], "rec": {"utc": "2026-11-05T17:15:00Z", "epoch": NOW - 3 * 3600},
          "grant_after": "connected", "ensure": [], "staff": {}, "saved": [], "console": "down"}
    monkeypatch.setattr(pf.prep, "expected_account", lambda: ACCOUNT)
    monkeypatch.setattr(pf.gk, "SESSION", FakeSession(st["sts"]))
    monkeypatch.setattr(pf.gk, "out", lambda refresh=False: {"GatewayId": "gw-1"})
    monkeypatch.setattr(pf.goto, "live_state", lambda: (dict(st["live"]), dict(st["gw"]), [], list(st["problems"])))
    monkeypatch.setattr(pf.consent_login, "last_refresh", lambda user=None: st["rec"])

    def ensure(users, force=False):
        st["ensure"].append((users, force))
        if st.get("ensure_error"):
            raise st["ensure_error"]
        st["rec"] = {"utc": "2026-11-05T21:00:00Z", "epoch": time.time()}
        return {"leo": {"before": "connected", "reconsented": True, "partner_signin_shown": True, "after": st["grant_after"]}}
    monkeypatch.setattr(pf.consent_login, "ensure_grants", ensure)
    import state  # noqa: PLC0415 - check 3 probes Leo's grant again after a failed renewal
    monkeypatch.setattr(state, "grant", lambda user: st["grant_after"])

    def staff(user, client="console"):
        err = st["staff"].get(user)
        if err:
            raise err
        return {"AccessToken": "not-a-real-token", "IdToken": "x"}
    monkeypatch.setattr(pf.gk, "staff_auth", staff)
    monkeypatch.setattr(pf.gk, "save", lambda name, data: st["saved"].append((name, data)) or str(tmp_path / f"{name}.json"))
    monkeypatch.setattr(pf.console_client, "SOCK", str(tmp_path / "no-console.sock"))
    assert os.path.realpath(pf.console_client.SOCK) != os.path.realpath(REAL_SOCK)
    return st


def run(st):
    lines = []
    rc = pf.main([], out=lines.append)
    rows = {r["n"]: r for r in st["saved"][-1][1]["checks"]}
    return rc, lines, rows


def test_all_ok_except_the_console_that_is_not_running(fakes):
    rc, lines, rows = run(fakes)
    assert [rows[n]["status"] for n in range(1, 8)] == ["OK"] * 6 + ["FAIL"]
    assert "start the console in terminal 1" in rows[7]["text"]
    assert rc == 1 and lines[-1] == "PREFLIGHT: 1 problem(s): fix them before you start"
    assert fakes["ensure"] == []  # Leo connected and re-consented 3 h ago: no renewal
    assert "runs out" in rows[3]["text"] and "EST" in rows[3]["text"]
    assert all(len(ln) < 400 for ln in lines) and not any("not-a-real-token" in ln for ln in lines)
    name, data = fakes["saved"][-1]
    assert name == "preflight" and data["problems"] == 1 and ACCOUNT not in json.dumps(data)  # the account ID masked in the file


def test_ok_with_a_healthy_console(fakes, monkeypatch):
    monkeypatch.setattr(pf, "check_console", lambda r, sock=None, wait_s=6: r.add(7, "Console", pf.OK, "running"))
    rc, lines, _ = run(fakes)
    assert rc == 0 and lines[-1] == "PREFLIGHT OK: ready for the demo"


def test_wrong_account_and_no_credentials(fakes, monkeypatch):
    monkeypatch.setattr(pf.gk, "SESSION", FakeSession(FakeSts(account="444455556666")))  # AWS documentation example ID  # nosemgrep
    rc, lines, rows = run(fakes)
    assert rows[1]["status"] == "FAIL" and "not the demo account" in rows[1]["text"] and "menu c" in rows[1]["text"]
    assert [rows[n]["status"] for n in range(2, 7)] == ["SKIP"] * 5 and rc == 1
    monkeypatch.setattr(pf.gk, "SESSION", FakeSession(FakeSts(error=RuntimeError("NoCredentialsError"))))
    rc, lines, rows = run(fakes)
    assert rows[1]["status"] == "FAIL" and "RuntimeError" in rows[1]["text"] and rc == 1


def test_gateway_not_ready_or_engine_not_enforce(fakes):
    fakes["gw"].update(gateway_status="UPDATING", gateway_engine_mode="LOG_ONLY")
    _, _, rows = run(fakes)
    assert rows[2]["status"] == "FAIL" and "gateway UPDATING" in rows[2]["text"] and "policy engine mode LOG_ONLY" in rows[2]["text"]
    fakes["gw"].update(GW, gateway_engine="eng-1")
    _, _, rows = run(fakes)
    assert rows[2]["status"] == "FAIL" and "not the stack's engine" in rows[2]["text"]


def test_leo_is_renewed_when_old_or_unrecorded_or_disconnected(fakes):
    fakes["rec"] = {"utc": "2026-11-04T18:28:00Z", "epoch": NOW - 22.8 * 3600}
    _, _, rows = run(fakes)
    assert fakes["ensure"] == [(("leo",), True)] and rows[3]["status"] == "OK" and rows[3]["text"].startswith("renewed now")
    fakes["rec"], fakes["ensure"] = None, []
    run(fakes)
    assert fakes["ensure"] == [(("leo",), True)]
    fakes["ensure"], fakes["live"] = [], before_live(leo="consent needed")
    fakes["rec"] = {"utc": "2026-11-05T20:00:00Z", "epoch": NOW - 3600}
    run(fakes)
    assert fakes["ensure"] == [(("leo",), True)]


def test_leo_still_not_connected_after_the_renewal_fails(fakes):
    fakes["live"], fakes["grant_after"] = before_live(leo="consent needed"), "consent needed"
    _, lines, rows = run(fakes)
    assert rows[3]["status"] == "FAIL" and "consent_login.py leo --screens" in rows[3]["text"] and "runs out" in rows[3]["text"]


def test_a_renewal_that_raises_is_a_warning_while_the_old_grant_holds(fakes):
    fakes["rec"] = {"utc": "2026-11-04T23:00:00Z", "epoch": NOW - 14 * 3600}
    fakes["ensure_error"] = TimeoutError("portal page did not load")
    _, _, rows = run(fakes)
    assert rows[3]["status"] == "WARN" and "TimeoutError" in rows[3]["text"] and "still connected" in rows[3]["text"]
    assert "in 10.0 h" in rows[3]["text"]  # the old grant: 24 h from its re-consent 14 h ago
    fakes["grant_after"] = "consent needed"
    _, _, rows = run(fakes)
    assert rows[3]["status"] == "FAIL" and "TimeoutError" in rows[3]["text"]


def test_maya_connected_is_a_warning(fakes):
    fakes["live"] = before_live(maya="connected")
    _, _, rows = run(fakes)
    assert rows[4]["status"] == "WARN" and "run the full reset (1)" in rows[4]["text"]


def test_controls_not_in_the_before_state_name_the_matching_goto_start(fakes):
    fakes["live"] = before_live(**{"deputy-mode": "act_on_behalf"})
    _, _, rows = run(fakes)
    assert rows[5]["status"] == "WARN" and "deputy-mode act_on_behalf (want service)" in rows[5]["text"]
    assert "Moment 4 (goto.py M4)" in rows[5]["text"]
    fakes["live"] = before_live(**dict(goto.expected_state(goto.plan("M6")), rate=2))
    _, _, rows = run(fakes)
    assert rows[5]["status"] == "WARN" and "match no goto.py start state" in rows[5]["text"] and "rate 2 (want 10000)" in rows[5]["text"]
    fakes["live"] = before_live(**{k: v for k, v in goto.expected_state(goto.plan("M5b")).items() if k not in ("maya", "leo")})
    _, _, rows = run(fakes)
    assert "Moment 5c (goto.py M5b)" in rows[5]["text"]  # the internal M5b is Moment 5c on stage


def test_staff_sign_in_prints_only_the_error_type(fakes):
    class NotAuthorizedException(Exception):
        pass
    fakes["staff"] = {"leo": NotAuthorizedException("Incorrect username or password. secret-ish detail")}
    _, lines, rows = run(fakes)
    assert rows[6]["status"] == "FAIL" and "leo NotAuthorizedException" in rows[6]["text"] and "sync_cognito_passwords.py" in rows[6]["text"]
    assert not any("secret-ish" in ln for ln in lines)
    fakes["staff"] = {"dana": KeyError("AuthenticationResult")}
    _, _, rows = run(fakes)
    assert "dana SignInChallenge" in rows[6]["text"]


# ---------------------------------------------------------------- check 7 with fakes (the socket's health reply)
def console_check(monkeypatch, tmp_path, reply, exists=True):
    path = tmp_path / f"console-{len(list(tmp_path.iterdir()))}.sock"
    if exists:
        path.write_text("")  # only its existence is read when nothing answers
    asked = []

    def health():
        asked.append(pf.console_client.SOCK)
        return reply() if callable(reply) else reply
    monkeypatch.setattr(pf.console_client, "health", health)
    r = pf.Report(out=lambda s: None)
    pf.check_console(r, sock=str(path), wait_s=0)
    assert asked and all(a == str(path) for a in asked)
    return r.rows[-1]


def health(state="ok", version=None, error=None, cards=15, card_errors=()):
    return {"ok": True, "started_utc": "2026-11-05T21:00:00Z", "pid": 4242, "port": 18797,
            "status": {"state": state, "error": error, "read_at": "16:00:00"}, "moment": {"current": "3", "source": "chip", "at": "16:00:00"},
            "version": version if version is not None else int(time.time()) + 60, "cards": cards, "card_errors": list(card_errors)}


def test_console_ok_old_version_no_credentials_and_card_errors(monkeypatch, tmp_path):
    row = console_check(monkeypatch, tmp_path, health())
    assert row["status"] == "OK" and "latest version" in row["text"] and "stage 3" in row["text"] and "15 step cards" in row["text"]
    row = console_check(monkeypatch, tmp_path, health(version=1))
    assert row["status"] == "WARN" and "restart the console to load the latest version" in row["text"]
    row = console_check(monkeypatch, tmp_path, health(state="unavailable", error="ExpiredTokenException"))
    assert row["status"] == "FAIL" and "no working AWS credentials" in row["text"] and "refresh them in terminal 1" in row["text"]
    row = console_check(monkeypatch, tmp_path, health(card_errors=["m9.json: menu must be an int from 3 to 17"]))
    assert row["status"] == "FAIL" and "m9.json: menu must be" in row["text"] and "skipped 1 card" in row["text"]
    row = console_check(monkeypatch, tmp_path, health(cards=0))
    assert row["status"] == "FAIL" and "no step cards" in row["text"]
    row = console_check(monkeypatch, tmp_path, None)
    assert row["status"] == "FAIL" and "no answer" in row["text"]
    row = console_check(monkeypatch, tmp_path, None, exists=False)
    assert row["status"] == "FAIL" and "start the console in terminal 1" in row["text"] and "no socket" in row["text"]


def test_console_still_starting_is_waited_for(monkeypatch, tmp_path):
    answers = iter([health(state="starting"), health()])
    monkeypatch.setattr(pf.time, "sleep", lambda s: None)
    monkeypatch.setattr(pf.console_client, "health", lambda: next(answers))
    r = pf.Report(out=lambda s: None)
    pf.check_console(r, sock=str(tmp_path / "x.sock"), wait_s=30)
    assert r.rows[-1]["status"] == "OK"


# ---------------------------------------------------------------- check 7 against the mock console (127.0.0.1, free port)
@pytest.mark.parametrize("old", [False, True])
def test_console_check_against_the_mock(old):
    from console_shots import start_mock  # noqa: PLC0415
    sock = os.path.join(tempfile.mkdtemp(prefix="gk-"), "run", "console.sock")
    proc, port = start_mock(sock, old=old, delay=0)
    try:
        assert port != 8765 and os.path.realpath(sock) != os.path.realpath(REAL_SOCK)
        r = pf.Report(out=lambda s: None)
        pf.check_console(r, sock=sock, wait_s=2)
        row = r.rows[-1]
        if old:  # a console from before the socket: nothing answers there
            assert row["status"] == "FAIL" and "no socket" in row["text"], row
        else:
            assert row["status"] == "OK" and f"127.0.0.1:{port}" in row["text"], row
            assert oct(os.stat(sock).st_mode & 0o777) == "0o600" and oct(os.stat(os.path.dirname(sock)).st_mode & 0o777) == "0o700"
    finally:
        proc.terminate()
        proc.wait(timeout=10)
