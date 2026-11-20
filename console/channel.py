"""The script channel: a Unix socket that step scripts, the runner, reset.py, goto.py and the pre-flight
use to reach the console, in place of the per-start bearer token the console used to write to logs/console-token.

Access control is the file system and the kernel, not a secret: the socket lives in a directory with mode 0700, the
socket file has mode 0600, and every connection's peer uid (SO_PEERCRED, Linux) must be the console's own uid, or the
connection is closed without a reply.

Protocol: one JSON object per line, one JSON reply per line, at most MAX_BYTES a message. Types: row, moment, banner,
card_start, card_lines, card_end, card_clear, health. Every field is checked against an allowlist and a length. No
message type can start a step: only the presenter's Run button (POST /api/steps/{id}/run, presenter session) does.

Path: env GK_CONSOLE_SOCK, else logs/run/console.sock in the repo. The runner's step logs go in the same directory.
"""
import asyncio
import json
import os
import re
import socket
import stat
import struct

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SOCK = os.path.normpath(os.path.join(HERE, "..", "logs", "run", "console.sock"))
MAX_BYTES = 64 * 1024
TYPES = ("row", "moment", "banner", "card_start", "card_lines", "card_end", "card_clear", "health")
DECISIONS = ("ALLOWED", "DENIED", "REDACTED", "SUPPRESSED", "THROTTLED", "CONSENT_REQUIRED", "STALE", "ERROR", "READY",
             "DISCONNECTED", "STEP")
SOURCE = re.compile(r"^[A-Za-z0-9_.-]{1,16}$")
RUN_ID = re.compile(r"^[a-f0-9]{8,32}$")
LINE_MAX, LINES_MAX = 400, 50  # characters a card output line, lines a card_lines message


def sock_path():
    return os.environ.get("GK_CONSOLE_SOCK") or DEFAULT_SOCK


class Refused(ValueError):
    pass


def _need(cond, msg):
    if not cond:
        raise Refused(msg)


def _str(body, key, limit, required=True, allow_empty=False):
    v = body.get(key)
    if v is None and not required:
        return None
    _need(isinstance(v, str) and len(v) <= limit and (allow_empty or v.strip()), f"{key}: a string of at most {limit} characters")
    return v


def _fields(body, allowed):
    extra = set(body) - set(allowed) - {"type"}
    _need(not extra, f"unknown field(s) {sorted(extra)}")


def check_row(b):
    _fields(b, ("persona", "tool", "decision", "control", "reason", "source"))
    _need(b.get("decision") in DECISIONS, "decision: not an allowed value")
    src = b.get("source", "script")
    _need(isinstance(src, str) and SOURCE.match(src), "source: 1 to 16 letters, digits, '.', '_' or '-'")
    return {"persona": _str(b, "persona", 16), "tool": _str(b, "tool", 48), "decision": b["decision"], "control": _str(b, "control", 80),
            "reason": _str(b, "reason", 600, allow_empty=True), "source": src}


def check_card_start(b, registry):
    _fields(b, ("id", "run_id", "command", "started"))
    card = registry.get(b.get("id"))
    _need(card, "id: not a loaded card")
    _need(isinstance(b.get("run_id"), str) and RUN_ID.match(b["run_id"]), "run_id: 8 to 32 hex digits")
    _need(b.get("command") == card["command"], "command: not this card's command")
    _need(isinstance(b.get("started"), (int, float)) and not isinstance(b["started"], bool), "started: epoch seconds")
    return card["id"], b["run_id"], float(b["started"])


def check_card_lines(b, registry):
    _fields(b, ("id", "run_id", "lines"))
    _need(registry.get(b.get("id")), "id: not a loaded card")
    _need(isinstance(b.get("run_id"), str) and RUN_ID.match(b["run_id"]), "run_id: 8 to 32 hex digits")
    lines = b.get("lines")
    _need(isinstance(lines, list) and 1 <= len(lines) <= LINES_MAX and all(isinstance(x, str) for x in lines),
          f"lines: 1 to {LINES_MAX} strings")
    return b["id"], b["run_id"], [x[:LINE_MAX] for x in lines]


def check_card_end(b, registry):
    _fields(b, ("id", "run_id", "exit_code", "seconds", "stopped"))
    _need(registry.get(b.get("id")), "id: not a loaded card")
    _need(isinstance(b.get("run_id"), str) and RUN_ID.match(b["run_id"]), "run_id: 8 to 32 hex digits")
    code = b.get("exit_code")
    _need(code is None or (type(code) is int and -255 <= code <= 255), "exit_code: an int or null")
    secs = b.get("seconds")
    _need(isinstance(secs, (int, float)) and not isinstance(secs, bool) and 0 <= secs < 86400, "seconds: a number")
    _need(type(b.get("stopped", False)) is bool, "stopped: true or false")
    return b["id"], b["run_id"], code, float(secs), b.get("stopped", False)


def peer_uid(sock):
    """The connecting process's uid (SO_PEERCRED), or None when the platform cannot tell (then the connection is refused).
    shortcut: Linux only; on macOS every connection is refused (fail closed); use getpeereid there if the console moves."""
    try:
        _pid, uid, _gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        return uid
    except (AttributeError, OSError):
        return None


def answers(path, timeout=1.0):
    """True if a console answers a health message on path."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(path)
            s.sendall(b'{"type": "health"}\n')
            return bool(s.recv(MAX_BYTES))
    except OSError:
        return False


def prepare(path):
    """Make the socket's directory (mode 0700, ours) and clear a stale socket file. Raises RuntimeError with a plain
    message when another console answers on path, or the path is not ours to use."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, mode=0o700, exist_ok=True)
    st = os.stat(d)
    if st.st_uid != os.getuid():
        raise RuntimeError(f"the socket directory {d} belongs to another user: set GK_CONSOLE_SOCK to a path in a directory of yours")
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(d, 0o700)
    if os.path.lexists(path):
        if not stat.S_ISSOCK(os.lstat(path).st_mode):
            raise RuntimeError(f"{path} exists and is not a socket: remove it or set GK_CONSOLE_SOCK")
        if answers(path):
            raise RuntimeError(f"another console already answers on {path}: stop it first (Ctrl-C in its terminal), "
                               "or give this one its own GK_CONSOLE_SOCK")
        os.unlink(path)  # a socket left by a console that ended without cleaning up


async def serve(path, handle, own_uid=None):
    """Listen on path (prepare() first) and answer each message with handle(dict) -> dict. Returns the asyncio server."""
    own = os.getuid() if own_uid is None else own_uid

    async def on_conn(reader, writer):
        try:
            if peer_uid(writer.get_extra_info("socket")) != own:
                return  # another user, or a platform without SO_PEERCRED: no reply
            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=30)
                except (ValueError, asyncio.LimitOverrunError):  # longer than MAX_BYTES
                    writer.write(b'{"ok": false, "error": "message longer than 64 KB"}\n')
                    return
                if not line:
                    return
                writer.write((json.dumps(dispatch(handle, line)) + "\n").encode())
                await writer.drain()
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()

    old = os.umask(0o177)  # the socket file is created 0600
    try:
        server = await asyncio.start_unix_server(on_conn, path=path, limit=MAX_BYTES)
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    return server


def dispatch(handle, line):
    try:
        body = json.loads(line)
        _need(isinstance(body, dict) and body.get("type") in TYPES, f"type must be one of {', '.join(TYPES)}")
        return {"ok": True, **(handle(body) or {})}
    except (ValueError, RuntimeError) as e:  # Refused, a JSONDecodeError, a console-side ValueError or a conflict
        return {"ok": False, "error": str(e)[:200]}
