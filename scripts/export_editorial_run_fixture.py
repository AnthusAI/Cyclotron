#!/usr/bin/env python3
"""Export a recorded editorial study as the marketing site's playback fixture.

Ported from Cyclotron-web's scripts/extract-editorial-runs.py, which wrote
editorial-run-v1.json; ``--schema-version 1`` reproduces that file byte for
byte from the same recording. Schema 2 accepts any corpus length and adds, per
window of 100 cycles and for both the decision model's own confidence and the
cyclotron's: accuracy, per-class precision, recall and F1, Brier, calibration
error, mean stated confidence, the routing metrics (mistakes in the
least-confident N percent; an auto-approve rule at thresholds 0.75 to 0.95),
optimizer events, and measured model usage and cost at list prices. Totals go
in ``usage_total``; the price basis goes in the attribution.

``--cycles N`` (schema 2) exports only the first N cycles of a longer
recording, N a multiple of 100: windows, usage, cost and wall-clock time count
those cycles alone, and the evaluator-only windows compare the cut's first and
last 100. The fixture records the cut in ``cycle_cut``.

The source result files remain local study artifacts. This exporter emits only
the pre-vote prediction, revealed simulated label, public metrics, lifecycle
outcomes, and source attribution required by the marketing playback.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from decision_flywheel.routing_metrics import Decision, routing  # noqa: E402
from decision_flywheel.run_usage import usage_by_window  # noqa: E402

WINDOW = 100
# List prices, USD per million tokens. gpt-4.1-mini is not in Hard-Decisions'
# engines.yaml, so its source is OpenAI's model page.
PRICES = {
    "gpt-4.1-mini": {"input_usd_per_mtok": 0.40, "cached_input_usd_per_mtok": 0.10, "output_usd_per_mtok": 1.60,
                     "source": "developers.openai.com/api/docs/models/gpt-4.1-mini, read 2026-10-09"},
    "gpt-4.1": {"input_usd_per_mtok": 2.00, "cached_input_usd_per_mtok": 0.50, "output_usd_per_mtok": 8.00,
                "source": "developers.openai.com/api/docs/models/gpt-4.1, read 2026-10-09"},
    "jev": {"input_usd_per_mtok": 0.042, "output_usd_per_mtok": 0.0, "source": "TypeSafe's published price"},
}

RUBRIC_SUMMARY = "Publish articles with a meaningful constructive or public-benefit outcome; reject harm-dominant or routine items."


def public_excerpt(title: str) -> str:
    """Make a readable adapted brief from the public title, not broken raw text.

    Historical dataset text retains partial wiki-markup removal in enough rows
    that presenting it as a verbatim excerpt would be misleading. The source
    URL remains with each card for the complete original reporting.
    """
    return f"Wikinews reports: {title}. The original linked article provides the complete reporting context."


def read(path: Path, count: int) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid results file {path}: {exc}") from exc
    if len(data.get("cycles", [])) != count:
        raise SystemExit(f"{path}: requires exactly {count} completed cycles")
    if data.get("disclosure", {}).get("operational_items") != count:
        raise SystemExit(f"{path}: expected {count} operational items")
    return data


def parse_scenarios(values: list[str]) -> dict[str, Path]:
    """Accept explicit named scenario directories; never assume a fixed mode set."""
    scenarios: dict[str, Path] = {}
    for value in values:
        name, separator, raw_path = value.partition("=")
        if not separator or not name or not raw_path or name in scenarios:
            raise SystemExit("--scenario must be unique NAME=RUN_DIRECTORY")
        scenarios[name] = Path(raw_path)
    if not scenarios:
        raise SystemExit("at least one --scenario NAME=RUN_DIRECTORY is required")
    return scenarios


def public_feedback_schedule(policy: object) -> dict:
    """Expose a readable schedule without leaking arbitrary runtime config."""
    if not isinstance(policy, dict):
        raise SystemExit("scenario feedback policy is invalid")
    schedule = {
        key: policy[key]
        for key in ("display_name", "summary", "mode", "selection_timing", "negative_label")
        if isinstance(policy.get(key), str)
    }
    phases = policy.get("phases")
    if phases is not None:
        if not isinstance(phases, list):
            raise SystemExit("feedback-policy phases must be a list")
        public_phases = []
        for phase in phases:
            if not isinstance(phase, dict):
                raise SystemExit("feedback-policy phase is invalid")
            public_phase = {key: phase[key] for key in ("start_cycle", "end_cycle") if key in phase and (isinstance(phase[key], int) or phase[key] is None)}
            label_rates = phase.get("predicted_label_rates")
            if not isinstance(label_rates, dict) or not all(isinstance(label, str) and isinstance(rate, (int, float)) and 0 <= rate <= 1 for label, rate in label_rates.items()):
                raise SystemExit("feedback-policy predicted_label_rates is invalid")
            public_phase["predicted_label_rates"] = label_rates
            public_phases.append(public_phase)
        schedule["phases"] = public_phases
    return schedule


def read_corpus(path: Path, manifest: dict) -> tuple[dict[str, dict], dict[str, str]]:
    """Read only the public attribution/context needed for the demo cards."""
    records: dict[str, dict] = {}
    reference_labels: dict[str, str] = {}
    try:
        lines = path.read_text().splitlines()
        for line in lines:
            record = json.loads(line)
            item_id = record.get("id")
            if not isinstance(item_id, str) or item_id in records:
                raise ValueError("invalid or duplicate corpus item id")
            title = record.get("title")
            records[item_id] = {
                "title": title,
                "excerpt": public_excerpt(title) if isinstance(title, str) else None,
                "source_url": record.get("source_url"),
                "license": record.get("license"),
                "license_url": record.get("license_url"),
                "modification_notice": "Brief adapted and shortened for this demo; see the original Wikinews article for the complete work.",
            }
            reference_label = record.get("simulated_label")
            if reference_label not in {"publish", "reject"}:
                raise ValueError("missing frozen simulated reference label")
            reference_labels[item_id] = reference_label
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid reviewed corpus {path}: {exc}") from exc
    if len(records) != manifest.get("actual_count"):
        raise SystemExit(f"{path}: expected exactly {manifest.get('actual_count')} reviewed corpus records")
    if any(not all(isinstance(value, str) and value for value in article.values()) for article in records.values()):
        raise SystemExit(f"{path}: missing public title, excerpt, source URL, or license attribution")
    if manifest.get("actual_count") != len(records):
        raise SystemExit(f"{path}: corpus count does not match manifest")
    return records, reference_labels


def public_events(outcomes: list[dict]) -> list[dict]:
    """Retain lifecycle state only; never export optimizer text or examples."""
    events = []
    for outcome in outcomes:
        result = outcome.get("result") or {}
        stage = result.get("stage")
        if not stage:
            continue
        if stage == "classifier":
            events.append({"type": "head", "outcome": "promoted" if result.get("promoted") else "rejected", "reason": None})
        else:
            validation = result.get("validation_status")
            event_outcome = "promoted" if result.get("promoted") else "provisional" if validation == "provisional" else "not activated"
            events.append({"type": "proposal", "stage": stage, "outcome": event_outcome, "reason": None})
    return events


def evaluator_window(cycles: list[dict], predictions: dict[int, dict], reference_labels: dict[str, str]) -> dict:
    """Aggregate frozen reference labels for an evaluator-only equal window."""
    values = []
    for cycle in cycles:
        event = predictions[cycle["cycle_number"]]
        actual = reference_labels[cycle["item_id"]]
        probabilities = event["probabilities"]
        brier = sum((probability - (1 if label == actual else 0)) ** 2 for label, probability in probabilities.items())
        values.append((event["confidence"], event["label"] == actual, brier))
    total = len(values)
    ece = 0.0
    for bucket in range(10):
        members = [value for value in values if min(9, int(value[0] * 10)) == bucket]
        if members:
            ece += len(members) / total * abs(sum(value[0] for value in members) / len(members) - sum(value[1] for value in members) / len(members))
    return {
        "count": total,
        "accuracy": sum(value[1] for value in values) / total,
        "brier": sum(value[2] for value in values) / total,
        "ece": ece,
    }


def compact_calibration(provenance: object) -> dict | None:
    """Keep method metadata, never the fitted-head training identifiers."""
    if not isinstance(provenance, dict):
        return None
    return {
        key: provenance[key]
        for key in ("fit_on", "method", "selection_reason")
        if provenance.get(key) is not None
    }


EXPORTED_KINDS = ("prediction", "human-feedback", "decision-response", "optimizer-response", "cycle-started", "cycle-completed")


def read_events(path: Path) -> list[dict]:
    """The runtime events this exporter reads, through a read-only connection."""
    if not path.is_file():
        raise SystemExit(f"missing runtime event log: {path}")
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        events = []
        for (payload,) in connection.execute("SELECT payload FROM runtime_events ORDER BY id"):
            if not any(f'"{kind}"' in payload for kind in EXPORTED_KINDS):
                continue
            event = json.loads(payload)
            if event.get("kind") in EXPORTED_KINDS:
                event.pop("classifier_snapshot", None)
                events.append(event)
    except (sqlite3.Error, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid immutable runtime event log {path}: {exc}") from exc
    finally:
        if "connection" in locals():
            connection.close()
    return events


def read_logged_predictions(path: Path, cycles: list[dict], events: list[dict]) -> dict[int, dict]:
    """Read the immutable pre-feedback prediction events, fail closed on drift.

    These values are deliberately not reconstructed from the final promoted
    head.  The read-only SQLite URI also makes accidental study mutation
    impossible while exporting the demo fixture.
    """

    logged: dict[int, dict] = {}
    for event in events:
        if event.get("kind") != "prediction":
            continue
        n = event.get("cycle_number")
        confidence = event.get("confidence")
        probabilities = event.get("probabilities")
        if not isinstance(n, int) or n in logged:
            raise SystemExit(f"{path}: invalid or duplicate prediction cycle number")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise SystemExit(f"{path}: prediction {n} has no valid logged confidence")
        if not isinstance(probabilities, dict) or not probabilities:
            raise SystemExit(f"{path}: prediction {n} has no logged probabilities")
        if event.get("label") not in probabilities or probabilities[event["label"]] != confidence:
            raise SystemExit(f"{path}: prediction {n} confidence does not match its recorded label probability")
        logged[n] = event

    if set(logged) != set(range(1, len(cycles) + 1)):
        raise SystemExit(f"{path}: expected one logged prediction event for every completed cycle")
    for cycle in cycles:
        event = logged[cycle["cycle_number"]]
        if event.get("cycle_item_id") != cycle.get("item_id") or event.get("label") != cycle.get("predicted_label"):
            raise SystemExit(f"{path}: prediction event does not match frozen result at cycle {cycle['cycle_number']}")
    return logged


def read_logged_feedback(path: Path, cycles: list[dict], rows: list[dict]) -> dict[int, str]:
    """Read the submitted simulated reviewer comment for a revealed vote only."""
    comments: dict[int, str] = {}
    for event in rows:
        if event.get("kind") != "human-feedback" or event.get("action") != "submitted":
            continue
        feedback = event.get("feedback")
        n = event.get("cycle_number")
        comment = feedback.get("edit_comment_value") if isinstance(feedback, dict) else None
        if not isinstance(n, int) or not isinstance(comment, str) or not comment or n in comments:
            raise SystemExit(f"{path}: invalid or duplicate submitted reviewer comment")
        comments[n] = comment
    expected = {cycle["cycle_number"] for cycle in cycles if cycle["feedback_selected"]}
    if set(comments) != expected:
        raise SystemExit(f"{path}: reviewer comments do not match revealed feedback cycles")
    return comments


def compact_cycle(cycle: dict, event: dict, article: dict, comments: dict[int, str]) -> dict:
    metrics = cycle["metrics"]
    return {
        "n": cycle["cycle_number"],
        "predicted": cycle["predicted_label"],
        # Values below are copied from the pre-feedback prediction event.
        "confidence": event["confidence"],
        "probabilities": event["probabilities"],
        "decision_model_probabilities": event.get("decision_model_probabilities"),
        "uncalibrated_probabilities": event.get("uncalibrated_probabilities"),
        "calibration": {
            "temperature": event.get("calibration_temperature"),
            "provenance": compact_calibration(event.get("calibration_provenance")),
            "fitted_head": event.get("fitted_head"),
            "version": event.get("version"),
        },
        "article": article,
        **({"actual": cycle["human_label"]} if cycle["feedback_selected"] else {}),
        **({"simulated_reviewer_rationale": comments[cycle["cycle_number"]]} if cycle["feedback_selected"] else {}),
        "feedback_revealed": cycle["feedback_selected"],
        "feedback_propensity": cycle["feedback_propensity"],
        "partition": cycle["role"],
        "metrics": {
            "count": metrics["count"], "accuracy": metrics["accuracy"],
            "brier": metrics["brier"], "calibration": {"ece": metrics["calibration"]["ece"]},
            "per_class": metrics["per_class"], "confusion_matrix": metrics["confusion_matrix"],
        },
        "events": public_events(cycle["outcomes"]),
        "requests": cycle["requests"],
    }


def _confidence_metrics(rows: list[tuple[str, dict, str]], labels: tuple[str, ...]) -> dict:
    """Accuracy, per-class precision/recall/F1, Brier and ECE for (label, probabilities, actual) rows."""
    total = len(rows)
    confidences = [probabilities[label] for label, probabilities, _ in rows]
    right = [label == actual for label, _, actual in rows]
    ece = 0.0
    for bucket in range(10):
        members = [index for index, value in enumerate(confidences) if min(9, int(value * 10)) == bucket]
        if members:
            ece += len(members) / total * abs(sum(confidences[i] for i in members) / len(members) - sum(right[i] for i in members) / len(members))
    per_class = {}
    for name in labels:
        predicted = sum(label == name for label, _, _ in rows)
        actual = sum(value == name for _, _, value in rows)
        hits = sum(label == name == value for label, _, value in rows)
        precision = hits / predicted if predicted else None
        recall = hits / actual if actual else None
        f1 = 2 * precision * recall / (precision + recall) if precision and recall else (0.0 if predicted or actual else None)
        per_class[name] = {"precision": precision, "recall": recall, "f1": f1, "predicted": predicted, "actual": actual}
    return {
        "count": total,
        "accuracy": sum(right) / total,
        "brier": sum(sum((probabilities.get(name, 0.0) - (name == actual)) ** 2 for name in labels)
                     for _, probabilities, actual in rows) / total,
        "ece": ece,
        "mean_confidence": sum(confidences) / total,
        "per_class": per_class,
    }


def decision_model_choice(probabilities: dict, labels: tuple[str, ...]) -> str:
    """The decision model's own label: its most probable, first declared label on a tie."""
    return max(labels, key=lambda name: probabilities.get(name, 0.0))


