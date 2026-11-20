"""Rehearsal harness: N runs of the talk in order, through both paths, with assertions.

  .venv/bin/python scripts/rehearse.py --runs N [--path agent|direct|both] [--moments R1.2,M1,...] [--out DIR]
                                       [--leo-delay S] [--cooldown-cap 600] [--stop-file PATH]
  .venv/bin/python scripts/rehearse.py --summary DIR       recompute DIR/summary.json from DIR/run-*.json

--stop-file PATH: before each run starts, if PATH exists the batch ends cleanly: summary.json is written, a line
starting "REHEARSE STOPPED by stop file" is printed and the exit status is 0. A run already in progress always finishes
(create the file to stop after the current run; delete it before continuing the batch with the same --out).

One run: consent_login --all; reset.py (the engine canary is inside it); the moments in talk order (scripts/moments.py),
each through the agent (the Runtime, driven like the console) and the direct path (no model), with the step scripts
applied between the before and after states; state.py on, with the M6 cool-down probe started at the raise of the
limit; then the deferred checks: CloudTrail for this run's vault events (M2-CT, bounded wait) and the LOG_ONLY
would-deny record (M5, only where the Gateway application log group exists). Each run's JSON is written to
DIR/run-NN.json when the run ends and DIR/summary.json after every run, so a cut-off session leaves usable data. With
--out naming an existing directory, new runs continue its numbering and the summary covers them all.

Exit 1 if any assertion failed (or the cool-down cap was reached, or the start state was not restored); exit 2 on an
engine stop: a policy mode switch or the canary failed with an engine error, and nothing more touches the engine.
Never prints or stores a token, password or raw Gateway log record.
"""
import glob
import json
import os
import sys
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agentlib as al  # noqa: E402
import consent_login  # noqa: E402
import gklib as gk  # noqa: E402
import moments as mo  # noqa: E402

R84_LIMIT_S = 20.0


