#!/usr/bin/env python3
"""A catalog of recorded editorial runs: record every run, publish only the few that explain something.

A recording is a directory ``<catalog>/<id>/`` holding ``recording.json`` (schema ``cyclotron-recording/v1``:
title, summary, what to notice, setup, results, cost) and ``fixture.json`` (the schema-2 playback export made by
``export_editorial_run_fixture``). ``add`` always creates a draft; ``publish`` validates and flips the status;
``export-published`` writes only published recordings, without the local-only ``source`` block, for the site.
No model calls are made and no environment values are read.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import math
import os
import re
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
SCHEMA = "cyclotron-recording/v1"
INDEX_SCHEMA = "cyclotron-recordings-index/v1"
MARKER = ".cyclotron-recordings"
PREDICTIONS = "predictions.jsonl"
STATUSES = ("draft", "published", "retired")
ID_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
DEFAULT_WINDOW = 70  # the replicate harness's window
SCENARIO = "run"
REVIEW_SUMMARIES = {
    "all": "Every story is reviewed.",
    "reject_half": "Every story the model calls publish is reviewed, and half of the stories it calls reject.",
    "casual_ten_percent": "One story in ten is reviewed.",
    "first_50_all_then_half": "Every story for the first 50 stories, then half of the stories.",
    "first_100_all_then_half": "Every story for the first 100 stories, then half of the stories.",
    "publish_priority_taper": "For the first 50 stories, every story the model calls publish and half of those it calls "
                              "reject; after that, half of the publishes and a quarter of the rejects.",
}


def review_summary(name):
    """The plain sentence for a policy name; the exporter's hyphenated names read as the underscore ones."""
    return REVIEW_SUMMARIES.get(str(name).replace("-", "_"))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return sys.modules[name]


exporter = _load("export_editorial_run_fixture")
replicates = _load("replicate_study")
study_constants = None


def study_baseline_rubric():
    """The study's curated BASELINE_RUBRIC constant, read from its source without importing the study."""
    global study_constants
    if study_constants is None:
        import ast
        tree = ast.parse((SCRIPTS / "run_editorial_prequential_study.py").read_text())
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "BASELINE_RUBRIC" for t in node.targets):
                study_constants = ast.literal_eval(node.value)
                break
        else:
            raise SystemExit("BASELINE_RUBRIC not found in run_editorial_prequential_study.py")
    return study_constants


# ------------------------------------------------------------------ file helpers

def dumps(value):
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def recording_dir(catalog, rid):
    return Path(catalog) / rid


def load_recording(catalog, rid):
    path = recording_dir(catalog, rid) / "recording.json"
    if not path.is_file():
        raise SystemExit(f"no recording {rid!r} in {catalog}")
    return json.loads(path.read_text())


def save_recording(catalog, rec):
    (recording_dir(catalog, rec["id"]) / "recording.json").write_text(dumps(rec))


def all_recordings(catalog):
    root = Path(catalog)
    if not root.is_dir():
        return []
    return [json.loads(p.read_text()) for p in sorted(root.glob("*/recording.json"))]


# ------------------------------------------------------------------ validation

