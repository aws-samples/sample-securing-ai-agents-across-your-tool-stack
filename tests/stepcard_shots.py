#!/usr/bin/env python3
"""Screenshots of the live page's step card on the mock console, without AWS.

  GK_CONSOLE_SOCK="$(mktemp -d)/console.sock" .venv/bin/python tests/stepcard_shots.py --all

Starts tests/console_mock.py (the real page and the real cards, 127.0.0.1 on a free port, never 8765; its socket at
GK_CONSOLE_SOCK, never logs/run/console.sock), opens the page at 1920x1080 and 1600x1000, and runs every card in
console/cards/ through the Run route in registry order (the mock's scripted runs). Writes logs/stepcard-<id>-<width>.png
in each card's done state, logs/stepcard-<id>-running-1920.png for m5-enforce and m6-storm-limit, and the contact sheet
logs/stepcard-sheet-1920.png. Checks: no code line clipped, no page overflow, the tracker row on one line at 1920,
Maya's chat pane at least 175 px high at 1920 and 70 px at 1600, a storm line on one line in the wide output pane, no
JavaScript error. Prints one JSON line per shot, then a summary; exit 1 on any failed check.
"""
import argparse
import base64
import json
import os
import sys
import time

import requests

DEMO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(DEMO, "tests"))
sys.path.insert(0, os.path.join(DEMO, "scripts"))
sys.path.insert(0, DEMO)

from console import stepcard  # noqa: E402
from console_shots import REAL_SOCK, start_mock  # noqa: E402

LOGS = os.path.join(DEMO, "logs")
SIZES = {1920: (1920, 1080), 1600: (1600, 1000)}
CHAT_MIN = {1920: 175, 1600: 70}
RUNNING = {"m5-enforce": 5.0, "m6-storm-limit": 5.5}  # seconds into the run for the running shot
MEASURE = """() => {
  const box = (s) => { const e = document.querySelector(s); if (!e) return null; const b = e.getBoundingClientRect();
                       return { top: Math.round(b.top), bottom: Math.round(b.bottom), height: Math.round(b.height), width: Math.round(b.width) }; };
  const clipped = [...document.querySelectorAll('#stepcard .code .ln')].filter((e) => e.scrollWidth > e.clientWidth + 1)
                                                                      .map((e) => e.textContent.trim().slice(0, 60));
  const t = document.querySelector('#moments'), pills = [...document.querySelectorAll('#moments button.mp, #moments button.run')];
  const tops = new Set(pills.map((p) => Math.round(p.getBoundingClientRect().top)));
  const out = document.querySelector('#stepcard .out .scroll');
  const lh = out ? parseFloat(getComputedStyle(out).lineHeight) : 0;
  const storm = [...document.querySelectorAll('#stepcard .out .ln')].filter((e) => /t\\+\\d\\ds  Maya allowed/.test(e.textContent));
  return { card: box('#stepcard'), state: (document.querySelector('#stepcard .state') || {}).textContent,
           tracker_one_line: t ? t.scrollWidth <= t.clientWidth + 1 && tops.size === 1 : null,
           runs: [...document.querySelectorAll('#moments button.run')].map((b) => b.textContent),
           chat_px: [...document.querySelectorAll('.chat')].map((c) => c.clientHeight), clipped_lines: clipped,
           code_px: document.querySelector('#stepcard .code') ? getComputedStyle(document.querySelector('#stepcard .code')).fontSize : null,
           out_px: out ? getComputedStyle(out).fontSize : null,
           storm_wrapped: storm.filter((e) => e.getBoundingClientRect().height > lh * 1.5).length, storm_lines: storm.length,
           overflow: document.documentElement.scrollHeight > innerHeight + 1 || document.documentElement.scrollWidth > innerWidth + 1 };
}"""


