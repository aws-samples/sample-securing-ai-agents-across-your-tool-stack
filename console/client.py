"""Post to a running console from a step script, the runner, goto.py, reset.py or the pre-flight:
a decision feed row, the moment tracker's stage, the script banner, the step card, or a health read.

Talks to the console's Unix socket (console/channel.py: env GK_CONSOLE_SOCK, else logs/run/console.sock), one JSON line
out and one back, with 1 s timeouts. No token: the socket's 0700 directory, its 0600 mode and the console's peer-uid
check decide who may talk. Every function does nothing and returns False (health(): None) when no console answers or
the console refuses the message, and never raises, so a step script never fails because of the console. Standard
library only.
"""
import json
import socket

from .channel import MAX_BYTES, sock_path

SOCK = sock_path()
TIMEOUT_S = 1.0


def send(msg):
    """The console's reply dict, or None when no console answers."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(TIMEOUT_S)
            s.connect(SOCK)
            s.sendall((json.dumps(msg) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n") and len(buf) <= MAX_BYTES:
                chunk = s.recv(MAX_BYTES)
                if not chunk:
                    break
                buf += chunk
        reply = json.loads(buf)
        return reply if isinstance(reply, dict) else None
    except Exception:  # noqa: BLE001 - no console, a timeout, a closed connection or an odd reply: the caller carries on
        return None


def _ok(msg):
    r = send(msg)
    return bool(r and r.get("ok"))


def post_row(persona, tool, decision, control, reason, source="script"):
    return _ok({"type": "row", "persona": str(persona)[:16], "tool": str(tool)[:48], "decision": decision,
                "control": str(control)[:80], "reason": str(reason)[:600], "source": source})


def post_moment(stage=None, step=None, source="script"):
    """Set the tracker's current stage: a stage id ("1".."6", "5a", "5b", "5c"), None for no current stage, or a step
    code (step="M3") that the console maps to its stage."""
    body = {"type": "moment", "step": step} if step is not None else {"type": "moment", "stage": stage}
    body["source"] = source
    return _ok(body)


def post_banner(text, state="running"):
    """Show "Running: <text>", "Done: <text>" or "Failed: <text>" (state running, done or failed) on the console."""
    return _ok({"type": "banner", "text": text, "state": state})


def post_card(event, **fields):
    """The step card: event "start" (id, run_id, command, started), "lines" (id, run_id, lines), "end" (id, run_id,
    exit_code, seconds, stopped) or "clear" (no fields: the card and the run results go, as after a reset)."""
    if event not in ("start", "lines", "end", "clear"):
        return False
    return _ok({"type": f"card_{event}", **fields})


def health():
    """The console's health dict (pid, port, start time, AWS status, version, card registry), or None."""
    r = send({"type": "health"})
    return r if r and r.get("ok") else None
