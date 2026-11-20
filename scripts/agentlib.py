"""Talk to the Member Navigator agent the way the console does: HTTPS with the persona's staff
access token as Bearer, one runtime session per conversation, the policy session ID echoed from the agent's first
`session` event. Works against the deployed Runtime (default) or a local agent (--local http://127.0.0.1:18080).

  .venv/bin/python scripts/agentlib.py <persona> "<prompt>" [ "<next prompt>" ...] [--local URL] [--save name]

Prints each turn's events with URLs reduced to their host and no tokens. A `session_reset` event starts a new
conversation and resends the prompt once, as the console does.
"""
import json
import os
import re
import sys
import time
import urllib.parse
import uuid

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gklib as gk  # noqa: E402


def runtime_url(arn=None, region=gk.REGION):
    arn = arn or gk.out()["AgentRuntimeArn"]
    return f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{urllib.parse.quote(arn, safe='')}/invocations?qualifier=DEFAULT"


def redact(value):
    t = re.sub(r"https?://([^/\s\"']+)[^\s\"']*", r"<url host=\1>", json.dumps(value) if not isinstance(value, str) else value)
    return re.sub(r"eyJ[\w-]+\.[\w-]+\.[\w-]+", "<jwt>", t)


class Conversation:
    def __init__(self, persona, url=None, token=None):
        self.persona = persona
        self.url = url or runtime_url()
        self.token = token or gk.access(persona)
        self.new()

    def new(self, keep_runtime_session=False):
        """A new conversation. Keeping the runtime session keeps its warm microVM: every new runtime session ID is a new
        microVM with a cold start of about 4.5 s, while a new policy session costs nothing."""
        if not keep_runtime_session or not getattr(self, "runtime_session_id", None):
            self.runtime_session_id = f"gk-{self.persona}-{uuid.uuid4().hex}"  # at least 33 characters
        self.policy_session_id = None
        self.fresh = True

    def _post(self, payload, timeout=180, on_event=None):
        h = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json", "Accept": "text/event-stream",
             "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": self.runtime_session_id}
        t0 = time.time()
        r = requests.post(self.url, headers=h, json=payload, stream=True, timeout=timeout)
        events, first = [], None
        if r.status_code != 200:
            return {"http": r.status_code, "body": r.text[:300], "events": [], "latency_s": round(time.time() - t0, 2)}
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except ValueError:
                continue
            if isinstance(ev, str):  # the SDK JSON-encodes non-dict values; ours are dicts
                continue
            if first is None:
                first = round(time.time() - t0, 2)
            events.append(ev)
            if on_event:  # the console streams each event to its pane as it arrives
                on_event(ev)
        return {"http": 200, "events": events, "first_event_s": first, "latency_s": round(time.time() - t0, 2)}

    def turn(self, prompt, retry_on_reset=True, on_event=None):
        payload = {"prompt": prompt}
        if self.policy_session_id:
            payload["policy_session_id"] = self.policy_session_id
        if self.fresh:
            payload["new_conversation"] = True
            self.fresh = False
        res = self._post(payload, on_event=on_event)
        summary = summarize(res)
        if summary.get("policy_session_id"):
            self.policy_session_id = summary["policy_session_id"]
        if summary["session_reset"] and retry_on_reset:
            self.new(keep_runtime_session=True)  # new policy session and history, same warm microVM
            retry = self.turn(prompt, retry_on_reset=False, on_event=on_event)
            retry["after_reset_of"] = summary
            return retry
        return summary


def summarize(res):
    ev = res.get("events", [])
    s = {"http": res.get("http"), "latency_s": res.get("latency_s"), "first_event_s": res.get("first_event_s"),
         "policy_session_id": next((e.get("policy_session_id") for e in ev if e.get("type") == "session"), None),
         "tools": [{"tool": e["tool"], "input": e.get("input")} for e in ev if e.get("type") == "tool_start"],
         "decisions": [{k: e.get(k) for k in ("tool", "decision", "policies", "layer", "limit", "limit_key", "reason") if e.get(k) is not None}
                       for e in ev if e.get("type") == "decision"],
         "consent": [e.get("url") for e in ev if e.get("type") == "consent"],
         "session_reset": any(e.get("type") == "session_reset" for e in ev),
         "errors": [e for e in ev if e.get("type") == "error"],
         "text": "".join(e.get("text", "") for e in ev if e.get("type") == "text"),
         "done": next((e for e in ev if e.get("type") == "done"), None)}
    if res.get("http") != 200:
        s["body"] = res.get("body")
    return s


def show(summary):
    s = dict(summary)
    s["consent"] = [urllib.parse.urlparse(u or "").netloc for u in s.get("consent") or []]
    return gk.mask(redact(s))


def main(argv):
    local = argv[argv.index("--local") + 1] if "--local" in argv else None
    save = argv[argv.index("--save") + 1] if "--save" in argv else None
    args = [a for i, a in enumerate(argv) if not a.startswith("--") and (i == 0 or not argv[i - 1].startswith("--"))]
    persona, prompts = args[0], args[1:]
    conv = Conversation(persona, url=(local.rstrip("/") + "/invocations") if local else None)
    turns = []
    for p in prompts:
        t = conv.turn(p)
        turns.append(t)
        print(show(t))
    if save:
        print("saved", gk.save(save, {"persona": persona, "target": "local" if local else "runtime", "turns": turns}))


if __name__ == "__main__":
    main(sys.argv[1:])
