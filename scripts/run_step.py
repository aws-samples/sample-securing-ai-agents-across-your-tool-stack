#!/usr/bin/env python3
"""Run one step by its card id and show it on the console's step card.

  .venv/bin/python scripts/run_step.py <card id> [--run-id <id>]

Looks the card up in console/cards/ (console/stepcard.py), prints `$ .venv/bin/python <argv...>`, runs that fixed argv
from the repo root with PYTHONUNBUFFERED=1, and copies every output line at once to this terminal and to the console's card
(card_start first, then card_lines with every 12-digit number masked, then card_end with the exit code and seconds).
Exits with the step's exit code. Ctrl+C reaches the step (the same process group) and ends the card as stopped. --run-id is set by the
console's Run button; without it the runner makes its own. Without a console the step still runs and prints everything
(the console posts are quiet no-ops). run.sh menu entries 3 to 17 call this.
"""
import argparse
import os
import re
import signal
import subprocess
import sys
import time
import uuid

DEMO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, DEMO)

from console import stepcard  # noqa: E402
from console.channel import LINE_MAX  # noqa: E402
from console.client import post_card  # noqa: E402

ACCT = re.compile(r"\d{12}")
RUN_ID = re.compile(r"^[a-f0-9]{8,32}$")


def mask(line):
    return ACCT.sub("<acct>", line)


def python():
    venv = os.path.join(DEMO, ".venv", "bin", "python")
    return venv if os.path.exists(venv) else sys.executable


def main(argv=None, out=sys.stdout):
    ap = argparse.ArgumentParser(prog="run_step.py", description="run one demo step and show it on the console's step card")
    ap.add_argument("card_id")
    ap.add_argument("--run-id", default=None)
    a = ap.parse_args(argv)
    reg = stepcard.load()
    card = reg.get(a.card_id)
    if not card:
        print(f"unknown step {a.card_id!r}; the cards are: {', '.join(sorted(reg.by_id)) or 'none'}", file=sys.stderr)
        for e in reg.errors:
            print(f"  skipped card {e}", file=sys.stderr)
        return 2
    if a.run_id is not None and not RUN_ID.match(a.run_id):
        print("--run-id must be 8 to 32 hex digits", file=sys.stderr)
        return 2
    rid = a.run_id or uuid.uuid4().hex[:12]
    cid = card["id"]
    out.write(f"$ {card['command']}\n")
    out.flush()
    t0 = time.time()
    post_card("start", id=cid, run_id=rid, command=card["command"], started=t0)
    # The step shares this runner's process group, so Ctrl+C in the terminal and the console's Stop (a signal to the
    # group) reach the step directly, once. This runner then only notes it, keeps copying the step's last lines, and ends
    # the card as stopped. shortcut: a SIGINT sent to this runner's pid alone is not passed on; nothing sends one today.
    child = subprocess.Popen([python(), *card["argv"]], cwd=DEMO, env={**os.environ, "PYTHONUNBUFFERED": "1"}, stdin=subprocess.DEVNULL,  # fixed argv list, no shell  # nosec B603  # nosemgrep
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    stopped = []

    def on_signal(sig, _frame):
        stopped.append(sig)
    old = {s: signal.signal(s, on_signal) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        for raw in iter(child.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            out.write(line + "\n")
            out.flush()
            post_card("lines", id=cid, run_id=rid, lines=[mask(line)[:LINE_MAX]])
        code = child.wait()
    finally:
        for s, h in old.items():
            signal.signal(s, h)
        if child.poll() is None:  # this runner is being torn down: do not leave the step behind
            child.kill()
            child.wait()
    secs = round(time.time() - t0, 1)
    post_card("end", id=cid, run_id=rid, exit_code=code, seconds=secs, stopped=bool(stopped))
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    sys.exit(main())
