#!/usr/bin/env python3
"""Export sanitized, evaluator-only temporal aggregates for the editorial study.

This exporter joins immutable ``prediction`` events to the frozen reference file
outside the learner.  It never writes a label, item ID, text, or per-item score
into the artifact, so the result is safe for the marketing workspace but cannot
be used as feedback by a live run.
"""
import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path


LABELS = ("publish", "reject")
WINDOWS = (100, 200)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def frozen_labels(path):
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    values = {row["id"]: row.get("simulated_label", row.get("label")) for row in rows}
    if len(values) != len(rows) or set(values.values()) - set(LABELS):
        raise ValueError("reference must contain unique editorial IDs with publish/reject labels")
    return values


def predictions(database, labels):
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    events = [json.loads(row[0]) for row in connection.execute("SELECT payload FROM runtime_events ORDER BY id")]
    connection.close()
    completed = {event.get("cycle_id") for event in events if event.get("kind") == "cycle-completed"}
    found = []
    for event in events:
        if event.get("kind") != "prediction" or event.get("cycle_id") not in completed:
            continue
        identifier = event["target_id"]
        if identifier not in labels:
            raise ValueError("prediction does not map to the frozen reference")
        found.append({"cycle_number": event["cycle_number"], "actual": labels[identifier],
                      "final_label": event["label"], "final_probabilities": event["probabilities"],
                      "raw_label": event["decision_model_label"],
                      "raw_probabilities": event["decision_model_probabilities"]})
    found.sort(key=lambda row: row["cycle_number"])
    if len({row["cycle_number"] for row in found}) != len(found):
        raise ValueError("more than one immutable prediction was recorded for a completed cycle")
    return found


def summary(rows, *, label_key, probability_key):
    if not rows:
        return None
    correct = sum(row[label_key] == row["actual"] for row in rows)
    brier = 0.0
    bins = [[] for _ in range(10)]
    actual_counts = Counter()
    predicted_counts = Counter()
    for row in rows:
        actual_counts[row["actual"]] += 1
        predicted_counts[row[label_key]] += 1
        probabilities = row[probability_key]
        if set(probabilities) != set(LABELS):
            raise ValueError("prediction probabilities do not match the frozen task labels")
        brier += sum((probabilities[label] - float(label == row["actual"])) ** 2 for label in LABELS)
        confidence = probabilities[row[label_key]]
        bins[min(9, int(confidence * 10))].append(float(row[label_key] == row["actual"]))
    ece = sum((len(values) / len(rows)) * abs(sum(values) / len(values) - (index + .5) / 10)
              for index, values in enumerate(bins) if values)
    return {"count": len(rows), "accuracy": correct / len(rows), "brier": brier / len(rows), "ece": ece,
            "actual_label_counts": dict(sorted(actual_counts.items())),
            "predicted_label_counts": dict(sorted(predicted_counts.items()))}


def temporal(rows):
    result = {"completed_predictions": len(rows)}
    for window in WINDOWS:
        result[f"first_{window}"] = (None if len(rows) < window else {
            "raw_decision": summary(rows[:window], label_key="raw_label", probability_key="raw_probabilities"),
            "final_output": summary(rows[:window], label_key="final_label", probability_key="final_probabilities")})
        result[f"last_{window}"] = (None if len(rows) < window else {
            "raw_decision": summary(rows[-window:], label_key="raw_label", probability_key="raw_probabilities"),
            "final_output": summary(rows[-window:], label_key="final_label", probability_key="final_probabilities")})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study-output", type=Path, required=True)
    parser.add_argument("--operational-corpus", type=Path, required=True)
    parser.add_argument("--operational-sha256", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if sha256(args.operational_corpus) != args.operational_sha256:
        raise ValueError("operational corpus hash mismatch")
    labels = frozen_labels(args.operational_corpus)
    protocol_path=args.study_output / "protocol.json"
    if protocol_path.exists():
        modes_to_export=tuple(json.loads(protocol_path.read_text()).get("modes", ()))
    else:
        modes_to_export=tuple(path.name for path in args.study_output.iterdir() if path.is_dir())
    if not modes_to_export:
        raise ValueError("study output has no declared scenario modes")
    modes = {}
    for mode in modes_to_export:
        database = args.study_output / mode / "runtime.sqlite3"
        if database.exists():
            modes[mode] = temporal(predictions(database, labels))
        else:
            modes[mode] = {"completed_predictions": 0, "first_100": None, "last_100": None,
                           "first_200": None, "last_200": None}
    artifact = {
        "artifact_type": "editorial-prequential-evaluator-only-aggregate",
        "version": 1,
        "sanitization": {"contains_item_ids": False, "contains_item_text": False,
                           "contains_individual_labels": False, "contains_individual_probabilities": False},
        "evaluator_firewall": "The frozen reference labels are joined only by this offline exporter; no label is emitted to the learner or included in this aggregate.",
        "source_mapping": {"reference": {"sha256": args.operational_sha256, "join_key": "reference.id = prediction.target_id"},
                           "prediction": {"database": "<study-output>/<mode>/runtime.sqlite3",
                                          "event_kind": "prediction", "order": "cycle_number", "fields": {
                                              "raw_decision": ["decision_model_label", "decision_model_probabilities"],
                                              "final_output": ["label", "probabilities"]}},
                           "eligibility": "only predictions in cycles with a durable cycle-completed event"},
        "modes": modes,
    }
    output = args.output or args.study_output / "evaluator_temporal_aggregates.json"
    output.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
