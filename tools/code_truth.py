#!/usr/bin/env python3
"""Code-truth check for the console's step-card excerpts (console/cards/*.py, *.dw, *.policy).

An excerpt file shows real code, reformatted for the screen. Its first line names the source:

    # SOURCE: lambdas/request_interceptor/handler.py L80-91, L102-110

(`//` instead of `#` is fine; paths are relative to the repo root). The rest of the file is the code as it
will appear on screen. Reformatting (line breaks, indentation, spacing) is allowed. Elisions are
allowed only where the excerpt says so: a line that is exactly `# ...` or `// ...`, or a `...`
token inside a line. Every other token must appear in the cited source lines, in the same order.

Checks:
  1. every excerpt token (identifiers, strings, numbers, operators, comments) is matched, in
     order, against the cited source ranges (a subsequence match);
  2. where source tokens were skipped between two matched excerpt tokens, the excerpt must carry
     an elision marker at that point, otherwise it is reported as an unmarked omission.

Exit 0 when every excerpt passes, 1 otherwise. Usage, from the repo root:
    python tools/code_truth.py <excerpt> [<excerpt> ...] [--demo <repo root>]
"""
import re
import sys
from pathlib import Path

DEMO = Path(__file__).resolve().parents[1]  # the repo root

TOKEN = re.compile(
    r'(?P<comment>(?:#|//)[^\n]*)'
    r'|(?P<string>[rRbBuUfF]{0,2}(?:"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'))'
    r'|(?P<ellipsis>\.\.\.)'
    r'|(?P<number>\d+(?:\.\d+)?)'
    r'|(?P<name>[A-Za-z_][A-Za-z0-9_]*)'
    r'|(?P<op>==|!=|<=|>=|->|\*\*|//|:=|&&|\|\||::|[{}()\[\],.:;=+\-*/%<>!&|^~@?])'
)
ELISION_LINE = re.compile(r'^\s*(#|//)\s*\.\.\.\s*$')
RANGE = re.compile(r'L\d{1,6}(?:-\d{1,6})?')


def parse_source_line(line):
    """'# SOURCE: <path> L<a>-<b>[, L<c>-<d>]' (or '//' or no comment marker) -> (path, ranges text), else None.

    Plain string operations plus one anchored range pattern per part, so no input can make the parse backtrack."""
    s = line.strip()
    for marker in ("#", "//"):
        if s.startswith(marker):
            s = s[len(marker):].lstrip()
            break
    if not s.startswith("SOURCE:"):
        return None
    path, _, ranges = s[len("SOURCE:"):].strip().partition(" ")
    parts = [p.strip() for p in ranges.split(",")]
    if not path or not parts or not all(RANGE.fullmatch(p) for p in parts):
        return None
    return path, ranges.strip()


def tokens(text):
    out = []
    for m in TOKEN.finditer(text):
        kind = m.lastgroup
        val = m.group(kind)
        if kind == "comment":
            body = val.lstrip("#/").strip()
            if not body:
                continue
            val = ("#" if val.startswith("#") else "//") + " " + re.sub(r"\s+", " ", body)
        if kind == "op" and val in STRUCTURE:
            continue  # brackets and commas change legitimately when a call is elided or re-wrapped
        out.append((kind, val))
    return out


STRUCTURE = {"(", ")", "[", "]", "{", "}", ","}


def parse_ranges(spec):
    rs = []
    for part in spec.split(","):
        part = part.strip().lstrip("L")
        a, _, b = part.partition("-")
        rs.append((int(a), int(b or a)))
    return rs


def check(excerpt_path, demo):
    lines = Path(excerpt_path).read_text().splitlines()
    if not lines:
        return False, ["empty file"]
    parsed = parse_source_line(lines[0])
    if not parsed:
        return False, ["first line must be '# SOURCE: <path> L<a>-<b>[, L<c>-<d>]'"]
    src_path, range_spec = parsed
    src = demo / src_path
    if not src.exists():
        return False, [f"source not found: {src}"]
    src_lines = src.read_text().splitlines()
    ranges = parse_ranges(range_spec)
    src_text = "\n".join("\n".join(src_lines[a - 1:b]) for a, b in ranges)
    stoks = tokens(src_text)

    # excerpt tokens, with elision markers kept as ("elide", ...)
    etoks = []
    for ln in lines[1:]:
        if ELISION_LINE.match(ln):
            etoks.append(("elide", "..."))
            continue
        for t in tokens(ln):
            etoks.append(("elide", "...") if t[0] == "ellipsis" else t)

    problems = []
    si = 0
    pending_elide = False
    matched = 0
    for kind, val in etoks:
        if kind == "elide":
            pending_elide = True
            continue
        start = si
        while si < len(stoks) and stoks[si][1] != val:
            si += 1
        if si >= len(stoks):
            problems.append(f"token not found in order in the cited source: {val!r}")
            si = start  # keep scanning the rest from where we were
            continue
        skipped = [v for k, v in stoks[start:si] if k != "comment"]  # dropping a source comment is fine
        if skipped and not pending_elide and matched:
            gap = " ".join(skipped)[:90]
            problems.append(f"unmarked omission before {val!r}: skipped [{gap}]")
        si += 1
        matched += 1
        pending_elide = False
    if matched == 0:
        problems.append("no code tokens matched")
    return not problems, problems


def main(argv):
    demo = DEMO
    files = []
    it = iter(argv)
    for a in it:
        if a == "--demo":
            demo = Path(next(it))
        else:
            files.append(a)
    if not files:
        print(__doc__)
        return 2
    ok_all = True
    for f in files:
        ok, problems = check(f, demo)
        ok_all &= ok
        print(f"{'PASS' if ok else 'FAIL'}  {f}")
        for p in problems:
            print(f"      - {p}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