def scenario_windows(cycles, predictions, reference_labels, labels, usage) -> list[dict]:
    """Per-100-cycle windows of evaluator-only metrics, routing, optimizer events, and usage."""
    usage_windows = {window["window"]: window for window in usage["windows"]}
    windows = []
    for index in range(0, len(cycles), WINDOW):
        chunk = cycles[index:index + WINDOW]
        cyclotron, decision_model = [], []
        for cycle in chunk:
            event = predictions[cycle["cycle_number"]]
            actual = reference_labels[cycle["item_id"]]
            cyclotron.append((event["label"], event["probabilities"], actual))
            raw = event.get("decision_model_probabilities") or event["probabilities"]
            decision_model.append((decision_model_choice(raw, labels), raw, actual))
        lifecycle = [event for cycle in chunk for event in public_events(cycle["outcomes"])]
        window_usage = usage_windows.get(index // WINDOW + 1, {})
        windows.append({
            "window": index // WINDOW + 1,
            "first_cycle": chunk[0]["cycle_number"], "last_cycle": chunk[-1]["cycle_number"],
            "label_source": "Frozen simulated reference labels, evaluator-only; never learner feedback.",
            "cyclotron": _confidence_metrics(cyclotron, labels),
            "decision_model": _confidence_metrics(decision_model, labels),
            "routing": {
                "cyclotron": routing([Decision(label, probabilities[label], actual) for label, probabilities, actual in cyclotron]),
                "decision_model": routing([Decision(label, probabilities[label], actual) for label, probabilities, actual in decision_model]),
            },
            "feedback_revealed": sum(bool(cycle["feedback_selected"]) for cycle in chunk),
            "optimizer_events": {
                "proposals": sum(event["type"] == "proposal" for event in lifecycle),
                "proposals_promoted": sum(event["type"] == "proposal" and event["outcome"] == "promoted" for event in lifecycle),
                "ml_model_refits": sum(event["type"] == "head" for event in lifecycle),
                "ml_model_refits_promoted": sum(event["type"] == "head" and event["outcome"] == "promoted" for event in lifecycle),
            },
            "model_calls": {"decision_model": window_usage.get("decision_model", {}).get("requests", 0),
                            "optimizer": window_usage.get("optimizer", {}).get("requests", 0)},
            "wall_clock_seconds": window_usage.get("wall_clock_seconds"),
            "usage": {key: window_usage[key] for key in ("decision_model", "optimizer", "usd") if key in window_usage},
        })
    return windows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--corpus-manifest", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--scenario", action="append", default=[], metavar="NAME=RUN_DIRECTORY")
    parser.add_argument("--schema-version", type=int, choices=(1, 2), default=2)
    parser.add_argument("--title", default="Curated editorial demo")
    parser.add_argument("--decision-model-price", choices=sorted(PRICES), default="gpt-4.1-mini")
    parser.add_argument("--optimizer-price", choices=sorted(PRICES), default="gpt-4.1-mini")
    parser.add_argument("--cycles", type=int, default=None,
                        help="export only the first N recorded cycles (schema 2; a multiple of 100)")
    args = parser.parse_args(argv)
    manifest = json.loads(args.corpus_manifest.read_text())
    count = manifest.get("actual_count")
    if manifest.get("label_disclosure") != "simulated_semantic_editorial_labels" or (
            count != 400 if args.schema_version == 1 else not isinstance(count, int) or count < 100):
        raise SystemExit("corpus manifest is not a reviewed simulated-label editorial corpus of the expected length")
    cut = args.cycles
    if cut is not None and (args.schema_version != 2 or cut < WINDOW or cut % WINDOW or cut > count):
        raise SystemExit(f"--cycles needs schema 2 and a multiple of {WINDOW} from {WINDOW} to {count}")
    corpus, reference_labels = read_corpus(args.corpus, manifest)
    scenario_dirs = parse_scenarios(args.scenario)
    runs = {name: read(path / "results.json", count) for name, path in scenario_dirs.items()}
    if len({run["protocol"]["task"] for run in runs.values()}) != 1:
        raise SystemExit("modes do not share the same frozen task hash")
    for name, run in runs.items():
        if {cycle.get("item_id") for cycle in run["cycles"]} != set(corpus):
            raise SystemExit(f"{name}: frozen results do not match the reviewed public corpus")
        for cycle in run["cycles"]:
            revealed = cycle.get("human_label")
            if revealed is not None and revealed != reference_labels[cycle["item_id"]]:
                raise SystemExit(f"{name}: revealed label does not match frozen reference at cycle {cycle['cycle_number']}")
        if cut is not None:
            run["cycles"] = run["cycles"][:cut]
    prices = {"decision_model": PRICES[args.decision_model_price], "optimizer": PRICES[args.optimizer_price]}
    labels = ("publish", "reject")
    scenarios = {}
    for name, run in runs.items():
        events = read_events(scenario_dirs[name] / "runtime.sqlite3")
        if cut is not None:
            events = [event for event in events
                      if not isinstance(event.get("cycle_number"), int) or event["cycle_number"] <= cut]
        predictions = read_logged_predictions(scenario_dirs[name] / "runtime.sqlite3", run["cycles"], events)
        comments = read_logged_feedback(scenario_dirs[name] / "runtime.sqlite3", run["cycles"], events)
        scenario = {
            "feedback_policy": public_feedback_schedule(run["feedback_policy"]),
            "evaluator_only_windows": {
                "window_size": 100,
                "label_source": "Frozen simulated reference labels, evaluator-only; never learner feedback.",
                "first_100": evaluator_window(run["cycles"][:100], predictions, reference_labels),
                "last_100": evaluator_window(run["cycles"][-100:], predictions, reference_labels),
            },
        }
        if args.schema_version == 2:
            usage = usage_by_window(events, prices, window=WINDOW)
            scenario["windows"] = scenario_windows(run["cycles"], predictions, reference_labels, labels, usage)
            scenario["usage_total"] = usage["total"]
        scenario["cycles"] = [compact_cycle(cycle, predictions[cycle["cycle_number"]], corpus[cycle["item_id"]], comments)
                              for cycle in run["cycles"]]
        scenarios[name] = scenario
    attribution = {"dataset": manifest["dataset"], "revision": manifest["dataset_revision"], "license": manifest["license"],
                   "license_url": manifest["license_url"], "corpus_sha256": manifest["item_set_sha256"]}
    if args.schema_version == 2:
        attribution["price_basis"] = {
            "basis": "list prices, USD per million tokens, applied to the provider-reported usage on every recorded call",
            "decision_model": {"model": args.decision_model_price, **prices["decision_model"]},
            "optimizer": {"model": args.optimizer_price, **prices["optimizer"]},
        }
    payload = {
        "schema_version": args.schema_version,
        "title": args.title,
        "disclosure": "Curated marketing demo using simulated semantic editorial labels; not a representative benchmark or real newsroom votes.",
        "rubric_summary": RUBRIC_SUMMARY,
        "attribution": attribution,
        "task_hash": next(iter(runs.values()))["protocol"]["task"],
        **({"cycle_cut": {"cycles": cut, "recorded_cycles": count}} if cut is not None else {}),
        "scenarios": scenarios,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    args.output.write_text(encoded + "\n")
    print(json.dumps({"output": str(args.output), "sha256": hashlib.sha256(encoded.encode()).hexdigest(), "scenarios": list(runs)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
