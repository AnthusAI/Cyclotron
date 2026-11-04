"""Standing runs retrain themselves: notice when votes are worth it, learn in a Fargate task, adopt through the queue.

The worker's scheduled check reads only the votes that arrived since it last looked and starts a task when a site is
due. The task learns away from the live store, so decisions keep flowing, then queues one data-only job that makes the
result live. The state beside the run records what was learned from, so a vote counts once and a change counts again.
Nothing here depends on anything outside this account's cloud.
"""

import asyncio
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4

from .decision_kinds import WRITTEN
from .embedded_cyclotron import ClassifierSpec, CyclotronDefinition
from .iteration_learning import IterationPlan, learn_iteration
from .iteration_transfer import CURRENT_ITERATION
from .journal import encode
from .lab_decisions import runtime_reader
from .machine_access import MachineActor
from .optimizer_agent import OptimizerAgent
from .retrain_trigger import STARTING_REQUIREMENT, LiveDecision, next_requirement, retrain_due
from .storage_errors import StorageConflict
from .vote_examples import Vote

CHECK_EVENT = "retraining-check"
OPTIMIZER_MODEL = "gpt-6-luna"
MODEL_CALL_SECONDS = 120
OPTIMIZER_CALL_SECONDS = 300
RETRY_AFTER_SECONDS = 900
FEEDBACK = "publication-machine-feedback"


def empty_state():
    return {
        "schema": "cyclotron-retraining/v1",
        "cursor": 0,
        "votes": {},
        "seen": {},
        "requirement": {},
        "claimed": {},
        "current": None,
        "pending": None,
        "started": None,
        "failures": 0,
        "history": [],
    }


def read_state(runtime, account_id, run_id):
    """The run's state for reading, or None before its first check. Creates nothing."""
    journal = runtime.journal(account_id, "optimizer", "retraining-" + run_id)
    return journal.read().value if journal.exists() else None


class RetrainingState:
    """One run's retraining state: a small journal in the optimizer domain, written with compare-and-set."""

    def __init__(self, runtime, account_id, run_id):
        self.journal = runtime.journal(account_id, "optimizer", "retraining-" + run_id)

    def read(self):
        if not self.journal.exists():
            self.journal.initialize(empty_state())
        return self.journal.read()

    def change(self, apply):
        record = self.read()
        value = json.loads(encode(record.value))
        apply(value)
        try:
            return self.journal.commit(record, value).value
        except StorageConflict:
            return self.change(apply)


def refresh_votes(store, run_id, list_id, state):
    """Bring the state up to the run's events: each voted item's current label, explanation and live decision."""
    touched, cursor = {}, state["cursor"]
    while True:
        rows = store.events(run_id, after=cursor, limit=1000)
        if not rows:
            break
        for row in rows:
            cursor = max(cursor, row["sequence"])
            payload = row.get("payload") or {}
            if payload.get("kind") == FEEDBACK:
                touched[(payload["output"], payload["itemId"])] = payload["itemRevision"]
    state["cursor"] = cursor
    for (output, item_id), revision in touched.items():
        current = [row for row in store.item_labels(list_id, item_id, revision) if row["classifier_id"] == output]
        votes = state["votes"].setdefault(output, {})
        if not current:
            votes.pop(item_id, None)
            continue
        latest = max(current, key=lambda row: row["sequence"])
        decided = None
        for result in store.item_results(list_id, item_id, revision):  # newest run first
            decided = result["payload"]["classifiers"].get(output)
            if decided is not None:
                break
        votes[item_id] = {
            "revision": revision,
            "label": latest["label"],
            "comment": latest["comment"] or "",
            "decided": decided,
        }
    return state