def _num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_recording(rec, *, for_publish=False, directory=None):
    """A list of problems (empty when the recording is valid)."""
    problems = []
    if rec.get("schema") != SCHEMA:
        problems.append(f"schema must be {SCHEMA}")
    if not isinstance(rec.get("id"), str) or not ID_PATTERN.match(rec.get("id") or ""):
        problems.append("id must be a kebab-case slug")
    for key in ("title", "summary", "created", "fixture"):
        if not isinstance(rec.get(key), str) or not rec[key].strip():
            problems.append(f"{key} is required")
    if rec.get("status") not in STATUSES:
        problems.append(f"status must be one of {', '.join(STATUSES)}")
    if not isinstance(rec.get("featured"), bool):
        problems.append("featured must be true or false")
    if not isinstance(rec.get("notice"), list) or not all(isinstance(n, str) and n.strip() for n in rec.get("notice") or []):
        problems.append("notice must be a list of non-empty strings")
    if not isinstance(rec.get("tags"), list) or not all(isinstance(t, str) and t.strip() for t in rec.get("tags") or []):
        problems.append("tags must be a list of non-empty strings")
    if not isinstance(rec.get("engine"), dict) or "commit" not in rec["engine"]:
        problems.append("engine.commit is required (a sha, or null when unknown)")
    setup = rec.get("setup")
    if not isinstance(setup, dict):
        problems.append("setup is required")
    else:
        for key in ("decision_model", "decision_provider", "optimizer_model", "objective"):
            if not isinstance(setup.get(key), str) or not setup[key]:
                problems.append(f"setup.{key} is required")
        if setup.get("starting_rubric") not in ("blank", "curated", "custom"):
            problems.append("setup.starting_rubric must be blank, curated or custom")
        elif setup["starting_rubric"] != "blank" and not setup.get("starting_rubric_text"):
            problems.append("setup.starting_rubric_text is required when the starting rubric is not blank")
        if not isinstance(setup.get("parameters"), dict):
            problems.append("setup.parameters is required")
        corpus = setup.get("corpus")
        if not isinstance(corpus, dict) or not corpus.get("id") or not isinstance(corpus.get("stories"), int):
            problems.append("setup.corpus needs id and stories")
        if not isinstance(setup.get("window_size"), int) or setup["window_size"] < 1:
            problems.append("setup.window_size must be a positive integer")
    benchmark = rec.get("benchmark")
    if benchmark is not None and not (isinstance(benchmark, dict) and isinstance(benchmark.get("comparable", True), bool)):
        problems.append("benchmark must be an object")
    review = setup.get("review") if isinstance(setup, dict) else None
    if review is not None:  # optional: older recordings omit it and mean every story was reviewed
        if not isinstance(review, dict):
            problems.append("setup.review must be an object")
        else:
            for key in ("policy", "summary"):
                if not isinstance(review.get(key), str) or not review[key].strip():
                    problems.append(f"setup.review.{key} is required")
            counts = [review.get("stories_reviewed"), review.get("stories_total")]
            if not all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in counts):
                problems.append("setup.review needs stories_reviewed and stories_total as non-negative integers")
            elif counts[0] > counts[1]:
                problems.append("setup.review.stories_reviewed cannot exceed stories_total")
    results = rec.get("results")
    if not isinstance(results, dict) or not isinstance(results.get("windows"), list) or not results["windows"]:
        problems.append("results.windows is required")
    else:
        for window in results["windows"]:
            if not all(_num(window.get(k)) for k in ("first_cycle", "last_cycle", "cyclotron_accuracy", "decision_model_accuracy")):
                problems.append("every window needs cycles and both accuracies")
                break
        for key in ("final_window_accuracy",):
            if not _num(results.get(key)):
                problems.append(f"results.{key} is required")
    cost = rec.get("cost")
    if not isinstance(cost, dict) or not _num(cost.get("usd_total")):
        problems.append("cost.usd_total is required")
    if not isinstance(rec.get("source"), dict):
        problems.append("source is required (it is dropped on export)")
    if for_publish:
        if not rec.get("notice"):
            problems.append("publish needs at least one notice bullet")
        corpus = (setup or {}).get("corpus") or {}
        if not isinstance(corpus.get("label_provenance"), str) or not corpus["label_provenance"].strip():
            problems.append("publish needs setup.corpus.label_provenance")
        if isinstance(results, dict) and any(not _num(w.get("majority_baseline")) for w in results.get("windows") or []):
            problems.append("publish needs a majority_baseline on every window")
        if not isinstance(cost, dict) or not isinstance(cost.get("price_basis"), str) or not cost["price_basis"].strip():
            problems.append("publish needs cost.price_basis")
        if not (isinstance(rec.get("fixture_sha256"), str) and rec["fixture_sha256"]):
            problems.append("publish needs fixture_sha256")
        elif directory is not None:
            fixture = Path(directory) / (rec.get("fixture") or "fixture.json")
            if not fixture.is_file():
                problems.append(f"fixture file {fixture.name} is missing")
            elif sha256_file(fixture) != rec["fixture_sha256"]:
                problems.append("fixture.json does not match the recorded fixture_sha256")
        predictions = rec.get("predictions_sha256")
        if predictions is not None and directory is not None:
            path = Path(directory) / PREDICTIONS
            if not path.is_file():
                problems.append(f"{PREDICTIONS} is missing")
            elif sha256_file(path) != predictions:
                problems.append(f"{PREDICTIONS} does not match the recorded predictions_sha256")
    return problems


def require_valid(rec, **kwargs):
    problems = validate_recording(rec, **kwargs)
    if problems:
        raise SystemExit("recording is not valid:\n" + "\n".join(f"  - {p}" for p in problems))


# ------------------------------------------------------------------ comparability

LABELS = ("publish", "reject")


def canonical_sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def fingerprint_inputs(setup, cycles):
    """Exactly what makes two runs the same benchmark. The decision model and its revision are deliberately absent."""
    params = setup.get("parameters") or {}
    return {"corpus_sha256": (setup.get("corpus") or {}).get("sha256"), "window_size": setup.get("window_size"),
            "review_policy": (setup.get("review") or {}).get("policy", "all"),
            "rubric_trigger": {k: params.get(k) for k in ("rubric_changes_every", "rubric_trigger_basis", "max_rubric_optimizations")},
            "optimizer_model": setup.get("optimizer_model"), "selection_policy_primary": setup.get("objective"),
            "starting_rubric": setup.get("starting_rubric"), "cycles": cycles}


