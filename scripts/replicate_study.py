#!/usr/bin/env python3
"""Run K replicates of the editorial prequential study and summarise their spread.

The harness reuses one set of study arguments (--base-args-file), gives each
replicate its own --output (DIR/rep-N) and --seed, enforces global call caps
across replicates, and reads each finished replicate read-only.  It never reads
.env files, never prints environment values and inherits the caller's
environment unchanged.  Tests inject a fake study script with --study-script.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shlex
import sqlite3
import statistics
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_STUDY = Path(__file__).with_name("run_editorial_prequential_study.py")
DEFAULT_BASE_SEED = "editorial-prequential-v1"
STUDY_DEFAULT_CAPS = {"--max-decision-calls": 6000, "--max-optimizer-calls": 60}


def load_base_args(path):
    """A JSON list of strings, or text split shell-style ('#' comments allowed)."""
    text = Path(path).read_text()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return shlex.split(text, comments=True)
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError("a JSON base-args file must be a list of strings")
    return value


def check_base_args(args):
    for token in args:
        name = token.split("=", 1)[0]
        if name in ("--output", "--seed"):
            raise ValueError(f"base args must not contain {name}; the harness sets it per replicate")


def per_run_cap(args, flag):
    for i, token in enumerate(args):
        if token == flag and i + 1 < len(args):
            return int(args[i + 1])
        if token.startswith(flag + "="):
            return int(token.split("=", 1)[1])
    return STUDY_DEFAULT_CAPS[flag]


def replicate_seeds(count, seeds, base_seed):
    if seeds:
        return list(seeds)
    return [f"{base_seed}:rep-{n}" for n in range(1, count + 1)]


def build_command(study, base_args, out, seed):
    return [sys.executable, str(study), *base_args, "--output", str(out), "--seed", seed]


# ---------------------------------------------------------------- reading runs

def find_mode_dir(rep_dir, mode=None):
    if mode:
        return rep_dir / mode
    found = sorted(p for p in rep_dir.iterdir() if p.is_dir() and (p / "results.json").exists()) \
        if rep_dir.is_dir() else []
    if found:
        return found[0]
    candidates = sorted(p for p in rep_dir.iterdir() if p.is_dir()) if rep_dir.is_dir() else []
    return candidates[0] if candidates else rep_dir / "none"


def read_ledger(rep_dir):
    path = rep_dir / "global_budget_ledger.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text()).get("consumed")
    except (OSError, json.JSONDecodeError):
        return None


def read_events(db):
    if not db.exists():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [json.loads(p) for (p,) in con.execute("select payload from runtime_events order by id")]
    finally:
        con.close()


def window_accuracies(correct, size):
    n = (len(correct) // size) * size
    return [100.0 * sum(correct[i:i + size]) / size for i in range(0, n, size)]


def majority_share(labels, size):
    n = (len(labels) // size) * size
    out = []
    for i in range(0, n, size):
        chunk = labels[i:i + size]
        out.append(100.0 * max(chunk.count(x) for x in set(chunk)) / size)
    return out


def activation_cycles(events):
    """Sorted cycle numbers at which a rubric/classifier candidate was activated."""
    return sorted(e["cycle_number"] for e in events if e.get("kind") == "classifier-activated")


def mean(values):
    return sum(values) / len(values) if values else None


def summarise_replicate(rep_dir, window, mode=None):
    """Metrics for one replicate, or {'status': 'incomplete', ...}."""
    mode_dir = find_mode_dir(rep_dir, mode)
    results_path = mode_dir / "results.json"
    ledger = read_ledger(rep_dir)
    if not results_path.exists():
        return {"status": "incomplete", "reason": "no results.json", "ledger": ledger}
    try:
        cycles = json.loads(results_path.read_text()).get("cycles", [])
    except (OSError, json.JSONDecodeError):
        return {"status": "incomplete", "reason": "unreadable results.json", "ledger": ledger}
    try:
        expected = json.loads((rep_dir / "protocol.json").read_text()).get("operational_items")
    except (OSError, json.JSONDecodeError):
        expected = None
    if expected is not None and len(cycles) < expected:
        return {"status": "incomplete", "reason": f"{len(cycles)} of {expected} cycles",
                "cycles": len(cycles), "ledger": ledger}
    if ledger is None:
        return {"status": "incomplete", "reason": "no global_budget_ledger.json", "cycles": len(cycles)}
    cycles = sorted(cycles, key=lambda c: c["cycle_number"])
    truth = {c["cycle_number"]: c["human_label"] for c in cycles}
    human = [c["human_label"] for c in cycles]
    cyclotron = window_accuracies([c["predicted_label"] == c["human_label"] for c in cycles], window)
    events = read_events(mode_dir / "runtime.sqlite3")
    predictions = {}
    for e in events:
        if e.get("kind") == "prediction":
            predictions[e["cycle_number"]] = e
    decision = [predictions[c["cycle_number"]]["decision_model_label"] == c["human_label"]
                if c["cycle_number"] in predictions else False for c in cycles]
    activations = activation_cycles(events)
    head = [bool(p.get("fitted_head")) for p in predictions.values()]
    post = mean(cyclotron[1:])
    return {
        "status": "complete", "cycles": len(cycles), "windows": len(cyclotron),
        "cyclotron_by_window": cyclotron,
        "decision_model_by_window": window_accuracies(decision, window),
        "majority_baseline_by_window": majority_share(human, window),
        "first_activation_cycle": activations[0] if activations else None,
        "rubric_activations": len(activations),
        "fitted_head_used": any(head),
        "fitted_head_share": (sum(head) / len(head)) if head else 0.0,
        "decision_requests": ledger.get("decision_requests"),
        "optimizer_requests": ledger.get("optimizer_requests"),
        "post_warmup_accuracy": post,
        "final_window_accuracy": cyclotron[-1] if cyclotron else None,
        "learning_curve_area": mean(cyclotron),
        "truth_cycles": len(truth),
    }


# -------------------------------------------------------------------- aggregate

def bootstrap_median_interval(values, seed="replicates-bootstrap", resamples=2000, level=0.90):
    if not values:
        return None
    rng = random.Random(seed)
    meds = sorted(statistics.median(rng.choices(values, k=len(values))) for _ in range(resamples))
    tail = (1 - level) / 2
    return [meds[int(tail * (resamples - 1))], meds[int(round((1 - tail) * (resamples - 1)))]]


def spread(values):
    values = [v for v in values if v is not None]
    if not values:
        return None
    return {"n": len(values), "median": statistics.median(values), "min": min(values),
            "max": max(values), "bootstrap_90_median": bootstrap_median_interval(values)}


def aggregate(replicates, margin=5.0):
    done = [r for r in replicates.values() if r.get("status") == "complete"]
    finals = [r["final_window_accuracy"] for r in done]
    posts = [r["post_warmup_accuracy"] for r in done if r["post_warmup_accuracy"] is not None]
    beats = [r["final_window_accuracy"] >= r["majority_baseline_by_window"][-1] + margin
             for r in done if r["majority_baseline_by_window"]]
    return {
        "complete": len(done), "total": len(replicates),
        "incomplete": sorted(k for k, r in replicates.items() if r.get("status") != "complete"),
        "final_window_accuracy": spread(finals),
        "post_warmup_accuracy": spread(posts),
        "reliability": {
            "share_final_beats_baseline_by_5": (sum(beats) / len(beats)) if beats else None,
            "margin_points": margin,
            "min_post_warmup_accuracy": min(posts) if posts else None,
        },
    }


def _f(v, digits=1):
    return "-" if v is None else f"{v:.{digits}f}"


def render_markdown(summary):
    reps = summary["replicates"]
    agg = summary["aggregate"]
    lines = [f"# Replicate summary", "",
             f"Window size {summary['window_size']}; {agg['complete']} of {agg['total']} replicates complete.", ""]
    lines += ["| replicate | status | seed | cycles | cyclotron by window | decision model by window | "
              "majority by window | first activation | activations | head share | Jev requests | "
              "optimizer requests | post-warm-up | final | AULC |",
              "|" + "---|" * 15]
    for name, r in reps.items():
        if r["status"] != "complete":
            lines.append(f"| {name} | {r['status']}: {r.get('reason', '')} | {r.get('seed', '')} |" + " |" * 12)
            continue
        by = lambda k: " / ".join(_f(x) for x in r[k])
        lines.append(f"| {name} | complete | {r.get('seed', '')} | {r['cycles']} | {by('cyclotron_by_window')} | "
                     f"{by('decision_model_by_window')} | {by('majority_baseline_by_window')} | "
                     f"{r['first_activation_cycle'] if r['first_activation_cycle'] is not None else '-'} | "
                     f"{r['rubric_activations']} | {_f(100 * r['fitted_head_share'])}% | {r['decision_requests']} | "
                     f"{r['optimizer_requests']} | {_f(r['post_warmup_accuracy'])} | "
                     f"{_f(r['final_window_accuracy'])} | {_f(r['learning_curve_area'])} |")
    lines += ["", "| metric | n | median | min | max | bootstrap 90% interval of median |", "|---|---|---|---|---|---|"]
    for key, label in (("final_window_accuracy", "final-window accuracy"),
                       ("post_warmup_accuracy", "post-warm-up accuracy")):
        s = agg[key]
        if s:
            iv = s["bootstrap_90_median"]
            lines.append(f"| {label} | {s['n']} | {_f(s['median'])} | {_f(s['min'])} | {_f(s['max'])} | "
                         f"{_f(iv[0])} to {_f(iv[1])} |")
        else:
            lines.append(f"| {label} | 0 | - | - | - | - |")
    rel = agg["reliability"]
    share = rel["share_final_beats_baseline_by_5"]
    lines += ["", f"Reliability: {_f(None if share is None else 100 * share, 0)}% of complete replicates have "
              f"final-window accuracy at least {rel['margin_points']:.0f} points above the majority-class baseline; "
              f"minimum post-warm-up accuracy {_f(rel['min_post_warmup_accuracy'])}.", ""]
    if summary.get("launch_notes"):
        lines += ["Launch notes:", *[f"- {n}" for n in summary["launch_notes"]], ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------- running

class Guard:
    """Global cap check. Finished replicates count at their ledger's consumed calls;
    running ones and the candidate count at the per-run cap (worst case)."""

    def __init__(self, decision_cap, optimizer_cap, run_decision, run_optimizer):
        self.caps = (decision_cap, optimizer_cap)
        self.run = (run_decision, run_optimizer)

    def allows(self, finished_ledgers, running):
        for i, key in enumerate(("decision_requests", "optimizer_requests")):
            cap = self.caps[i]
            if cap is None:
                continue
            used = sum((l or {}).get(key, self.run[i]) if l is not None else self.run[i] for l in finished_ledgers)
            if used + (running + 1) * self.run[i] > cap:
                return False, f"{key}: {used} consumed + {running + 1} x {self.run[i]} per-run cap > global {cap}"
        return True, ""


def run_all(args, base_args, seeds, out_dir):
    study = Path(args.study_script)
    guard = Guard(args.max_total_decision_calls, args.max_total_optimizer_calls,
                  per_run_cap(base_args, "--max-decision-calls"), per_run_cap(base_args, "--max-optimizer-calls"))
    names = [f"rep-{n}" for n in range(1, len(seeds) + 1)]
    queue = list(zip(names, seeds))
    finished_ledgers, running, exits, notes = [], {}, {}, []
    halted = False
    # Existing replicate directories are reused only with --resume: complete ones are
    # skipped and counted from their ledgers; partial ones are reported, never relaunched.
    existing = [(n, sd) for n, sd in queue if (out_dir / n).exists()]
    if existing and not args.resume:
        raise SystemExit("replicate output already exists (" + ", ".join(n for n, _ in existing)
                         + "); pass --resume to reuse it")
    for name, seed in existing:
        queue.remove((name, seed))
        s = summarise_replicate(out_dir / name, args.window_size, args.mode)
        finished_ledgers.append(read_ledger(out_dir / name))
        if s["status"] == "complete":
            exits[name] = "skipped (already complete)"
        else:
            notes.append(f"{name}: existing output is incomplete ({s.get('reason')}); not relaunched")
            exits[name] = "incomplete (not relaunched)"
    while queue or running:
        while queue and not halted and len(running) < args.parallel:
            ok, why = guard.allows(finished_ledgers, len(running))
            if not ok:
                notes.append(f"global cap guard stopped launching: {why}")
                halted = True
                break
            name, seed = queue.pop(0)
            cmd = build_command(study, base_args, out_dir / name, seed)
            with open(out_dir / f"{name}.log", "ab") as log:
                running[name] = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
        if not running:
            break
        time.sleep(args.poll_seconds)
        for name in [n for n, p in running.items() if p.poll() is not None]:
            code = running.pop(name).returncode
            exits[name] = code
            finished_ledgers.append(read_ledger(out_dir / name))
            if code != 0 and not halted:
                notes.append(f"{name} exited {code}; no further replicates launched")
                halted = True
    for name, _ in queue:
        exits[name] = "not launched"
    return exits, notes


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-args-file", type=Path, required=True)
    p.add_argument("--study-script", type=Path, default=DEFAULT_STUDY)
    p.add_argument("--replicates", type=int, default=3)
    p.add_argument("--seeds", help="comma-separated seeds used verbatim (sets the replicate count)")
    p.add_argument("--base-seed", default=DEFAULT_BASE_SEED,
                   help="default seeds are <base-seed>:rep-N")
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--parallel", type=int, default=2)
    p.add_argument("--max-total-optimizer-calls", type=int)
    p.add_argument("--max-total-decision-calls", type=int)
    p.add_argument("--window-size", type=int, default=70)
    p.add_argument("--mode", help="study mode subdirectory to read (default: first with results)")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--poll-seconds", type=float, default=1.0, help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if args.parallel < 1 or args.window_size < 1:
        p.error("--parallel and --window-size must be at least 1")
    base_args = load_base_args(args.base_args_file)
    try:
        check_base_args(base_args)
    except ValueError as error:
        p.error(str(error))
    seeds = replicate_seeds(args.replicates, args.seeds.split(",") if args.seeds else None, args.base_seed)
    if len(set(seeds)) != len(seeds):
        p.error("seeds must be distinct")
    if args.dry_run:
        for n, seed in enumerate(seeds, 1):
            print(shlex.join(build_command(args.study_script, base_args, args.out_dir / f"rep-{n}", seed)))
        return 0
    args.out_dir.mkdir(parents=True, exist_ok=True)
    exits, notes = run_all(args, base_args, seeds, args.out_dir)
    replicates = {}
    for n, seed in enumerate(seeds, 1):
        name = f"rep-{n}"
        if exits.get(name) == "not launched":
            s = {"status": "incomplete", "reason": "not launched"}
        else:
            s = summarise_replicate(args.out_dir / name, args.window_size, args.mode)
        replicates[name] = {"seed": seed, "exit": exits.get(name), **s}
    summary = {"window_size": args.window_size, "seeds": seeds, "launch_notes": notes,
               "replicates": replicates, "aggregate": aggregate(replicates)}
    (args.out_dir / "replicates-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (args.out_dir / "replicates-summary.md").write_text(render_markdown(summary))
    print(render_markdown(summary))
    bad = [n for n, c in exits.items() if isinstance(c, int) and c != 0]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