def problems(m, width, name):
    bad = []
    if m["overflow"]:
        bad.append("page overflows")
    if m["clipped_lines"]:
        bad.append(f"code lines clipped: {m['clipped_lines']}")
    if width == 1920 and m["tracker_one_line"] is False:
        bad.append("tracker row not on one line")
    if m["chat_px"] and m["chat_px"][0] < CHAT_MIN[width]:
        bad.append(f"Maya's chat pane {m['chat_px'][0]} px, under {CHAT_MIN[width]}")
    if m["storm_wrapped"]:
        bad.append(f"{m['storm_wrapped']} storm line(s) wrap")
    if width == 1920 and m["code_px"] != "26px":
        bad.append(f"code at {m['code_px']}, not 26px")
    return [f"{name}: {b}" for b in bad]


def sheet(page, paths, out):
    tiles = "".join(f'<figure><img src="data:image/png;base64,{base64.b64encode(open(p, "rb").read()).decode()}">'
                    f"<figcaption>{os.path.basename(p)}</figcaption></figure>" for p in paths)
    page.set_viewport_size({"width": 1920, "height": 1080})
    page.set_content("<style>body{margin:0;background:#222;color:#eee;font:16px sans-serif;display:grid;"
                     "grid-template-columns:repeat(4,1fr);gap:8px;padding:8px}figure{margin:0}img{width:100%;display:block}</style>" + tiles)
    page.screenshot(path=out, full_page=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="every card in console/cards/ (the default)")
    ap.parse_args()
    sock = os.environ.get("GK_CONSOLE_SOCK")
    if not sock or os.path.realpath(sock) == os.path.realpath(REAL_SOCK):
        raise SystemExit("set GK_CONSOLE_SOCK to a scratch path (never logs/run/console.sock)")
    reg = stepcard.load()
    if reg.errors:
        print("registry skipped:", reg.errors)
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    from consent_login import launch  # noqa: PLC0415 - falls back to the cached headless shell
    proc, port = start_mock(sock, delay=0)
    base = f"http://127.0.0.1:{port}"
    errors, bad, done_1920 = [], [], []
    try:
        with sync_playwright() as p:
            browser = launch(p)
            pages = {}
            for w, (vw, vh) in SIZES.items():
                pg = browser.new_context(viewport={"width": vw, "height": vh}).new_page()
                pg.on("pageerror", lambda e, w=w: errors.append(f"{w}: {str(e)[:300]}"))
                pg.goto(base + "/")
                pg.wait_for_selector("#status .it", timeout=20000)
                pages[w] = pg
            time.sleep(1.5)
            for c in reg.cards:
                r = requests.post(f"{base}/api/steps/{c['id']}/run", headers={"Origin": base}, timeout=5)
                assert r.status_code == 202, (c["id"], r.status_code, r.text)
                t0 = time.time()
                if c["id"] in RUNNING:
                    time.sleep(max(0.0, t0 + RUNNING[c["id"]] - time.time()))
                    pg = pages[1920]
                    path = os.path.join(LOGS, f"stepcard-{c['id']}-running-1920.png")
                    pg.screenshot(path=path)
                    m = pg.evaluate(MEASURE)
                    print(json.dumps({"shot": os.path.relpath(path, DEMO), **m}))
                    bad += problems(m, 1920, f"{c['id']} running 1920")
                for w, pg in pages.items():
                    pg.wait_for_function("() => { const s = document.querySelector('#stepcard .state');"
                                         " return s && !document.querySelector('#stepcard').classList.contains('hidden') && s.classList.contains('done'); }",
                                         timeout=30000)
                for w, pg in pages.items():
                    pg.wait_for_timeout(300)
                    path = os.path.join(LOGS, f"stepcard-{c['id']}-{w}.png")
                    pg.screenshot(path=path)
                    m = pg.evaluate(MEASURE)
                    print(json.dumps({"shot": os.path.relpath(path, DEMO), **m}))
                    bad += problems(m, w, f"{c['id']} {w}")
                    if w == 1920:
                        done_1920.append(path)
            sheet(browser.new_page(), done_1920, os.path.join(LOGS, "stepcard-sheet-1920.png"))
            browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    bad += [f"JavaScript error {e}" for e in errors]
    print(json.dumps({"cards": len(reg.cards), "registry_errors": reg.errors, "problems": bad,
                      "sheet": "logs/stepcard-sheet-1920.png"}, indent=1))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