def due_outputs(state):
    """Every output's trigger decision, from the votes in the state."""
    decisions = {}
    for output, rows in state["votes"].items():
        votes = [Vote(output, item_id, {}, row["label"], row["comment"]) for item_id, row in rows.items()]
        live = {
            item_id: LiveDecision(row["decided"]["label"], row["decided"].get("confidence"))
            for item_id, row in rows.items()
            if row["decided"]
        }
        decisions[output] = retrain_due(
            votes,
            live,
            seen=state["seen"].get(output, {}),
            requirement=state["requirement"].get(output, STARTING_REQUIREMENT),
            claimed_accuracy=state["claimed"].get(output),
        )
    return decisions


def settle_adoption(store, state, now):
    """Move a finished adoption into the state: what it learned from no longer counts as new, and the bar adapts."""
    pending = state["pending"]
    if pending is None or pending["jobId"] is None:
        return
    job = store.command_envelope(pending["jobId"])[0]
    if job["status"] not in ("completed", "failed", "interrupted"):
        return
    state["pending"] = None
    state["history"].append({**pending["summary"], "adoption": job["status"], "at": datetime.now(timezone.utc).isoformat()})
    if job["status"] != "completed":
        # Learning was paid for and did not land, so the next attempt waits like a failed task's.
        state["failures"] += 1
        state["started"] = now
        return
    state["current"] = pending["iteration"]
    for output, row in pending["learned"].items():
        state["seen"][output] = row["seen"]
        state["claimed"][output] = row["claimed"]
        gain = row["candidateRight"] - row["incomingRight"]
        state["requirement"][output] = next_requirement(state["requirement"].get(output, STARTING_REQUIREMENT), gain)


def may_start(state, now):
    """One task at a time, with nothing awaiting adoption, and a failed task's retry spaced further apart each time."""
    if state["pending"] is not None:
        return False
    return state["started"] is None or now >= state["started"] + RETRY_AFTER_SECONDS * 2 ** state["failures"]


def check_run(runtime, target, ecs, settings, *, now=None):
    """One standing run's scheduled check. Paid work happens only in the task it may start."""
    now = time.time() if now is None else now
    store = runtime.console(target.account_id)
    config = store.run(target.run_id)["config"]
    state = RetrainingState(runtime, target.account_id, target.run_id)
    held = state.read().value["pending"]
    if held is not None and held["jobId"] is None:
        # The task stopped between storing its result and queueing it. Queueing again finds the same job.
        job = submit_adoption(runtime, target, held["iteration"], principal=settings["principal"])
        state.change(lambda value: value["pending"].update(jobId=job["id"]))
    current = state.change(
        lambda value: (refresh_votes(store, target.run_id, config["item_list_id"], value), settle_adoption(store, value, now))
    )
    decisions = due_outputs(current)
    due = sorted(output for output, row in decisions.items() if row["due"])
    if not due or not may_start(current, now):
        return {"due": due, "started": False, "outputs": decisions}
    running = ecs.list_tasks(cluster=settings["cluster"], startedBy=target.run_id, desiredStatus="RUNNING")["taskArns"]
    if running:
        return {"due": due, "started": False, "outputs": decisions}

    def started(value):
        if value["started"] is not None:
            value["failures"] += 1  # A task that finishes clears this; one still marked here never finished.
        value["started"] = now

    state.change(started)
    ecs.run_task(
        cluster=settings["cluster"],
        taskDefinition=settings["task_definition"],
        launchType="FARGATE",
        startedBy=target.run_id,
        networkConfiguration={"awsvpcConfiguration": {"subnets": settings["subnets"], "assignPublicIp": "ENABLED"}},
        overrides={
            "containerOverrides": [
                {
                    "name": "retraining",
                    "environment": [
                        {"name": "CYCLOTRON_RETRAIN_ACCOUNT", "value": target.account_id},
                        {"name": "CYCLOTRON_RETRAIN_RUN", "value": target.run_id},
                    ],
                }
            ]
        },
    )
    print(json.dumps({"event": "retraining_started", "run": target.run_id, "due": due}))
    return {"due": due, "started": True, "outputs": decisions}


