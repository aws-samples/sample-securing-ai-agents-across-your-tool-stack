"""Offline checks of the step cards: every console/cards/*.json against console/cards/README.md, every
excerpt through the clips' code checker, every argv[0], and run.sh entries 3 to 17 against the cards. No AWS.

  .venv/bin/python -m pytest -q tests/test_stepcards.py
"""
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

DEMO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, DEMO)

from console import stepcard  # noqa: E402

CARDS = os.path.join(DEMO, "console", "cards")
CODE_TRUTH = os.path.join(DEMO, "tools", "code_truth.py")
# run.sh menu number -> card id. Number 7 is not used; the other numbers match the talk's walkthrough.
MENU = {3: "m1-unauth", 4: "m1-consent-maya", 5: "m2-consent-leo", 6: "m2-disconnect", 8: "m2-reconnect-maya",
        9: "m3-mode", 10: "m4-mask", 11: "m4-guardrail", 12: "m5-enforce", 13: "m5c-guardrail", 14: "m5b-enforce",
        15: "m6-storm-free", 16: "m6-limit", 17: "m6-storm-limit"}


def test_every_card_is_valid():
    reg = stepcard.load()
    assert reg.errors == [], reg.errors
    assert reg.cards, "no cards in console/cards/"
    names = sorted(f[:-5] for f in os.listdir(CARDS) if f.endswith(".json"))
    assert sorted(reg.by_id) == names
    for c in reg.cards:
        assert MENU[c["menu"]] == c["id"], c["id"]  # the menu number the content brief gives this id
        assert c["command"] == ".venv/bin/python " + " ".join(reg.by_id[c["id"]]["argv"])
        assert os.path.isfile(os.path.join(DEMO, reg.by_id[c["id"]]["argv"][0]))
        assert "argv" not in c  # the page gets the command line, not the argv list
        for x in c["excerpts"]:
            assert x["key"] is None or 0 <= x["key"] < len(x["lines"])
            assert re.fullmatch(r"\S+ \u00b7 lines? [\d, -]+( \u00b7 .+)?", x["label"]), x["label"]


def test_cards_are_ordered_by_show_in_then_order():
    reg = stepcard.load()
    keys = [(stepcard.SHOW_IN.index(c["show_in"]), c["order"]) for c in reg.cards]
    assert keys == sorted(keys)


@pytest.mark.skipif(not os.path.exists(CODE_TRUTH), reason="tools/code_truth.py not here")
def test_every_excerpt_passes_the_code_checker():
    reg = stepcard.load()
    files = sorted({os.path.join(CARDS, x["file"]) for c in reg.cards for x in c["excerpts"]})
    r = subprocess.run([sys.executable, CODE_TRUTH, *files], cwd=DEMO, capture_output=True, text=True, timeout=120)  # fixed argv list, no shell  # nosec B603  # nosemgrep
    assert r.returncode == 0, (r.stdout + r.stderr)[-3000:]


def write(d, name, obj):
    with open(os.path.join(d, name), "w", encoding="utf-8") as f:
        f.write(obj if isinstance(obj, str) else json.dumps(obj))


def good(**over):
    c = json.load(open(os.path.join(CARDS, "m4-mask.json"), encoding="utf-8"))
    c.update(over)
    return c