def scrub(o):
    if isinstance(o, str):
        return gk.mask(al.redact(o))
    if isinstance(o, dict):
        return {k: scrub(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [scrub(x) for x in o]
    return o


def write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(scrub(data), f, indent=1, default=str)
    os.replace(tmp, path)


def capture_engine():
    """Reads only: every active policy's status and the engine (for an engine stop)."""
    out = {}
    for n in gk.active_policies():
        try:
            p = gk.get_policy(n)
            out[n] = {k: p.get(k) for k in ("status", "enforcementMode", "updatedAt", "statusReasons")}
        except Exception as e:  # noqa: BLE001
            out[n] = {"error": type(e).__name__}
    try:
        e = gk.ctl().get_policy_engine(policyEngineId=gk.engine_id())
        out["_engine"] = {k: e.get(k) for k in ("policyEngineId", "status", "updatedAt", "statusReasons")}
    except Exception as e:  # noqa: BLE001
        out["_engine"] = {"error": type(e).__name__}
    return out


def one_run(n, args, out_dir):
    tag = f"run-{n:02d}"
    t0 = time.time()
    run = {"run": n, "tag": tag, "start_utc": mo.utc(t0), "stack": gk.STACK, "region": gk.REGION, "path": args["path"],
           "moments_selected": args["moments"], "leo_delay": args["leo_delay"], "moments": [], "run_failures": []}
    ctx = None
    try:
        run["grants"] = consent_login.ensure_grants()
        ctx = mo.Ctx(args["path"], out_dir, tag, leo_delay=args["leo_delay"])
        r = ctx.step("reset.py")
        run["reset"] = {k: r.get(k) for k in ("rc", "seconds", "ready", "saved")}
        if r.get("saved") and os.path.exists(r["saved"]):
            rj = json.load(open(r["saved"]))
            run["reset"].update({"canary": rj.get("canary"), "mode_switch_seconds": rj.get("mode_switch_seconds"), "maya": rj.get("maya"), "seed": rj.get("seed"),
                                 "problems": rj.get("problems")})
            ctx.runtime_not_before = float(rj.get("rate_limit_raised_at") or 0) + 60
        if r.get("rc") != 0:
            run["run_failures"].append(f"reset not READY: {r.get('tail')}")
        else:
            for mid in mo.ORDER:
                if mid not in args["moments"] or mid == "M2-CT":
                    continue
                t1 = time.time()
                try:
                    rec = mo.RUNNERS[mid](ctx)
                except mo.EngineStop:
                    raise
                except Exception as e:  # noqa: BLE001 - one moment's failure must not hide the others
                    traceback.print_exc()
                    rec = mo.new_rec(mid)
                    rec["exception"] = f"{type(e).__name__}: {str(e)[:300]}"
                    for p in ("agent", "direct"):
                        rec["failures"][p].append(f"{mid} exception {rec['exception']}")
                        rec[p] = rec[p] or {"exception": True}
                rec["seconds"] = round(time.time() - t1, 1)
                run["moments"].append(rec)
                print(f"  {tag} {mid} done in {rec['seconds']} s", flush=True)
        # after state: raise the limit now (state.py on finds it set) and measure the cool-down from this moment
        t_raise = time.time()
        gk.rate_limit(mo.DEMO_RATE)
        cool = {}
        th = None
        if "M6" in args["moments"]:
            th = threading.Thread(target=lambda: cool.update(mo.cooldown(t_raise, ctx.tokens["maya"], cap_s=args["cooldown_cap"])), daemon=True)
            th.start()
        st = ctx.step("state.py", "on")
        run["state_on"] = {k: st.get(k) for k in ("rc", "seconds", "saved")}
        if st.get("saved") and os.path.exists(st["saved"]):
            sj = json.load(open(st["saved"]))
            run["state_on"].update({"problems": sj.get("problems"), "canary": sj.get("canary"), "reconnected": sj.get("reconnected")})
        if st.get("rc") != 0:
            run["run_failures"].append(f"start state not restored: {run['state_on'].get('problems')}")
        if th:
            th.join()
            run["cooldown"] = cool
            if cool.get("cap_reached"):
                run["run_failures"].append(f"cool-down cap reached ({args['cooldown_cap']} s)")
        # deferred checks
        if "M2" in args["moments"] and ctx.direct:
            ct = mo.new_rec("M2-CT")
            ct["direct"] = mo.cloudtrail_check(ctx)
            run["moments"].insert(next((i + 1 for i, m in enumerate(run["moments"]) if m["id"] == "M2"), len(run["moments"])), ct)
        m5 = next((m for m in run["moments"] if m["id"] == "M5" and "would_deny" in ctx.deferred), None)
        if m5:
            wd = mo.would_deny_check(ctx, ctx.deferred["would_deny"])
            for p in ("agent", "direct"):
                if p in wd and m5.get(p):
                    m5[p]["would_deny"] = wd[p]
    except mo.EngineStop as e:
        run["engine_stop"] = {"utc": mo.utc(), "detail": str(e)[:1500], "policies": capture_engine(),
                              "mode_switches": ctx.mode_switches if ctx else []}
        run["run_failures"].append("ENGINE STOP: a policy mode switch or the canary failed (see engine_stop)")
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        run["run_failures"].append(f"run exception {type(e).__name__}: {str(e)[:300]}")
    # assertions
    for rec in run["moments"]:
        if ctx is not None:
            try:
                mo.evaluate(ctx, rec)
            except Exception as e:  # noqa: BLE001 - a check that cannot read its data is a failure, not a crash
                traceback.print_exc()
                rec["failures"]["agent" if ctx.agent else "direct"].append(f"check exception {type(e).__name__}: {str(e)[:200]}")
                rec["green"] = {p: (False if ctx.agent and p == "agent" or ctx.direct and p == "direct" else None) for p in ("agent", "direct")}
    if ctx is not None and ctx.agent:
        lat = [x["latency_s"] for x in ctx.latencies]
        r84 = mo.new_rec("R8.4")
        r84["agent"] = {"turns": ctx.latencies, "n": len(lat), "p95": mo.p95(lat), "max": max(lat) if lat else None}
        r84["failures"]["agent"] = [] if lat and mo.p95(lat) <= R84_LIMIT_S else [f"p95 {mo.p95(lat)} s over {R84_LIMIT_S} s ({len(lat)} turns)"]
        r84["green"] = {"agent": not r84["failures"]["agent"], "direct": None}
        run["moments"].append(r84)
        run["latency"] = {"n": len(lat), "p95": mo.p95(lat), "max": max(lat) if lat else None}
        run["mode_switches"] = ctx.mode_switches
    m5 = next((m for m in run["moments"] if m["id"] == "M5"), None)
    if m5 and m5.get("agent"):
        run["m5_outcomes"] = {h: (m5["agent"].get(h) or {}).get("outcome") for h in ("LOG_ONLY", "ACTIVE")}
    m5b = next((m for m in run["moments"] if m["id"] == "M5b"), None)
    if m5b and (m5b.get("agent") or {}).get("retry") is not None:
        run["m5b_retry_outcome"] = mo.m5b_retry_outcome(m5b["agent"]["retry"])
    run["seconds"] = round(time.time() - t0, 1)
    run["end_utc"] = mo.utc()
    run["green"] = not run["run_failures"] and all(v is not False for m in run["moments"] for v in (m.get("green") or {}).values())
    return run, "engine_stop" in run


def streaks(seq):
    end, best, cur = 0, 0, 0
    for v in seq:
        cur = cur + 1 if v else 0
        best = max(best, cur)
    for v in reversed(seq):
        if not v:
            break
        end += 1
    return end, best


def summarize(out_dir):
    runs = [json.load(open(p)) for p in sorted(glob.glob(os.path.join(out_dir, "run-[0-9][0-9].json")))]
    s = {"dir": out_dir, "runs": len(runs), "updated_utc": mo.utc(), "per_moment": {}, "guardrail_moments": {}, "per_run": []}
    for mid in mo.ORDER + ["R8.4"]:
        for path in ("agent", "direct"):
            seq = [(r["run"], m["green"].get(path)) for r in runs for m in r["moments"] if m["id"] == mid and m.get("green")]
            vals = [v for _, v in seq if v is not None]
            if not vals:
                continue
            end, best = streaks(vals)
            s["per_moment"][f"{mid}/{path}"] = {"runs": len(vals), "green": sum(vals), "consecutive_green_at_end": end, "best_consecutive": best,
                                                "sequence": "".join("G" if v else "R" for v in vals), "red_runs": [n for n, v in seq if v is False]}
    for mid in mo.GUARDRAIL_MOMENTS:
        for path in ("agent", "direct"):
            k = s["per_moment"].get(f"{mid}/{path}")
            if k:
                s["guardrail_moments"][f"{mid}/{path}"] = f"{k['green']} of {k['runs']}"
    lat = [t["latency_s"] for r in runs for m in r["moments"] if m["id"] == "R8.4" for t in m["agent"].get("turns", [])]
    s["latency_all_runs"] = {"n": len(lat), "p50": sorted(lat)[len(lat) // 2] if lat else None, "p95": mo.p95(lat), "max": max(lat) if lat else None}
    outcomes = {"LOG_ONLY": {}, "ACTIVE": {}}
    for r in runs:
        for h, o in (r.get("m5_outcomes") or {}).items():
            if o:
                outcomes[h][o] = outcomes[h].get(o, 0) + 1
    s["m5_agent_outcomes"] = outcomes
    s["m5b_agent_retry_outcomes"] = {}
    for r in runs:
        o = r.get("m5b_retry_outcome")
        if o:
            s["m5b_agent_retry_outcomes"][o] = s["m5b_agent_retry_outcomes"].get(o, 0) + 1
    for r in runs:
        m6 = next((m for m in r["moments"] if m["id"] == "M6"), {})
        st = {}
        for k in ("before", "after"):
            d = (m6.get("direct") or {}).get(k) or {}
            st[k] = {"maya": d.get("maya_total"), "leo": d.get("leo_total"),
                     "leo_throttles": [c for c in d.get("leo_calls", []) if c.get("kind") != "ok"], "leo_delay": d.get("leo_delay")}
        cd = r.get("cooldown") or {}
        s["per_run"].append({"run": r["run"], "start_utc": r["start_utc"], "seconds": r.get("seconds"), "green": r.get("green"),
                             "failures": r.get("run_failures") + [f"{m['id']}/{p}: {x}" for m in r["moments"] for p in ("agent", "direct")
                                                                  for x in (m.get("failures") or {}).get(p, [])],
                             "p95": (r.get("latency") or {}).get("p95"), "cooldown_s": cd.get("cooldown_s"), "cooldown_cap_reached": cd.get("cap_reached"),
                             "cooldown_engine_throttles": cd.get("engine_throttles"), "cooldown_settled_s": cd.get("settled_s"), "cooldown_last_throttle_s": cd.get("last_throttle_s"), "storms": st, "m5": r.get("m5_outcomes"), "m5b_retry": r.get("m5b_retry_outcome"),
                             "reset_canary": (r.get("reset") or {}).get("canary"), "engine_stop": bool(r.get("engine_stop"))})
    write(os.path.join(out_dir, "summary.json"), s)
    return s


def parse(argv):
    def opt(name, default=None):
        return argv[argv.index(name) + 1] if name in argv else default
    sel = opt("--moments")
    moments = [m for m in mo.ORDER if not sel or m in sel.split(",") or (m == "M2-CT" and "M2" in sel.split(","))]
    unknown = [m for m in (sel.split(",") if sel else []) if m not in mo.ORDER]
    if unknown:
        sys.exit(f"unknown moments {unknown}; choose from {mo.ORDER}")
    path = opt("--path", "both")
    if path not in ("agent", "direct", "both"):
        sys.exit("--path must be agent, direct or both")
    return {"runs": int(opt("--runs", "1")), "path": path, "moments": moments, "out": opt("--out"),
            "leo_delay": float(opt("--leo-delay", "0")), "cooldown_cap": int(opt("--cooldown-cap", "600")), "stop_file": opt("--stop-file")}


def main(argv):
    if "--summary" in argv:
        print(json.dumps(summarize(argv[argv.index("--summary") + 1])["per_moment"], indent=1))
        return
    args = parse(argv)
    out_dir = args["out"] or os.path.join(gk.LOGS, f"rehearse-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}")
    os.makedirs(out_dir, exist_ok=True)
    first = len(glob.glob(os.path.join(out_dir, "run-[0-9][0-9].json"))) + 1
    print(f"rehearse: {args['runs']} runs, path {args['path']}, moments {','.join(args['moments'])}, out {out_dir}"
          f"{', stop file ' + args['stop_file'] if args['stop_file'] else ''}", flush=True)
    failed = False
    for n in range(first, first + args["runs"]):
        if args["stop_file"] and os.path.exists(args["stop_file"]):
            s = summarize(out_dir)
            done = n - first
            print(f"REHEARSE STOPPED by stop file {args['stop_file']} before run {n}: {done} of {args['runs']} runs done in this batch "
                  f"({'runs ' + str(first) + ' to ' + str(n - 1) if done else 'none'}), {s['runs']} runs in {out_dir}, summary.json written. "
                  f"Delete the stop file and rerun with the same --out to continue.", flush=True)
            sys.exit(0)
        run, stopped = one_run(n, args, out_dir)
        write(os.path.join(out_dir, f"run-{n:02d}.json"), run)
        s = summarize(out_dir)
        greens = " ".join(f"{m['id']}:{''.join('-' if v is None else 'G' if v else 'R' for v in (m.get('green') or {}).values())}" for m in run["moments"])
        print(f"RUN {n} {'GREEN' if run['green'] else 'RED'} in {run['seconds']} s  {greens}  p95 {(run.get('latency') or {}).get('p95')}  "
              f"cooldown {(run.get('cooldown') or {}).get('cooldown_s')}  failures {len(s['per_run'][-1]['failures'])}", flush=True)
        failed = failed or not run["green"]
        if stopped:
            print("ENGINE STOP: harness stopped; nothing more touches the engine. See", os.path.join(out_dir, f"run-{n:02d}.json"), flush=True)
            sys.exit(2)
    print("REHEARSE DONE", "RED" if failed else "GREEN", out_dir, flush=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main(sys.argv[1:])
