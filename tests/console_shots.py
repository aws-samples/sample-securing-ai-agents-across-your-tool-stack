"""Visual check of the console page without AWS: starts tests/console_mock.py on a free 127.0.0.1 port
(never 8765), opens the real page in the demo venv's headless Chromium, measures the layout and takes screenshots.

  GK_CONSOLE_SOCK="$(mktemp -d)/console.sock" .venv/bin/python tests/console_shots.py [--old-page PATH] [--out logs/v3-console]

Shots (PNG, <out>-<name>.png): 1920 and 1600 wide, "before" (the fixture: stage 3 current) and "after" the mock's
scripted change (stage 4, masking on with its CHANGED mark, the done banner); 1600 "running" (the running banner),
1920 "failed" (a failed banner posted with console/client.py over the mock's socket) and 1600 "shortfeed" (one-line feed
rows only). With --old-page (a copy of the page before the change): that page against the new API, at both widths and
with the short feed at 1600. Always: the new page against the old API
(--old: no /api/moment, /api/banner or /api/health, no moment or banner in the snapshot), with a pill click and a chip
send. Prints one JSON summary: per shot the measurements (tracker on one line, chat lines visible, overlaps), the page's
JavaScript errors and console errors, and what the page sent. Exit 1 on a JavaScript error or a failed layout check.
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time

import requests

DEMO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(DEMO, "scripts"))
sys.path.insert(0, DEMO)
REAL_SOCK = os.path.join(DEMO, "logs", "run", "console.sock")

MEASURE = """() => {
  const box = (s) => { const e = document.querySelector(s); if (!e) return null; const r = e.getBoundingClientRect();
                       return {top: r.top, bottom: r.bottom, left: r.left, right: r.right, height: r.height, width: r.width}; };
  const tr = document.querySelector('#moments'), pills = [...document.querySelectorAll('#moments button.mp')];
  const ban = document.querySelector('#moments .banner');
  const chat = [...document.querySelectorAll('.pane .chat')].filter((c) => c.offsetParent);
  const lh = (c) => parseFloat(getComputedStyle(c).lineHeight) || 35;
  const tag = document.querySelector('#status .tag'), st = box('#status');
  const pillTops = pills.map((p) => Math.round(p.getBoundingClientRect().top));
  return {
    viewport: [innerWidth, innerHeight],
    overflowX: document.documentElement.scrollWidth > innerWidth, overflowY: document.documentElement.scrollHeight > innerHeight,
    status: st, tracker: tr ? box('#moments') : null, main: box('main'), feed: box('#feed'),
    tracker_one_line: tr ? (new Set(pillTops)).size === 1 && tr.scrollWidth <= tr.clientWidth : null,
    pills_right: pills.length ? Math.round(pills[pills.length - 1].getBoundingClientRect().right) : null,
    pills: pills.map((p) => [p.textContent, p.className.replace('mp ', '')]),
    banner: ban ? {text: ban.textContent, cls: ban.className, shown: !ban.classList.contains('hidden'),
                   truncated: ban.scrollWidth > ban.clientWidth, left: Math.round(ban.getBoundingClientRect().left)} : null,
    chat_lines: chat.map((c) => Math.floor(c.clientHeight / lh(c))),
    status_items: [...document.querySelectorAll('#status .it')].map((i) => [i.textContent, i.classList.contains('chg')]),
    changed_tag: tag ? {box: box('#status .tag'), inside: tag.getBoundingClientRect().top >= st.top && tag.getBoundingClientRect().bottom <= st.bottom
                                                   && tag.getBoundingClientRect().right <= st.right} : null,
  };
}"""


def free_port():
    while True:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        if port != 8765:
            return port


def start_mock(sock, old=False, page=None, delay=6.0, short_feed=False):
    """Start tests/console_mock.py on a free 127.0.0.1 port with its socket at sock (never the presenter console's)."""
    if os.path.realpath(sock) == os.path.realpath(REAL_SOCK):
        raise SystemExit("the mock never uses the presenter console's socket: pass a scratch path")
    port = free_port()
    cmd = [sys.executable, os.path.join(DEMO, "tests", "console_mock.py"), "--port", str(port), "--delay", str(delay)]
    cmd += ["--old"] if old else []
    cmd += ["--page", page] if page else []
    cmd += ["--short-feed"] if short_feed else []
    proc = subprocess.Popen(cmd, cwd=DEMO, env={**os.environ, "GK_CONSOLE_SOCK": sock, "PYTHONDONTWRITEBYTECODE": "1"},  # fixed argv list, no shell  # nosec B603  # nosemgrep
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(80):
        try:
            if requests.get(f"http://127.0.0.1:{port}/api/prompts", timeout=1).status_code == 200:
                return proc, port
        except requests.RequestException:
            time.sleep(0.25)
    proc.terminate()
    raise SystemExit(f"mock did not start: {proc.stdout.read()[:500] if proc.stdout else ''}")


def problems_of(m, name):
    bad = []
    if m["overflowX"] or m["overflowY"]:
        bad.append("page overflows the viewport")
    if m.get("tracker_one_line") is False:
        bad.append("tracker row is not on one line")
    order = [m.get("status"), m.get("tracker"), m.get("main"), m.get("feed")]
    order = [b for b in order if b]
    for a, b in zip(order, order[1:]):
        if a["bottom"] > b["top"] + 0.5:
            bad.append(f"overlap: {a} over {b}")
    if any(n < 4 for n in m.get("chat_lines") or []):
        bad.append(f"chat panes show too few lines: {m['chat_lines']}")
    if m.get("changed_tag") and not m["changed_tag"]["inside"]:
        bad.append("CHANGED tag clipped by the status strip")
    return [f"{name}: {b}" for b in bad]


def shoot(p, launch, base, size, shots, out, summary, actions=None):
    """Open base at size; shots is [(name, seconds after load)]; actions(page, log) runs after the last shot."""
    browser = launch(p)
    page = browser.new_context(viewport={"width": size[0], "height": size[1]}).new_page()
    errors, console_errors = [], []
    page.on("pageerror", lambda e: errors.append(str(e)[:300]))
    page.on("console", lambda msg: console_errors.append(msg.text[:200]) if msg.type == "error" else None)
    t0 = time.time()
    page.goto(base + "/")
    page.wait_for_selector("#feedrows tr td.d .dec", timeout=20000)
    page.wait_for_selector("#status .it", timeout=20000)
    for name, at in shots:
        page.wait_for_timeout(max(0, int((t0 + at - time.time()) * 1000)))
        m = page.evaluate(MEASURE)
        path = f"{out}-{name}.png"
        page.screenshot(path=path)
        summary["shots"][name] = {"path": os.path.relpath(path, DEMO), "measure": m}
        summary["problems"] += problems_of(m, name)
    if actions:
        actions(page, summary)
    summary["js_errors"][shots[0][0]] = errors
    summary["console_errors"][shots[0][0]] = console_errors
    browser.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(DEMO, "logs", "v3-console"))
    ap.add_argument("--old-page", default=None, help="a copy of index.html from before the change")
    a = ap.parse_args()
    token_file = os.environ.get("GK_CONSOLE_SOCK")  # the mock's socket path (the name is kept from the token-file days)
    if not token_file or os.path.realpath(token_file) == os.path.realpath(REAL_SOCK):
        raise SystemExit("set GK_CONSOLE_SOCK to a scratch path (never logs/run/console.sock)")
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    from consent_login import launch  # noqa: PLC0415 - falls back to the cached headless shell
    summary = {"shots": {}, "problems": [], "js_errors": {}, "console_errors": {}, "sent": {}}
    with sync_playwright() as p:
        for size, names in (((1920, 1080), [("1920-before", 3.0), ("1920-after", 10.5)]),
                            ((1600, 1000), [("1600-before", 3.0), ("1600-running", 7.2), ("1600-after", 10.5)])):
            proc, port = start_mock(token_file)
            try:
                def failed_banner(page, summ, port=port):
                    if size[0] != 1920:
                        return
                    import console.client as cc  # noqa: PLC0415
                    cc.SOCK = token_file  # the mock's socket
                    ok = cc.post_banner("SSN guardrail: enforce", "failed") and cc.post_moment(step="M4", source="script")
                    page.wait_for_timeout(1200)
                    m = page.evaluate(MEASURE)
                    path = f"{a.out}-1920-failed.png"
                    page.screenshot(path=path)
                    summ["shots"]["1920-failed"] = {"path": os.path.relpath(path, DEMO), "measure": m, "client_posts_ok": ok}
                    summ["problems"] += problems_of(m, "1920-failed")
                    # A pill click and a chip send against the new API: the page posts them, the tracker follows.
                    page.click("#moments button.mp[data-stage='5b']")
                    page.select_option("#pane-maya .steps", label="M4 Draft a reply")
                    page.press("#pane-maya textarea", "Enter")
                    page.wait_for_timeout(1200)
                    summ["sent"]["new-api"] = requests.get(f"http://127.0.0.1:{port}/api/mock/log", timeout=3).json()
                    summ["sent"]["new-api-tracker-after-chip"] = page.evaluate(MEASURE)["pills"]
                shoot(p, launch, f"http://127.0.0.1:{port}", size, names, a.out, summary, actions=failed_banner)
            finally:
                proc.terminate()
                proc.wait(timeout=10)
        # The new page against the old API: no JavaScript error; a pill click and a chip send still work.
        proc, port = start_mock(token_file, old=True)
        try:
            def old_api_actions(page, summ, port=port):
                page.click("#moments button.mp[data-stage='4']")
                page.select_option("#pane-maya .steps", label="M3 Loan summary")
                page.press("#pane-maya textarea", "Enter")
                page.fill("#pane-right textarea", "Free text with no chip")
                page.press("#pane-right textarea", "Enter")
                page.wait_for_timeout(1500)
                summ["sent"]["old-api"] = requests.get(f"http://127.0.0.1:{port}/api/mock/log", timeout=3).json()
                summ["sent"]["old-api-tracker-after-click"] = page.evaluate(MEASURE)["pills"]
                page.screenshot(path=f"{a.out}-oldapi-1920.png")
                summ["shots"]["oldapi-1920"] = {"path": os.path.relpath(f"{a.out}-oldapi-1920.png", DEMO)}
            shoot(p, launch, f"http://127.0.0.1:{port}", (1920, 1080), [("oldapi-1920-load", 9.0)], f"{a.out}-tmp", summary,
                  actions=old_api_actions)
            os.remove(f"{a.out}-tmp-oldapi-1920-load.png")
            summary["shots"].pop("oldapi-1920-load", None)
        finally:
            proc.terminate()
            proc.wait(timeout=10)
        # 1600 wide with six one-line feed rows: the panes when no long reason is in the feed (the R-2003 row is the worst case).
        proc, port = start_mock(token_file, short_feed=True)
        try:
            shoot(p, launch, f"http://127.0.0.1:{port}", (1600, 1000), [("1600-shortfeed", 10.5)], a.out, summary)
        finally:
            proc.terminate()
            proc.wait(timeout=10)
        if a.old_page:  # the page from before the change against the new API (it must ignore the new events)
            for size, short in (((1920, 1080), False), ((1600, 1000), False), ((1600, 1000), True)):
                proc, port = start_mock(token_file, page=a.old_page, short_feed=short)
                try:
                    shoot(p, launch, f"http://127.0.0.1:{port}", size, [(f"oldpage-{size[0]}" + ("-shortfeed" if short else ""), 10.5)],
                          a.out, summary)
                finally:
                    proc.terminate()
                    proc.wait(timeout=10)
    summary["problems"] += [f"JavaScript error in {k}: {v}" for k, v in summary["js_errors"].items() if v]
    print(json.dumps(summary, indent=1))
    return 1 if summary["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
