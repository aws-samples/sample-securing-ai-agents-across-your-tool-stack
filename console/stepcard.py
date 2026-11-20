"""The step-card registry: every console/cards/*.json, checked against console/cards/README.md.

load() reads the folder once and returns a Registry: `cards` (the public card dicts, ordered by `show_in` in talk order
and then `order`), `by_id` (the same dicts plus `argv`, for scripts/run_step.py and the console's Run route) and
`errors` (one line per card that was skipped). An invalid card never raises: it is left out and named in `errors`.

Each excerpt is resolved for the page: its `label` (`<path> · line 4` / `<path> · lines 217-222`, then ` · <note>`), its
code `lines` (the file without the SOURCE line) and `key`, the index of the highlighted line (or null).
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.dirname(HERE)
CARDS_DIR = os.path.join(HERE, "cards")
PYTHON = ".venv/bin/python"

STAGE_IDS = ("1", "2", "3", "4", "5a", "5b", "5c", "6")  # console/core.py STAGES, in talk order
SHOW_IN = ("",) + STAGE_IDS
KINDS = ("api", "switch", "stand-in")
LABEL_PARTS = ("AGENTCORE API CALL", "AWS API CALL", "DEMO SWITCH", "STAND-IN")
FIELDS = {"id", "menu", "stage", "show_in", "order", "fallback", "clip", "kind", "label", "button", "title", "desc", "argv",
          "wide_output", "excerpts"}
EXCERPT_FIELDS = {"file", "note", "key"}
ID = re.compile(r"^[a-z0-9-]+$")
EXCERPT_FILE = re.compile(r"^[A-Za-z0-9_.-]+\.(py|dw|policy)$")
ARG = re.compile(r"^[A-Za-z0-9_./=-]{1,64}$")
SOURCE = re.compile(r"^\s*(?:#|//)\s*SOURCE:\s*(?P<path>\S+)\s+(?P<ranges>L\d+(?:-\d+)?(?:\s*,\s*L\d+(?:-\d+)?)*)\s*$")
MAX_COLS, MAX_LINES = 70, 8
WIDE_MAX_COLS = 45  # a wide_output card's code pane is about 42% wide: about 48 columns at 26 px on 1920 (measured 2026-11-06)
DOT = "\u00b7"


class CardError(ValueError):
    pass


class Registry:
    def __init__(self, cards, errors):
        order = {s: i for i, s in enumerate(SHOW_IN)}
        cards = sorted(cards, key=lambda c: (order[c["show_in"]], c["order"], c["menu"]))
        self.by_id = {c["id"]: c for c in cards}
        self.cards = [public(c) for c in cards]
        self.errors = errors

    def get(self, card_id):
        return self.by_id.get(card_id) if isinstance(card_id, str) else None


def public(card):
    return {k: v for k, v in card.items() if k != "argv"}


def _need(cond, msg):
    if not cond:
        raise CardError(msg)


def _text(card, key, limit):
    v = card.get(key)
    _need(isinstance(v, str) and v.strip() and len(v) <= limit, f"{key} must be a string of 1 to {limit} characters")
    return v


def _label(path, ranges, note):
    parts = [r.strip()[1:] for r in ranges.split(",")]
    single = len(parts) == 1 and "-" not in parts[0]
    text = f"{path} {DOT} {'line' if single else 'lines'} {', '.join(parts)}"
    return f"{text} {DOT} {note}" if note else text


def excerpt(item, cards_dir):
    _need(isinstance(item, dict) and not set(item) - EXCERPT_FIELDS, "an excerpt is {file, note?, key?}")
    name = item.get("file")
    _need(isinstance(name, str) and EXCERPT_FILE.match(name), f"excerpt file {name!r}: a .py, .dw or .policy name in this folder")
    path = os.path.join(cards_dir, name)
    _need(os.path.isfile(path), f"excerpt file {name} not found")
    note, key = item.get("note"), item.get("key")
    _need(note is None or (isinstance(note, str) and 0 < len(note) <= 48), f"{name}: note must be 1 to 48 characters")
    _need(key is None or (isinstance(key, str) and key), f"{name}: key must be a non-empty string")
    with open(path, encoding="utf-8") as f:
        raw = f.read().split("\n")
    m = SOURCE.match(raw[0]) if raw else None
    _need(m, f"{name}: the first line must be '# SOURCE: <path> L<a>-<b>'")
    _need(os.path.isfile(os.path.join(DEMO, m["path"])), f"{name}: SOURCE file {m['path']} not found in the repo")
    lines = raw[1:]
    while lines and not lines[-1].strip():
        lines.pop()
    _need(lines, f"{name}: no code lines")
    wide = [i + 2 for i, ln in enumerate(lines) if len(ln) > MAX_COLS]
    _need(not wide, f"{name}: line(s) {wide} longer than {MAX_COLS} columns")
    key_at = None
    if key is not None:
        hits = [i for i, ln in enumerate(lines) if key in ln]
        _need(len(hits) == 1, f"{name}: key {key!r} is in {len(hits)} lines, not exactly one")
        key_at = hits[0]
    return {"file": name, "label": _label(m["path"], m["ranges"], note), "lines": lines, "key": key_at}


def validate(card, file_id, cards_dir=CARDS_DIR):
    """The resolved card (with argv and command) or CardError naming the first rule it breaks."""
    _need(isinstance(card, dict), "the file must hold one JSON object")
    extra = set(card) - FIELDS
    _need(not extra, f"unknown field(s) {sorted(extra)}")
    missing = (FIELDS - {"wide_output"}) - set(card)
    _need(not missing, f"missing field(s) {sorted(missing)}")
    _need(isinstance(card["id"], str) and ID.match(card["id"]) and card["id"] == file_id, "id must be [a-z0-9-]+ and the file name")
    _need(type(card["menu"]) is int and 3 <= card["menu"] <= 17, "menu must be an int from 3 to 17")
    _need(card["stage"] in STAGE_IDS, f"stage must be one of {', '.join(STAGE_IDS)}")
    _need(card["show_in"] in SHOW_IN, "show_in must be \"\" or a stage")
    _need(type(card["order"]) is int and card["order"] >= 1, "order must be an int, 1 or more")
    _need(type(card["fallback"]) is bool, "fallback must be true or false")
    _need(card["clip"] is None or (isinstance(card["clip"], str) and re.fullmatch(r"\d\d", card["clip"])), "clip must be null or two digits")
    _need(card["kind"] in KINDS, f"kind must be one of {', '.join(KINDS)}")
    _need(isinstance(card["label"], str) and all(p in LABEL_PARTS for p in card["label"].split(" + ")), "label: badge texts joined by ' + '")
    _text(card, "button", 24)
    _text(card, "title", 48)
    _text(card, "desc", 110)
    argv = card["argv"]
    _need(isinstance(argv, list) and argv and all(isinstance(a, str) and ARG.match(a) for a in argv), "argv: a list of plain arguments")
    _need(argv[0].startswith("scripts/") and argv[0].endswith(".py") and ".." not in argv[0], "argv[0] must be a script under scripts/")
    _need(os.path.isfile(os.path.join(DEMO, argv[0])), f"argv[0] {argv[0]} not found in the repo")
    _need(type(card.get("wide_output", False)) is bool, "wide_output must be true or false")
    ex = card["excerpts"]
    _need(isinstance(ex, list) and 1 <= len(ex) <= 3, "excerpts: 1 to 3 items")
    _need(sum(1 for e in ex if isinstance(e, dict) and "key" in e) <= 1, "at most one excerpt has a key")
    excerpts = [excerpt(e, cards_dir) for e in ex]
    total = sum(len(e["lines"]) for e in excerpts)
    _need(total <= MAX_LINES, f"{total} code lines over all excerpts (at most {MAX_LINES})")
    if card.get("wide_output", False):
        long = [f"{e['file']}:{i + 2}" for e in excerpts for i, ln in enumerate(e["lines"]) if len(ln) > WIDE_MAX_COLS]
        _need(not long, f"wide_output: line(s) {long} longer than {WIDE_MAX_COLS} columns (the code pane is narrower)")
    out = {k: card[k] for k in FIELDS - {"excerpts", "wide_output"}}
    out.update({"wide_output": card.get("wide_output", False), "excerpts": excerpts, "command": " ".join([PYTHON, *argv])})
    return out


def load(cards_dir=CARDS_DIR):
    cards, errors, menus = [], [], {}
    names = sorted(f for f in os.listdir(cards_dir) if f.endswith(".json")) if os.path.isdir(cards_dir) else []
    for name in names:
        file_id = name[:-5]
        try:
            with open(os.path.join(cards_dir, name), encoding="utf-8") as f:
                card = validate(json.load(f), file_id, cards_dir)
            _need(card["menu"] not in menus, f"menu {card['menu']} is also {menus.get(card['menu'])}")
        except (OSError, ValueError) as e:  # CardError and json.JSONDecodeError are ValueErrors
            errors.append(f"{name}: {str(e)[:200]}")
            continue
        menus[card["menu"]] = card["id"]
        cards.append(card)
    return Registry(cards, errors)