def check_all(runtime, ecs, settings):
    from .hosted_publications import configured_targets

    return {target.run_id: check_run(runtime, target, ecs, settings) for target in configured_targets(runtime) if target.standing}


def settings_from_environment(environment):
    return {
        "cluster": environment["CYCLOTRON_RETRAIN_CLUSTER"],
        "task_definition": environment["CYCLOTRON_RETRAIN_TASK_DEFINITION"],
        "subnets": environment["CYCLOTRON_RETRAIN_SUBNETS"].split(","),
        "principal": environment["CYCLOTRON_RETRAIN_ROLE_ARN"],
    }


class Deadlined:
    """A decision model whose calls cannot hang: a dead connection fails the task instead of holding it open for hours."""

    def __init__(self, model, seconds):
        self._model, self._seconds, self.requests = model, seconds, 0

    def __getattr__(self, name):
        return getattr(self._model, name)

    async def classify_many(self, *args, **kwargs):
        self.requests += 1
        async with asyncio.timeout(self._seconds):
            return await self._model.classify_many(*args, **kwargs)


def definition_of(config):
    """The same definition the worker opens for this run."""
    specs, rubrics = [], {}
    for row in config["classifiers"]:
        definition = row["config"]
        positive = next((c["label"] for c in definition["classes"] if c.get("role") == "positive"), None)
        specs.append(
            ClassifierSpec(
                row["id"],
                tuple(c["label"] for c in definition["classes"]),
                definition["question"],
                input_field=definition.get("input_field", "text"),
                positive_label=positive,
            )
        )
        rubrics[row["id"]] = definition.get("seed_rubric", "")
    return CyclotronDefinition(config["cyclotron_id"], tuple(specs), seed=config["seed"]), rubrics


def live_store(runtime, account_id, config):
    """What the live store decides with, read without taking ownership: examples, current iteration, each output's version."""
    name = "publication-" + sha256(config["cyclotron_id"].encode()).hexdigest()
    meta = runtime.journal(account_id, "workspace", name).read().value["data"]["meta"]
    versions = {}
    for row in config["classifiers"]:
        stored = runtime_reader(runtime, account_id, config["cyclotron_id"], row["id"]).observed_state("active")
        if stored:
            versions[row["id"]] = json.loads(stored)
    return json.loads(meta.get("priming_examples") or "{}"), meta.get(CURRENT_ITERATION), versions


def full_votes(store, list_id, state):
    votes = {}
    for output, rows in state["votes"].items():
        votes[output] = tuple(
            Vote(output, item_id, _values(store, list_id, item_id, row["revision"]), row["label"], row["comment"])
            for item_id, row in rows.items()
        )
    return votes


def _values(store, list_id, item_id, revision):
    values = store.item_revision(list_id, item_id, revision)
    return values.get("values", values)


def learned_summary(result, votes):
    return {
        output: {
            "seen": {vote.item_id: vote.label for vote in votes[output]},
            "claimed": _accuracy(row["acceptance"]),
            "candidateRight": row["acceptance"]["candidateRight"],
            "incomingRight": row["acceptance"]["incomingRight"],
        }
        for output, row in result["report"].items()
        if row["acceptance"]["accepted"]
    }


def _accuracy(acceptance):
    compared = acceptance["compared"]
    return None if not compared else acceptance.get("candidateAgreed", 0) / compared


def store_iteration(runtime, target, record):
    """Keep the record, content-addressed, and return the reference a queued adoption names."""
    reference = runtime.journal(target.account_id, "optimizer", "retraining").payloads.put(encode(record))
    return {"id": record["id"], "digest": reference.digest, "length": reference.length}


