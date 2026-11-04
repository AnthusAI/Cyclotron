"""Move a finished iteration from the store it was learned in to the store that decides.

An iteration is learned away from the live store, so decisions keep flowing while it runs. Adopting it needs no model:
the record carries each fitted version and every answer it was fitted on, so the live store can check them as if it
had asked itself. Nothing in a record is article text beyond the examples the version decides with.
"""

import json
from dataclasses import asdict
from uuid import uuid4

from .flywheel import _restore
from .journal import encode
from .priming_context import decode_priming, encode_priming


class IterationStale(RuntimeError):
    """The live store changed after this iteration began; it was learned from something no longer true."""


def version_record(wheel) -> dict:
    """One output's active version with every answer it depends on."""
    active = wheel.active
    answers, requests = {}, {}
    for dependency in active.answer_dependencies:
        answers[dependency["request_fingerprint"]] = wheel.runtime_cache.answer(dependency["request_fingerprint"])[1]
        provider = dependency.get("provider_dependency")
        if provider is not None:
            requests[provider["request_fingerprint"]] = provider["generation"]
    return {"classifier": json.loads(encode(asdict(active))), "answers": answers, "requests": requests}


def iteration_record(engine, *, base, examples, acceptance) -> dict:
    """What adopting needs: the versions it began from, the new versions, and the votes they decide with."""
    tasks = {cid: spec.task for cid, spec in engine.specs.items()}
    versions = {cid: version_record(wheel) for cid, wheel in engine.wheels.items() if wheel.active.fingerprint != base[cid]}
    settled = {}
    for version in versions.values():
        for fingerprint, generation in version["requests"].items():
            settled[fingerprint] = {"generation": generation, "payload": engine.shared.storage.get(fingerprint).payload}
    return {
        "base": dict(base),
        "versions": versions,
        "requests": settled,
        "examples": encode_priming(dict(examples), tasks, redact=engine.priming_redaction),
        "acceptance": dict(acceptance),
    }


def _import_request(storage, fingerprint, generation, payload):
    row = storage.get(fingerprint, payload=False)
    if row is None:
        owner = str(uuid4())
        storage.reserve(fingerprint, None, owner, claim=True)
        row = storage.settle(fingerprint, owner, "complete", payload)
    if row.status != "complete" or row.generation != generation:
        raise IterationStale("the live ledger holds a different answer for a request this iteration used")


def _import_answer(cache, key, payload):
    row = cache.answer(key)
    if row is None:
        cache.begin_answer(key, None)
        cache.complete_answer(key, payload, previous=None)
    elif row[0] != "complete" or json.loads(row[1]) != json.loads(payload):
        raise IterationStale("the live store holds a different answer for a request this iteration used")


async def adopt_iteration(engine, record, *, execution_guard=None) -> dict:
    """Make ``record`` the live versions, all at once or not at all. Adopting the same record again changes nothing."""
    versions = {cid: _restore(row["classifier"]) for cid, row in record["versions"].items()}
    async with engine._learn_lock, engine.execution(execution_guard=execution_guard):
        if all(engine.wheels[cid].active.fingerprint == version.fingerprint for cid, version in versions.items()):
            return {"adopted": False, "versions": {cid: version.fingerprint for cid, version in versions.items()}}
        moved = [cid for cid, fingerprint in record["base"].items() if engine.wheels[cid].active.fingerprint != fingerprint]
        if moved:
            raise IterationStale(f"these outputs changed while the iteration was learned: {sorted(moved)}")
        for fingerprint, row in record["requests"].items():
            _import_request(engine.shared.storage, fingerprint, row["generation"], row["payload"])
        for cid, row in record["versions"].items():
            for key, payload in row["answers"].items():
                _import_answer(engine.wheels[cid].runtime_cache, key, payload)
        stored = engine.root_storage.meta("priming_examples")
        merged = json.loads(stored[0]) if stored else {}
        for cid, rows in record["examples"].items():
            replaced = {row["item"]["id"] for row in rows}
            merged[cid] = [row for row in merged.get(cid, ()) if row["item"]["id"] not in replaced] + list(rows)
        with engine.root_storage.transaction():
            engine.root_storage.put_meta("priming_examples", encode(merged))
        engine.priming_examples = decode_priming(merged)
        for cid, version in versions.items():
            wheel = engine.wheels[cid]
            wheel._activate(version)
            wheel._emit({"kind": "iteration-accepted", "acceptance": record["acceptance"].get(cid), "version": version.fingerprint})
        engine._sync()
    return {"adopted": True, "versions": {cid: version.fingerprint for cid, version in versions.items()}}
