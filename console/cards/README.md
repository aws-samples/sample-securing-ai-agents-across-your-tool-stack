# Step cards: what the console's step card and Run buttons read

One JSON file per step (`<id>.json`) and its code excerpts (`*.py`, or `*.dw` / `*.policy` for policies), all in this
folder. The console loads every `*.json` here when it starts (`console/stepcard.py`). `tests/test_stepcards.py` checks
every file against the rules below, and runs `tools/code_truth.py` on every excerpt.

Worked examples: `m4-mask.json` (a demo switch) and `m5-enforce.json`
(an AgentCore API call).

## Step file fields

| Field | Type | Rule |
|---|---|---|
| `id` | string | `[a-z0-9-]+`, the file name without `.json` |
| `menu` | int | the `run.sh` menu number that runs this step (3 to 17) |
| `stage` | string | the moment the step belongs to: `"1"`, `"2"`, `"3"`, `"4"`, `"5a"`, `"5b"`, `"5c"`, `"6"`. The tracker moves here when the step starts. |
| `show_in` | string | the stage whose Run buttons include this step. `""` means before Moment 1 (no current stage). A step that runs while a clip plays usually shows in the stage before its own. |
| `order` | int | position among the buttons of `show_in`, 1 first |
| `fallback` | bool | `true`: listed under the Fallbacks button, not as a main Run button |
| `clip` | string or null | `"05"` when the step runs while that clip plays on the deck; the button then reads `<button> (clip 05)` |
| `kind` | string | `"api"`, `"switch"` or `"stand-in"`: the badge color (blue, yellow, purple) |
| `label` | string | the badge text: `AGENTCORE API CALL`, `AWS API CALL`, `DEMO SWITCH` or `STAND-IN`; a step that does two of these may join them with ` + ` (for example `STAND-IN + DEMO SWITCH`, kind of the first) |
| `button` | string | Run button text, at most 24 characters, without the clip suffix |
| `title` | string | card title, `Moment <on-stage name> · <what>`, at most 48 characters |
| `desc` | string | one or two short sentences, at most 110 characters: what the step does; for a demo switch or a stand-in, also what production does instead |
| `argv` | list of strings | the fixed command, relative to the repo root, run as `.venv/bin/python <argv...>` from the repo root. Never built from user input. |
| `wide_output` | bool, optional | `true`: the output pane is wider than the code pane (the storm cards, whose per-second lines are about 66 characters) |
| `excerpts` | list | 1 to 3 items, shown top to bottom: `{"file": "<excerpt file in this folder>", "note": "<optional short label>", "key": "<optional exact text>"}`. At most one item per card has `key`. That text must appear in exactly one line of that excerpt: the line the card highlights in orange. |

## Excerpt files

- First line: `# SOURCE: <path in the repo> L<a>-<b>[, L<c>-<d>]` (`//` instead of `#` in a policy file). Check them
  with `.venv/bin/python tools/code_truth.py console/cards/*.py`, from the repo root.
- Real code only, reformatted for the screen: re-wrap long lines, elide with a `# ...` line or an inline `...`. Never
  rename, re-order, add or simplify; the checker compares every identifier, string, number, operator and comment, in
  order, with the cited lines.
- At most 70 columns per line: the card shows code at 26 px on a 1920 px screen. At most 45 columns on a
  `wide_output` card, whose code pane is narrower (about 42% of the card).
- At most 8 code lines per card in total, over all its excerpts (the SOURCE lines do not count).
- No secret values. The name of a secret or a parameter is fine.
- One excerpt file may serve several cards (the three policy switches share one).

The card labels each excerpt from its SOURCE line: `<path> · line 4` or `<path> · lines 217-222`, then ` · <note>`.

## On-stage names and internal codes

The audience hears Moment 1 to Moment 6. Scripts, chips and file names keep the build codes, which differ in Moment 5:

| On stage | Internal code | Script |
|---|---|---|
| Moment 5a, the sequence rule | M5 | `m5_enforce.py` |
| Moment 5b, the prompt guard | M5c | `m5c_guardrail.py` |
| Moment 5c, human approval | M5b | `m5b_enforce.py` |

Card titles and `stage` use the on-stage names; ids and file names may keep the internal codes.

## Text rules

Plain English, active voice, short sentences. No em dashes, no emoji. Say what is real and what is a demo device:
a demo switch exists so the talk can show a before and an after; a stand-in plays a person, the partner or a runaway
agent.