def submit_adoption(runtime, target, iteration, *, principal):
    """Queue the job that makes an iteration live, in its place among the run's decisions. Repeating finds the same job.

    The scheduled sweep starts it when it reaches the head of the queue.
    """
    binding = target.bindings[0]
    actor = MachineActor(
        principal, "retraining", target.account_id, target.run_id, runtime.binding(target.account_id, target.run_id)
    )
    command = {"publication": binding.publication_id, "bindingRevision": binding.revision, "iteration": iteration}
    store = runtime.console(target.account_id)
    return store.enqueue_authorized_command(target.run_id, "retrain:" + iteration["id"], WRITTEN, command, actor)


def retrain(runtime, target, *, model, optimizer, principal, directory, transport=None):
    """One task's whole job. Writes the result to the state, including when nothing was accepted."""
    store = runtime.console(target.account_id)
    config = store.run(target.run_id)["config"]
    state = RetrainingState(runtime, target.account_id, target.run_id)
    current = state.read().value
    decisions = due_outputs(current)
    due = tuple(sorted(output for output, row in decisions.items() if row["due"]))
    votes = full_votes(store, config["item_list_id"], current)
    definition, rubrics = definition_of(config)
    priming, parent, live = live_store(runtime, target.account_id, config)
    plan = IterationPlan(
        identity=str(uuid4()),
        definition=definition,
        rubrics=rubrics,
        priming=priming,
        votes={spec.id: votes.get(spec.id, ()) for spec in definition.classifiers},
        due=due,
        parent=parent,
        live=live,
    )
    result = asyncio.run(learn_iteration(plan, model, optimizer, directory))
    summary = {
        "id": plan.identity,
        "due": list(due),
        "report": result["report"],
        "siblings": result.get("siblings", {}),
        "reason": result.get("reason"),
        "calls": {"decision": getattr(model, "requests", None), "optimizer": getattr(transport, "calls", None)},
    }
    print(json.dumps({"event": "retraining_learned", "run": target.run_id, "accepted": result["record"] is not None}))
    if result["record"] is None:

        def refused(value):
            value["started"], value["failures"] = None, 0
            value["history"].append({**summary, "adoption": "not-accepted"})
            for output in due:  # What was just judged waits for new evidence before it is judged again.
                value["seen"][output] = {vote.item_id: vote.label for vote in votes[output]}

        state.change(refused)
        return summary
    iteration = store_iteration(runtime, target, result["record"])
    state.change(
        lambda value: value.update(
            started=None,
            failures=0,
            pending={"jobId": None, "iteration": iteration, "learned": learned_summary(result, votes), "summary": summary},
        )
    )
    job = submit_adoption(runtime, target, iteration, principal=principal)
    state.change(lambda value: value["pending"].update(jobId=job["id"]))
    return summary


def main():
    """The Fargate task's entry point."""
    from openai import OpenAI

    from .adapters.openai_optimizer import OpenAIOptimizer
    from .cloud_entry import runtime
    from .hosted_publications import configured_targets
    from .provider_credentials import SecretsManagerKeySource

    host = runtime()
    account, run = os.environ["CYCLOTRON_RETRAIN_ACCOUNT"], os.environ["CYCLOTRON_RETRAIN_RUN"]
    target = next(t for t in configured_targets(host) if t.account_id == account and t.run_id == run)
    config = host.console(account).run(run)["config"]
    model, _ = host.model_factory(config)
    from botocore.session import get_session

    key = SecretsManagerKeySource(lambda: get_session().create_client("secretsmanager", region_name=host.config.region))()
    transport = OpenAIOptimizer(
        OpenAI(api_key=key, max_retries=0, timeout=OPTIMIZER_CALL_SECONDS), model=OPTIMIZER_MODEL, max_calls=1000
    )
    with tempfile.TemporaryDirectory() as directory:
        retrain(
            host,
            target,
            model=Deadlined(model, MODEL_CALL_SECONDS),
            optimizer=OptimizerAgent(transport),
            transport=transport,
            principal=os.environ["CYCLOTRON_RETRAIN_ROLE_ARN"],
            directory=directory,
        )


if __name__ == "__main__":
    main()
