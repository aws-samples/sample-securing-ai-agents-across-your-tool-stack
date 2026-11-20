"""Run the Demo Console on the presenter laptop: `.venv/bin/python -m console [--port 8765]` from the repo root.

Binds to 127.0.0.1 only. The address is a constant on purpose: there is no option to listen anywhere else.
Also listens on the script channel's Unix socket (console/channel.py: logs/run/console.sock, or GK_CONSOLE_SOCK).
Needs AWS credentials for the demo account in the environment (the same ones the step scripts use).
"""
import argparse
import sys

import uvicorn

from . import channel
from .app import HOST, create_app


def main():
    ap = argparse.ArgumentParser(prog="python -m console", description="Gatekeepers demo console (localhost only)")
    ap.add_argument("--port", type=int, default=8765)
    port = ap.parse_args().port
    sock = channel.sock_path()
    try:
        channel.prepare(sock)
    except RuntimeError as e:
        sys.exit(f"console not started: {e}")
    print(f"console on http://{HOST}:{port}/ (presenter sign-in required; bound to {HOST} only); script channel {sock}", flush=True)
    uvicorn.run(create_app(port, sock), host=HOST, port=port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
