"""Persistent measurements, distinct from the decision to deploy a classifier."""
import hashlib
import json
import math


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def question_id(question):
    return hashlib.sha256(_json(question).encode()).hexdigest()


def probability_diagnostics(classes, options, rows):
    """Describe training probabilities, never estimate generalization from them."""
    groups = {label: [] for label in classes}
    for label, probabilities in rows:
        if label not in groups:
            raise ValueError("diagnostic label is not declared")
        if probabilities is not None:
            if set(probabilities) != set(options) or any(
                isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1
                for v in probabilities.values()) or not math.isclose(sum(probabilities.values()), 1., abs_tol=1e-6):
                raise ValueError("diagnostics require a complete valid probability distribution")
        groups[label].append(probabilities)
    report = {}
    for label, values in groups.items():
        answered = [v for v in values if v is not None]
        report[label] = {"count": len(values), "answered": len(answered), "missing": len(values)-len(answered),
                         "mean_probabilities": {option: sum(v[option] for v in answered)/len(answered)
                                                if answered else None for option in options}}
    return {"scope": "training only; descriptive, not generalization evidence", "by_class": report}


class FeatureBank:
    def __init__(self, db):
        self.db = db
        db.execute("CREATE TABLE IF NOT EXISTS feature_bank (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")

    def register(self, question, *, rationale, evidence):
        if set(question) != {"name", "instructions", "labels"}:
            raise ValueError("feature definition must contain name, instructions and labels")
        # Validation uses the same type as actual decision-model requests.
        from .models import DecisionTask
        task = DecisionTask(question["name"], question["labels"], question["instructions"])
        if task.name == "decision":
            raise ValueError("supporting feature name cannot be decision")
        question = {"name": task.name, "instructions": task.instructions, "labels": list(task.labels)}
        key = question_id(question)
        existing = next((entry for entry in self.entries() if entry["id"] == key), None)
        if existing:
            if evidence not in existing["discovery_evidence"]:
                existing["discovery_evidence"].append(evidence)
                self._save(existing)
            return key
        parents = [entry["id"] for entry in self.entries() if entry["question"]["name"] == task.name]
        self._save({"id": key, "question": question, "concept": task.name, "rationale": rationale,
                    "parent_ids": parents, "discovery_evidence": [evidence], "attempts": [], "state": "proposed"})
        return key

    def _save(self, entry):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO feature_bank VALUES (?,?)", (entry["id"], _json(entry)))

    def entries(self, *, active_tasks=()):
        deployed = {question_id(question) for question in active_tasks}
        entries = [json.loads(row[0]) for row in self.db.execute("SELECT payload FROM feature_bank ORDER BY id")]
        for entry in entries:
            if entry["id"] in deployed:
                entry["state"] = "deployed"
        return tuple(entries)

    def record(self, key, result, diagnostics):
        entry = next(entry for entry in self.entries() if entry["id"] == key)
        attempt = {**result, "diagnostics": diagnostics}
        if attempt not in entry["attempts"]:
            entry["attempts"].append(attempt)
        entry["state"] = "measured" if result.get("trial_fingerprint") or diagnostics else "deferred"
        self._save(entry)