def compute_benchmark(setup, cycles, ids_known=True):
    inputs = fingerprint_inputs(setup, cycles)
    reason = None
    if not inputs["corpus_sha256"]:
        reason = "the corpus sha256 is unknown"
    elif not ids_known:
        reason = "story ids are not recoverable for this recording"
    if not inputs["corpus_sha256"]:
        return {"id": None, "fingerprint": None, "inputs": inputs, "comparable": False, "reason": reason}
    fingerprint = canonical_sha(inputs)
    out = {"id": f"{setup['corpus']['id']}-{fingerprint[:8]}", "fingerprint": fingerprint, "inputs": inputs,
           "comparable": reason is None}
    if reason:
        out["reason"] = reason
    return out


def public_decision_model(protocol):
    """The model name safe to publish; a Foundry deployment label is private, the model it serves is not."""
    return "microsoft-decision-1" if protocol.get("decision_provider") == "foundry" else protocol.get("decision_model")


def decision_model_detail(setup_or_protocol, card_path):
    revision = None
    try:
        argv = json.loads(Path(card_path).read_text()).get("argv") or []
        for i, item in enumerate(argv):
            if item == "--decision-revision" and i + 1 < len(argv):
                revision = argv[i + 1]
            elif isinstance(item, str) and item.startswith("--decision-revision="):
                revision = item.split("=", 1)[1]
    except (OSError, ValueError, AttributeError):
        pass
    provider = setup_or_protocol.get("decision_provider")
    return {"name": public_decision_model(setup_or_protocol), "revision": revision, "provider": provider,
            "hosting": "local" if provider == "kev" else "hosted"}


def card_for(run_dir):
    run_dir = Path(run_dir)
    return run_dir.with_name(run_dir.name + ".card.json")


def prediction_rows(fixture, ids=None):
    """One row per exported cycle. gold is the revealed label ('actual'); null when the run did not reveal it."""
    rows = []
    for c in fixture["scenarios"][SCENARIO]["cycles"]:
        raw = c.get("decision_model_probabilities")
        story = (c.get("article") or {}).get("id") or (ids or {}).get(c["n"])
        rows.append({"n": c["n"], "id": story, "gold": c.get("actual"),
                     "raw_label": exporter.decision_model_choice(raw, LABELS) if raw else None, "raw_probs": raw,
                     "final_label": c.get("predicted"), "final_probs": c.get("probabilities"),
                     "reviewed": bool(c.get("feedback_revealed"))})
    return rows


def write_predictions(path, rows):
    Path(path).write_text("".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in rows))


