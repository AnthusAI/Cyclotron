"""One binding's read-only view of its classifiers, for the newsroom that uses them.

Everything here is scoped to the binding's own outputs. Agreement compares editors' current votes with the decision
recorded for the same item revision. It is not accuracy: editors choose what they vote on, so votes are not a random
sample, and only a held-out set can say how accurate a classifier is.
"""

from collections import Counter, defaultdict
from datetime import datetime, timezone

from .lab_decisions import _parallel
from .publication_lab import criteria_fingerprint

SCHEMA = "cyclotron-classifier-report/v1"
LEARNING_WAITING = "no retraining has been due yet; each site retrains when its votes are worth it"
LEARNING_ON = "this site retrains in the cloud when its votes are worth it"
NO_HELD_OUT = "standing runs do not produce held-out evaluations yet"
NO_MODEL = "no machine-learning model has been fitted for this classifier"
NO_SNAPSHOT = "no decision has completed for this binding yet"


def binding_votes(store, run_id, binding):
    """The item revisions this binding's editors voted on, as (output, item id, item revision)."""
    votes = {}
    for event in store.all_events(run_id):
        payload = event.get("payload") or {}
        if (
            payload.get("kind") == "publication-machine-feedback"
            and payload.get("publicationId") == binding.publication_id
            and payload.get("output") in binding.outputs
        ):
            votes[(payload["output"], payload["itemId"], payload["itemRevision"])] = True
    return list(votes)


def scored_vote(store, list_id, vote):
    """The current vote and the decision recorded for it, or None when the vote was retracted."""
    output, item_id, revision = vote
    current = [row for row in store.item_labels(list_id, item_id, revision) if row["classifier_id"] == output]
    if not current:
        return None
    row = max(current, key=lambda r: r["sequence"])
    decided = None
    for result in store.item_results(list_id, item_id, revision):  # newest run first
        decided = result["payload"]["classifiers"].get(output)
        if decided is not None:
            break
    return {
        "output": output,
        "label": row["label"],
        "classifierRevision": row["classifier_revision"],
        "day": row["created_at"][:10],
        "decided": None if decided is None else decided["label"],
    }


def _scores(votes):
    matched = [v for v in votes if v["decided"] is not None]
    return {"votes": len(votes), "compared": len(matched), "agreed": sum(v["decided"] == v["label"] for v in matched)}


def agreement(votes):
    """Scores overall, by classifier revision and by day. A vote with no recorded decision is counted, not compared."""
    by_revision, by_day = defaultdict(list), defaultdict(list)
    for vote in votes:
        by_revision[vote["classifierRevision"]].append(vote)
        by_day[vote["day"]].append(vote)
    return {
        **_scores(votes),
        "byClassifierRevision": [{"classifierRevision": k, **_scores(by_revision[k])} for k in sorted(by_revision)],
        "byDay": [{"day": k, **_scores(by_day[k])} for k in sorted(by_day)],
    }


def learning_state(retraining, output):
    """What retraining knows about one output: how close the next retrain is, why, and how each round went."""
    from .retraining import due_outputs

    if retraining is None:
        return {"state": "waiting", "reason": LEARNING_WAITING, "next": None, "rounds": []}
    rounds = [
        {
            "id": row["id"],
            "adoption": row.get("adoption"),
            "acceptance": row["report"][output]["acceptance"],
            "calls": row.get("calls"),
        }
        for row in retraining["history"]
        if output in row.get("report", {})
    ]
    return {
        "state": "active" if rounds else "waiting",
        "reason": LEARNING_ON if rounds else LEARNING_WAITING,
        "next": due_outputs(retraining).get(output),
        "rounds": rounds,
    }


def output_report(definition, status, votes, retraining=None):
    config = definition["config"]
    labels = Counter(vote["label"] for vote in votes)
    cyclotron, pending = (status or {}).get("cyclotron") or {}, (status or {}).get("pending") or {}
    alignment = (status or {}).get("alignment") or {}
    return {
        "classifier": {
            "id": definition["id"],
            "revision": definition["revision"],
            "question": config["question"],
            "criteriaFingerprint": criteria_fingerprint(config["question"]),
            "rubric": config.get("rubric") or "",
            "classes": config["classes"],
        },
        "learning": {
            **learning_state(retraining, definition["id"]),
            "version": cyclotron.get("version"),
            "refits": cyclotron.get("refits"),
            "lastChange": (status or {}).get("lastChange"),
            "asOf": (status or {}).get("asOf"),
            "unavailableReason": None if status else NO_SNAPSHOT,
        },
        "agreementWithEditors": agreement(votes),
        "heldOut": {"accuracy": None, "reason": NO_HELD_OUT},
        "counts": {
            "labels": dict(labels),
            "reviewedDecisions": alignment.get("labels"),
            "primedExamples": cyclotron.get("primedExamples"),
            "decisionsAwaitingReview": pending.get("decisionsAwaitingReview"),
        },
        "model": {"features": None, "weights": None, "reason": NO_MODEL},
    }


def classifier_report(store, run, binding, snapshot, retraining=None):
    """The report for every output of the binding. ``snapshot`` is its newest status snapshot, or None;
    ``retraining`` is the run's retraining state, or None before its first check."""
    list_id = run["config"].get("item_list_id")
    scored = _parallel(lambda vote: scored_vote(store, list_id, vote), binding_votes(store, run["id"], binding))
    statuses = (snapshot or {}).get("classifiers") or {}
    return {
        "schema": SCHEMA,
        "operation": "report",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "scope": "votes recorded on this run",
        "outputs": {
            definition["id"]: output_report(
                definition,
                statuses.get(definition["id"]),
                [v for v in scored if v and v["output"] == definition["id"]],
                retraining,
            )
            for definition in run["config"]["classifiers"]
            if definition["id"] in binding.outputs
        },
    }