def test_an_invalid_card_is_skipped_and_reported(tmp_path):
    for f in ("m4-mask.py", "gklib-ssm-put.py", "resp-mask-flag.py"):
        shutil.copy(os.path.join(CARDS, f), tmp_path / f)
    write(tmp_path, "m4-mask.json", good())
    bad = {
        "x-extra": good(id="x-extra", menu=3, color="red"),
        "x-menu": good(id="x-menu", menu=18),
        "x-stage": good(id="x-stage", menu=4, stage="5"),
        "x-argv": good(id="x-argv", menu=5, argv=["../elsewhere/scripts/steps/m4_mask.py"]),
        "x-shell": good(id="x-shell", menu=6, argv=["scripts/steps/m4_mask.py", "; rm -rf /"]),
        "x-label": good(id="x-label", menu=7, label="DEMO SWITCH + MAGIC"),
        "x-button": good(id="x-button", menu=8, button="b" * 25),
        "x-key2": good(id="x-key2", menu=9, excerpts=[{"file": "gklib-ssm-put.py", "key": "e"}]),
        "x-twokeys": good(id="x-twokeys", menu=11, excerpts=[{"file": "m4-mask.py", "key": "gk"}, {"file": "gklib-ssm-put.py", "key": "def"}]),
        "x-file": good(id="x-file", menu=12, excerpts=[{"file": "../app.py"}]),
        "x-name": good(id="x-other-name", menu=13),
        "x-dup": good(id="x-dup", menu=10),  # the same menu number as m4-mask
    }
    for name, card in bad.items():
        write(tmp_path, f"{name}.json", card)
    write(tmp_path, "x-json.json", "{not json")
    write(tmp_path, "long.py", "# SOURCE: scripts/gklib.py L231-232\n" + "x = 1\n" * 9)
    write(tmp_path, "x-lines.json", good(id="x-lines", menu=14, excerpts=[{"file": "long.py"}]))
    write(tmp_path, "wide.py", "# SOURCE: scripts/gklib.py L231\n" + "y" * 71 + "\n")
    write(tmp_path, "x-wide.json", good(id="x-wide", menu=15, excerpts=[{"file": "wide.py"}]))
    write(tmp_path, "nosrc.py", "x = 1\n")
    write(tmp_path, "x-nosrc.json", good(id="x-nosrc", menu=16, excerpts=[{"file": "nosrc.py"}]))
    # wide_output: the narrower code pane holds 45 columns (gklib-ssm-put.py has a 63-column line)
    write(tmp_path, "x-wideout.json", good(id="x-wideout", menu=17, wide_output=True))
    reg = stepcard.load(str(tmp_path))
    assert list(reg.by_id) == ["m4-mask"]
    skipped = sorted(e.split(":")[0] for e in reg.errors)
    assert skipped == sorted([f"{n}.json" for n in bad] + ["x-json.json", "x-lines.json", "x-wide.json", "x-nosrc.json",
                                                           "x-wideout.json"]), reg.errors
    assert any("x-wideout.json" in e and "45 columns" in e for e in reg.errors), reg.errors
    assert reg.get("x-menu") is None and reg.get(None) is None and reg.get(["m4-mask"]) is None


def test_excerpt_labels():
    assert stepcard._label("scripts/steps/m4_mask.py", "L4", None) == "scripts/steps/m4_mask.py \u00b7 line 4"
    assert stepcard._label("scripts/gklib.py", "L217-222", "set_policy_mode()") == "scripts/gklib.py \u00b7 lines 217-222 \u00b7 set_policy_mode()"
    assert stepcard._label("a.py", "L1-2, L9-10", None) == "a.py \u00b7 lines 1-2, 9-10"


def run_sh_entries():
    text = open(os.path.join(DEMO, "run.sh"), encoding="utf-8").read()
    return {int(m[1]): (m[2], m[3], m[4]) for m in re.finditer(r'^  "(\d+)\|([^|]*)\|([^|]*)\|([^"]*)"$', text, re.M)}


def test_run_sh_entries_3_to_17_run_the_cards():
    entries = run_sh_entries()
    assert sorted(entries) == [1, 2, *sorted(MENU)]  # 7 is gone, and nothing took its number
    assert entries[1][1] == "$PY scripts/prep.py" and entries[2][1] == "$PY scripts/prep.py --quick"  # prep stays
    for n in sorted(MENU):
        assert entries[n][1] == f"$PY scripts/run_step.py {MENU[n]}", (n, entries[n])
    for c in stepcard.load().cards:  # every card present now: its menu entry runs it
        assert entries[c["menu"]][1] == f"$PY scripts/run_step.py {c['id']}"