def read_predictions(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def publish_metrics(rows, key):
    """Precision, recall and F1 for the positive class 'publish' over rows with a gold label; null when undefined."""
    tp = sum(r["gold"] == "publish" and r[key] == "publish" for r in rows)
    fp = sum(r["gold"] != "publish" and r["gold"] is not None and r[key] == "publish" for r in rows)
    fn = sum(r["gold"] == "publish" and r[key] != "publish" for r in rows)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = 2 * precision * recall / (precision + recall) if precision is not None and recall is not None and precision + recall else None
    rnd = lambda v: None if v is None else round(v, 3)
    return {"precision": rnd(precision), "recall": rnd(recall), "f1": rnd(f1)}


def accuracy(rows, key):
    scored = [r for r in rows if r["gold"] is not None and r[key] is not None]
    return round(sum(r[key] == r["gold"] for r in scored) / len(scored), 3) if scored else None


def add_classification_results(results, rows):
    """Additive: per-window and overall publish precision/recall/F1 (final and raw) and overall accuracies."""
    for w in results["windows"]:
        part = [r for r in rows if w["first_cycle"] <= r["n"] <= w["last_cycle"]]
        w["final_publish"] = publish_metrics(part, "final_label")
        w["raw_publish"] = publish_metrics(part, "raw_label")
    results["final_publish"] = publish_metrics(rows, "final_label")
    results["raw_publish"] = publish_metrics(rows, "raw_label")
    results["final_accuracy"] = accuracy(rows, "final_label")
    results["raw_accuracy"] = accuracy(rows, "raw_label")


def mcnemar_exact(only_a, only_b):
    """Two-sided exact binomial p-value from the discordant counts (A right/B wrong, A wrong/B right)."""
    n = only_a + only_b
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, k) for k in range(min(only_a, only_b) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


# ------------------------------------------------------------------ add

def starting_rubric(protocol):
    text = protocol.get("baseline_rubric") or ""
    if not text:
        return "blank", None
    return ("curated" if text == study_baseline_rubric() else "custom"), text


def setup_parameters(protocol):
    trigger = protocol.get("rubric_trigger") or {}
    params = {"rubric_changes_every": trigger.get("every"), "rubric_trigger_basis": trigger.get("basis"),
              "max_rubric_optimizations": trigger.get("max_attempts")}
    for key in ("optimize_every", "retrain_every"):
        if key in protocol:
            params[key] = protocol[key]
    screening = protocol.get("idea_screening")
    params["idea_screening"] = screening if isinstance(screening, dict) and screening.get("enabled") else None
    if isinstance(protocol.get("selection_policy"), dict):
        params["selection_policy"] = protocol["selection_policy"]
    return params


def build_fixture(run_dir, mode_dir, manifest_path, corpus_path, out, *, cycles, window, decision_price, optimizer_price):
    args = ["--output", str(out), "--corpus-manifest", str(manifest_path), "--corpus", str(corpus_path),
            "--scenario", f"{SCENARIO}={mode_dir}", "--window-size", str(window),
            "--decision-model-price", decision_price, "--optimizer-price", optimizer_price,
            "--title", "Recorded run"]
    if cycles is not None:
        args += ["--cycles", str(cycles)]
    with contextlib.redirect_stdout(io.StringIO()):
        exporter.main(args)


def compute_results(fixture, mode_dir, window):
    scenario = fixture["scenarios"][SCENARIO]
    cycles = scenario["cycles"]
    windows = []
    for w in scenario["windows"]:
        # results.json has a human label only for reviewed stories; the fixture's per-class "actual" counts
        # come from the evaluator-only reference labels and cover every story in the window.
        actual = [v.get("actual") for v in (w["cyclotron"].get("per_class") or {}).values()]
        total = w["cyclotron"].get("count") or 0
        baseline = max(actual) / total if total and actual and all(isinstance(x, int) for x in actual) else None
        windows.append({"first_cycle": w["first_cycle"], "last_cycle": w["last_cycle"],
                        "cyclotron_accuracy": round(w["cyclotron"]["accuracy"], 3),
                        "decision_model_accuracy": round(w["decision_model"]["accuracy"], 3),
                        "majority_baseline": None if baseline is None else round(baseline, 3)})
    events = replicates.read_events(mode_dir / "runtime.sqlite3")
    activations = [c for c in replicates.activation_cycles(events) if c <= len(cycles)]
    later = [w["cyclotron_accuracy"] for w in windows[1:]]
    return {"windows": windows, "first_rubric_cycle": activations[0] if activations else None,
            "final_window_accuracy": windows[-1]["cyclotron_accuracy"],
            "post_warm_up_accuracy": round(replicates.mean(later), 3) if later else None}


def compute_cost(fixture):
    scenario = fixture["scenarios"][SCENARIO]
    total = scenario["usage_total"]
    basis = fixture["attribution"]["price_basis"]
    line = (f"{basis['basis']}; decision model {basis['decision_model']['model']} ({basis['decision_model']['source']}); "
            f"optimizer {basis['optimizer']['model']} ({basis['optimizer']['source']})")
    return {"usd_total": total["usd"], "decision_model_usd": total["decision_model"]["usd"],
            "optimizer_usd": total["optimizer"]["usd"], "decision_requests": total["decision_model"]["requests"],
            "optimizer_requests": total["optimizer"]["requests"], "price_basis": line}


def compute_review(fixture, protocol, mode_dir):
    """How much of the run a person reviewed: the policy name, a plain sentence and the realized counts."""
    scenario = fixture["scenarios"][SCENARIO]
    policy = scenario.get("feedback_policy") if isinstance(scenario.get("feedback_policy"), dict) else {}
    modes = protocol.get("modes")
    name = policy.get("mode") or (modes[0] if isinstance(modes, list) and len(modes) == 1 else None) or mode_dir.name
    summary = policy.get("summary") if isinstance(policy.get("summary"), str) and policy["summary"].strip() else None
    summary = summary or review_summary(name) or f"Stories are reviewed under the {policy.get('display_name') or name} policy."
    cycles = scenario["cycles"]
    return {"policy": name, "summary": summary,
            "stories_reviewed": sum(bool(c.get("feedback_revealed")) for c in cycles), "stories_total": len(cycles)}


def cmd_add(a):
    if not ID_PATTERN.match(a.id):
        raise SystemExit("--id must be a kebab-case slug")
    target = recording_dir(a.catalog, a.id)
    if target.exists() and not a.replace:
        raise SystemExit(f"recording {a.id!r} already exists (use --replace)")
    run_dir = Path(a.run_dir)
    try:
        protocol = json.loads((run_dir / "protocol.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read {run_dir / 'protocol.json'}: {exc}") from exc
    mode_dir = replicates.find_mode_dir(run_dir)
    if not (mode_dir / "results.json").is_file():
        raise SystemExit(f"no results.json under {run_dir}")
    window = a.window_size or DEFAULT_WINDOW
    decision_price = a.decision_model_price or ({"foundry": "microsoft-decision-1"}.get(protocol.get("decision_provider"), protocol.get("decision_provider"))
                                                 if protocol.get("decision_provider") in ("jev", "kev", "foundry") else None) \
        or "gpt-4.1-mini"
    optimizer_price = a.optimizer_price or (protocol.get("optimizer_model") if protocol.get("optimizer_model") in exporter.PRICES
                                            else "gpt-4.1-mini")
    manifest = json.loads(Path(a.corpus_manifest).read_text())
    with tempfile.TemporaryDirectory() as tmp:
        built = Path(tmp) / "fixture.json"
        build_fixture(run_dir, mode_dir, a.corpus_manifest, a.corpus, built, cycles=a.cycles, window=window,
                      decision_price=decision_price, optimizer_price=optimizer_price)
        fixture = json.loads(built.read_text())
        rubric_kind, rubric_text = starting_rubric(protocol)
        commit = a.engine_commit or protocol.get("engine_commit")
        if not commit:
            print("warning: no engine commit recorded; pass --engine-commit to set it", file=sys.stderr)
        provenance = manifest.get("label_provenance") or fixture["disclosure"]
        corpus = {"id": manifest.get("corpus") or manifest.get("dataset"), "stories": manifest.get("actual_count"),
                  "label_provenance": provenance}
        sha = manifest.get("operational_sha256") or manifest.get("sha256")
        if sha:
            corpus["sha256"] = sha
        setup = {"decision_model": public_decision_model(protocol), "decision_provider": protocol.get("decision_provider"),
                 "optimizer_model": protocol.get("optimizer_model"),
                 "objective": (protocol.get("selection_policy") or {}).get("primary"),
                 "starting_rubric": rubric_kind, "parameters": setup_parameters(protocol), "corpus": corpus,
                 "window_size": window}
        setup["review"] = compute_review(fixture, protocol, mode_dir)
        setup["decision_model_detail"] = decision_model_detail(protocol, card_for(run_dir))
        if rubric_text:
            setup["starting_rubric_text"] = rubric_text
        rec = {"schema": SCHEMA, "id": a.id, "title": a.title, "summary": a.summary, "notice": list(a.notice or []),
               "tags": list(a.tag or []), "status": "draft", "featured": False, "created": date.today().isoformat(),
               "engine": {"commit": commit or None}, "setup": setup,
               "results": compute_results(fixture, mode_dir, window), "cost": compute_cost(fixture),
               "fixture": "fixture.json", "fixture_sha256": sha256_file(built),
               "source": {"run_dir": str(run_dir.resolve()), "replicate_of": a.replicate_of}}
        rows = prediction_rows(fixture)
        add_classification_results(rec["results"], rows)
        rec["benchmark"] = compute_benchmark(setup, len(rows), ids_known=all(r["id"] for r in rows))
        require_valid(rec)
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        shutil.copyfile(built, target / "fixture.json")
        write_predictions(target / PREDICTIONS, rows)
        rec["predictions_sha256"] = sha256_file(target / PREDICTIONS)
        save_recording(a.catalog, rec)
    print(f"added {a.id} (draft) in {a.catalog}")


# ------------------------------------------------------------------ list / show / status

def pct(value):
    return "-" if value is None else f"{100 * value:.1f}%"


def table(rows):
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows)


def review_of(rec):
    """The recorded review block, or the every-story default for recordings made before it existed."""
    review = (rec.get("setup") or {}).get("review")
    if review:
        return review
    total = (rec.get("setup") or {}).get("corpus", {}).get("stories")
    return {"policy": "all", "summary": REVIEW_SUMMARIES["all"], "stories_reviewed": total, "stories_total": total}


def review_cell(rec):
    review = review_of(rec)
    if (rec.get("setup") or {}).get("review") is None:
        return "all"
    return f"{review['policy']} {review['stories_reviewed']}/{review['stories_total']}"


def cmd_list(a):
    rows = [("id", "status", "featured", "title", "final window", "first rubric", "reviewed", "cost")]
    for rec in all_recordings(a.catalog):
        if a.status and rec["status"] != a.status:
            continue
        if a.tag and a.tag not in rec["tags"]:
            continue
        first = rec["results"].get("first_rubric_cycle")
        rows.append((rec["id"], rec["status"], "yes" if rec["featured"] else "-", rec["title"],
                     pct(rec["results"].get("final_window_accuracy")), "-" if first is None else f"cycle {first}", review_cell(rec),
                     f"${rec['cost']['usd_total']:.3f}"))
    print(table(rows))


def cmd_show(a):
    print(dumps(load_recording(a.catalog, a.id)), end="")


def cmd_publish(a):
    rec = load_recording(a.catalog, a.id)
    if rec["status"] == "retired":
        raise SystemExit("a retired recording cannot be published; unpublish it first")
    rec["status"] = "published"
    require_valid(rec, for_publish=True, directory=recording_dir(a.catalog, a.id))
    if a.featured:
        for other in all_recordings(a.catalog):
            if other["id"] != a.id and other["featured"]:
                other["featured"] = False
                save_recording(a.catalog, other)
        rec["featured"] = True
    save_recording(a.catalog, rec)
    print(f"published {a.id}" + (" (featured)" if a.featured else ""))


def _set(a, status=None, featured=None):
    rec = load_recording(a.catalog, a.id)
    if status:
        rec["status"] = status
        if status != "published":
            rec["featured"] = False
    if featured is not None:
        rec["featured"] = featured
    save_recording(a.catalog, rec)
    print(f"{a.id}: {rec['status']}, featured={'yes' if rec['featured'] else 'no'}")


# ------------------------------------------------------------------ export

def cmd_export(a):
    published = [r for r in all_recordings(a.catalog) if r["status"] == "published"]
    for rec in published:
        require_valid(rec, for_publish=True, directory=recording_dir(a.catalog, rec["id"]))
    # featured first, then newest created, then id (stable sorts, last key first)
    published.sort(key=lambda r: r["id"])
    published.sort(key=lambda r: r["created"], reverse=True)
    published.sort(key=lambda r: not r["featured"])
    dest = Path(a.to)
    marked = (dest / MARKER).is_file()
    dest.mkdir(parents=True, exist_ok=True)
    keep = {r["id"] for r in published}
    removed = []
    if marked:
        for child in sorted(dest.iterdir()):
            if child.is_dir() and (child / "recording.json").is_file() and child.name not in keep:
                shutil.rmtree(child)
                removed.append(child.name)
    for rec in published:
        out = dest / rec["id"]
        if out.exists():
            shutil.rmtree(out)
        out.mkdir()
        public = {k: v for k, v in rec.items() if k != "source"}
        (out / "recording.json").write_text(dumps(public))
        shutil.copyfile(recording_dir(a.catalog, rec["id"]) / rec["fixture"], out / rec["fixture"])
        if rec.get("predictions_sha256"):
            shutil.copyfile(recording_dir(a.catalog, rec["id"]) / PREDICTIONS, out / PREDICTIONS)
    index = {"schema": INDEX_SCHEMA, "recordings": [
        {k: r[k] for k in ("id", "title", "summary", "tags", "featured")} for r in published]}
    (dest / "index.json").write_text(dumps(index))
    (dest / MARKER).write_text("Managed by scripts/recordings.py export-published; stale recordings here may be removed.\n")
    note = "" if marked or not any(p.is_dir() for p in dest.iterdir() if p.name not in keep) else \
        " (no marker file existed, so nothing was removed)"
    print(f"exported {len(published)} recording(s) to {dest}; removed {len(removed)} stale" + note)


# ------------------------------------------------------------------ compare

ATOMIC = ("idea_screening", "selection_policy")  # compared whole, not field by field


def flatten(value, prefix=""):
    if isinstance(value, dict) and value and prefix.rstrip(".").rsplit(".", 1)[-1] not in ATOMIC:
        out = {}
        for k, v in value.items():
            out.update(flatten(v, f"{prefix}{k}."))
        return out
    return {prefix[:-1]: value}


def setup_diff(recs, ids):
    setups = []
    for rec in recs:
        flat = flatten({k: v for k, v in rec["setup"].items() if k not in ("corpus", "review")}
                       | {"corpus": rec["setup"]["corpus"].get("id")})
        flat["review.policy"] = review_of(rec)["policy"]
        flat.pop("corpus.label_provenance", None)
        setups.append(flat)
    keys = sorted({k for s in setups for k in s})
    differing = [k for k in keys if len({json.dumps(s.get(k), sort_keys=True) for s in setups}) > 1]
    fmt = lambda v: "-" if v is None else (v if isinstance(v, str) else json.dumps(v, sort_keys=True))
    clip = lambda s: s if len(s) <= 40 else s[:37] + "..."
    if not differing:
        return "(none)", fmt
    return table([("setting", *ids)] + [(k, *(clip(fmt(s.get(k))) for s in setups)) for k in differing]), fmt


def benchmark_problem(recs, ids):
    """Why these recordings cannot be compared as one benchmark, or None."""
    marks = [r.get("benchmark") for r in recs]
    missing = [i for i, m in zip(ids, marks) if not m or not m.get("id")]
    if missing:
        return f"no benchmark id for {', '.join(missing)} (run `recordings.py upgrade ID`, or the corpus sha256 is unknown)"
    bad = [i for i, m in zip(ids, marks) if m.get("comparable") is False]
    if bad:
        return "; ".join(f"{i} is not comparable: {m.get('reason')}" for i, m in zip(ids, marks) if m.get("comparable") is False)
    if len({m["id"] for m in marks}) > 1:
        a, b = marks[0].get("inputs") or {}, marks[1].get("inputs") or {}
        diffs = [f"  {k}: {a.get(k)!r} vs {b.get(k)!r}" for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]
        return (f"different benchmarks ({marks[0]['id']} vs {marks[1]['id']}); fields that differ:\n" + "\n".join(diffs or ["  (inputs not recorded)"]))
    return None


def paired_rows(catalog, rec):
    path = recording_dir(catalog, rec["id"]) / PREDICTIONS
    if path.is_file():
        return read_predictions(path)
    fixture = json.loads((recording_dir(catalog, rec["id"]) / rec["fixture"]).read_text())
    return prediction_rows(fixture)


def cmd_compare(a):
    recs = [load_recording(a.catalog, rid) for rid in a.ids]
    if len(recs) != 2:
        raise SystemExit("compare takes exactly two recording ids")
    problem = benchmark_problem(recs, a.ids)
    if problem and not a.force:
        raise SystemExit(f"cannot compare: {problem}\n(use --force to compare anyway)")
    if problem:
        print(f"WARNING: THESE RECORDINGS ARE NOT THE SAME BENCHMARK; differences below may not be due to the model.\n{problem}\n")
    diff_text, fmt = setup_diff(recs, a.ids)
    print("Setup differences")
    print(diff_text)
    print("\nWindow accuracy: final / raw decision model / majority baseline")
    rows = [("window", *a.ids)]
    for i in range(max(len(r["results"]["windows"]) for r in recs)):
        cells, span = [], None
        for rec in recs:
            ws = rec["results"]["windows"]
            if i < len(ws):
                w = ws[i]
                span = span or f"{w['first_cycle']}-{w['last_cycle']}"
                cells.append(f"{pct(w['cyclotron_accuracy'])} / {pct(w['decision_model_accuracy'])} / {pct(w.get('majority_baseline'))}")
            else:
                cells.append("-")
        rows.append((span, *cells))
    rows.append(("first rubric", *(fmt(r["results"].get("first_rubric_cycle")) for r in recs)))
    rows.append(("cost", *(f"${r['cost']['usd_total']:.3f}" for r in recs)))
    print(table(rows))
    data = [paired_rows(a.catalog, r) for r in recs]
    summary = [("overall", *a.ids)]
    for label, key in (("final accuracy", "final_accuracy"), ("raw accuracy", "raw_accuracy")):
        summary.append((label, *(pct(accuracy(d, key.replace("accuracy", "label"))) for d in data)))
    for label, _ in (("final publish recall", "final_publish"), ("final publish precision", "final_publish"),
                       ("raw publish recall", "raw_publish"), ("raw publish precision", "raw_publish")):
        metric = "recall" if "recall" in label else "precision"
        which = "final_label" if label.startswith("final") else "raw_label"
        summary.append((label, *(pct(publish_metrics(d, which)[metric]) for d in data)))
    print("\n" + table(summary))
    keyed = []
    for d in data:
        use_id = all(r.get("id") for r in d)
        keyed.append({(r["id"] if use_id else r["n"]): r for r in d})
    if not all(r.get("id") for d in data for r in d):
        print("\nNote: story ids are missing for at least one recording; pairing by cycle number.")
    shared = [k for k in keyed[0] if k in keyed[1]]
    if len(shared) != len(keyed[0]) or len(shared) != len(keyed[1]):
        print(f"\nNote: only {len(shared)} of {len(keyed[0])} / {len(keyed[1])} stories are in both recordings.")
    disagreements = []
    print(f"\nPaired comparison over {len(shared)} stories (exact McNemar, two-sided)")
    for label, key in (("final decisions", "final_label"), ("raw decision-model answers", "raw_label")):
        pairs = [(keyed[0][k], keyed[1][k]) for k in shared if keyed[0][k]["gold"] is not None and keyed[0][k][key] and keyed[1][k][key]]
        only_a = sum(x[key] == x["gold"] and y[key] != y["gold"] for x, y in pairs)
        only_b = sum(x[key] != x["gold"] and y[key] == y["gold"] for x, y in pairs)
        differ = sum(x[key] != y[key] for x, y in pairs)
        print(f"  {label}: {len(pairs)} scored; only {a.ids[0]} right {only_a}, only {a.ids[1]} right {only_b}; "
              f"p = {mcnemar_exact(only_a, only_b):.4f}; stories where the two answers differ: {differ}")
        if key == "final_label":
            disagreements = [(x, y) for x, y in pairs if x[key] != y[key]]
    if a.disagreements:
        print(f"\nFirst {a.disagreements} disagreements on final decisions: story, gold, {a.ids[0]}, {a.ids[1]}")
        for x, y in disagreements[:a.disagreements]:
            print(f"  {x.get('id') or x['n']}  gold={x['gold']}  {x['final_label']}  {y['final_label']}")


# ------------------------------------------------------------------ upgrade

def cmd_upgrade(a):
    rec = load_recording(a.catalog, a.id)
    directory = recording_dir(a.catalog, a.id)
    fixture = json.loads((directory / rec["fixture"]).read_text())
    src = (rec.get("source") or {}).get("run_dir")
    run_dir = a.run_dir or (Path(src) if src else None)
    setup = rec["setup"]
    ids = None
    cycles = fixture["scenarios"][SCENARIO]["cycles"]
    if not all((c.get("article") or {}).get("id") for c in cycles) and run_dir:
        try:
            mode_dir = replicates.find_mode_dir(Path(run_dir))
            results = json.loads((mode_dir / "results.json").read_text())
            ids = {c["cycle_number"]: c["item_id"] for c in results["cycles"]}
        except (OSError, ValueError, KeyError):
            ids = None
    rows = prediction_rows(fixture, ids)
    if run_dir and Path(run_dir).is_dir():
        protocol = {}
        try:
            protocol = json.loads((Path(run_dir) / "protocol.json").read_text())
        except (OSError, ValueError):
            pass
        corpus = setup["corpus"]
        if not corpus.get("sha256") and protocol.get("operational_sha256"):
            corpus["sha256"] = protocol["operational_sha256"]
        setup["decision_model_detail"] = decision_model_detail(setup, card_for(run_dir))
    elif "decision_model_detail" not in setup:
        setup["decision_model_detail"] = decision_model_detail(setup, "/nonexistent")
    add_classification_results(rec["results"], rows)
    rec["benchmark"] = compute_benchmark(setup, len(rows), ids_known=all(r["id"] for r in rows))
    write_predictions(directory / PREDICTIONS, rows)
    rec["predictions_sha256"] = sha256_file(directory / PREDICTIONS)
    save_recording(a.catalog, rec)
    b = rec["benchmark"]
    print(f"upgraded {a.id}: benchmark {b['id']}" + ("" if b["comparable"] else f" (not comparable: {b['reason']})"))


# ------------------------------------------------------------------ cli

def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--catalog", type=Path, default=os.environ.get("CYCLOTRON_RECORDINGS_CATALOG"),
                        help="catalog directory (default: $CYCLOTRON_RECORDINGS_CATALOG)")
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", parents=[common], help="record a run as a draft")
    add.add_argument("run_dir", type=Path)
    add.add_argument("--id", required=True)
    add.add_argument("--title", required=True)
    add.add_argument("--summary", required=True)
    add.add_argument("--notice", action="append", default=[])
    add.add_argument("--tag", action="append", default=[])
    add.add_argument("--corpus-manifest", type=Path, required=True)
    add.add_argument("--corpus", type=Path, required=True)
    add.add_argument("--cycles", type=int)
    add.add_argument("--window-size", type=int, help=f"default {DEFAULT_WINDOW}")
    add.add_argument("--optimizer-price", choices=sorted(exporter.PRICES))
    add.add_argument("--decision-model-price", choices=sorted(exporter.PRICES))
    add.add_argument("--engine-commit")
    add.add_argument("--replicate-of")
    add.add_argument("--replace", action="store_true")
    add.set_defaults(func=cmd_add)
    lst = sub.add_parser("list", parents=[common])
    lst.add_argument("--status", choices=STATUSES)
    lst.add_argument("--tag")
    lst.set_defaults(func=cmd_list)
    for name, func in (("show", cmd_show), ("unpublish", lambda a: _set(a, "draft")), ("retire", lambda a: _set(a, "retired")),
                       ("unfeature", lambda a: _set(a, featured=False))):
        p = sub.add_parser(name, parents=[common])
        p.add_argument("id")
        p.set_defaults(func=func)
    pub = sub.add_parser("publish", parents=[common])
    pub.add_argument("id")
    pub.add_argument("--featured", action="store_true")
    pub.set_defaults(func=cmd_publish)
    exp = sub.add_parser("export-published", parents=[common])
    exp.add_argument("--to", type=Path, required=True)
    exp.set_defaults(func=cmd_export)
    cmp_ = sub.add_parser("compare", parents=[common])
    cmp_.add_argument("ids", nargs="+")
    cmp_.add_argument("--disagreements", type=int, default=0, metavar="N", help="list the first N stories where the final decisions differ")
    cmp_.add_argument("--force", action="store_true", help="compare recordings of different benchmarks, with a warning")
    cmp_.set_defaults(func=cmd_compare)
    upg = sub.add_parser("upgrade", parents=[common], help="backfill benchmark, identity and per-story fields on an existing recording")
    upg.add_argument("id")
    upg.add_argument("--run-dir", type=Path, help="the run directory (default: the recording's source.run_dir)")
    upg.set_defaults(func=cmd_upgrade)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.catalog is None:
        raise SystemExit("--catalog is required (or set CYCLOTRON_RECORDINGS_CATALOG)")
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
