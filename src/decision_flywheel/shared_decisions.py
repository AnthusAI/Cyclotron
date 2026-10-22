"""Durable complete-request batch cache and a run-wide paid-attempt ceiling."""

import hashlib
import json
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from uuid import uuid4

from .batch_storage import BatchInProgress, SQLiteBatchStorage
from .batched_classification import batch_request, output_shares
from .classifier_config import ClassifiedAnswers
from .decision_cache import CacheOptions
from .models import DecisionResult


def key(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def feature_context_identity(model_identity, configs, training):
    """Stable provenance for fitting and frozen endpoint validation."""
    context = {}
    for name, definition in configs.items():
        pool = {row.item.id: row for row in training[name]}
        context[name] = {
            "configuration": definition.fingerprint,
            "examples": [asdict(pool[item_id]) for item_id in definition.example_ids],
        }
    return model_identity + ":shared-features-v1:" + key(context)


class SharedDecisions:
    def __init__(self, path, model, *, max_requests, observer, share_supporting_questions=False, storage=None):
        self.model, self.max_requests, self.observer = model, max_requests, observer
        # Off for stores created before sharing existed: their requests never change.
        self.share_supporting_questions = bool(share_supporting_questions)
        self.storage = storage if storage is not None else SQLiteBatchStorage(path)
        # Compatibility for existing local snapshot/export callers only. The
        # shared-request consumer itself never issues SQL through this handle.
        self.db = getattr(self.storage, "db", None)
        self.prepared = {}
        self.identity = "unprepared"
        self.context_configs = {}
        self.context_training = {}

    def bind_context(self, configs, training):
        """Pin siblings for all candidate and inference feature requests."""
        if not configs or set(configs) != set(training):
            raise ValueError("every classifier needs its own explicit training pool")
        self.context_configs = dict(configs)
        self.context_training = {name: tuple(rows) for name, rows in training.items()}
        self.prepared = {}

    def unbind_context(self):
        """Forget the pinned siblings, as when the classifier set changes."""
        self.context_configs = {}
        self.context_training = {}
        self.prepared = {}

    MUTABLE = ("context_configs", "context_training", "prepared", "prepared_scope", "identity")

    def snapshot(self):
        """Every mutable binding field, for an exact restore."""
        return {name: getattr(self, name, None) for name in self.MUTABLE}

    def restore(self, state):
        for name, value in state.items():
            setattr(self, name, value)

    @contextmanager
    def bound(self, configs, training):
        """Bind a temporary sibling context, then restore the previous binding exactly."""
        saved = (self.context_configs, self.context_training, self.prepared, getattr(self, "prepared_scope", None))
        self.bind_context(configs, training)
        try:
            yield
        finally:
            self.context_configs, self.context_training, self.prepared, self.prepared_scope = saved

    def scope(self, identifier, config, training):
        return ({**self.context_configs, identifier: config}, {**self.context_training, identifier: tuple(training)})

    @property
    def requests(self):
        return self.storage.requests()

    def close(self):
        self.storage.close()

    def generation(self, fingerprint):
        row = self.storage.get(fingerprint, payload=False)
        return row.generation if row else 1

    def validate_capabilities(self, request):
        capabilities = getattr(self.model, "capabilities", None)
        if capabilities is not None:
            for scope in request["state"]["classifiers"].values():
                capabilities.validate_context(scope["examples"])

    async def prepare(self, configs, target, training, *, now=None, options=None):
        # Prepared children belong to exactly one complete batch. A later
        # scope must not silently retain answers from its predecessor.
        self.prepared = {}
        options = options or CacheOptions()
        now = now or datetime.now(timezone.utc)
        request, identities = batch_request(configs, target, training, now=now, share_questions=self.share_supporting_questions)
        self.validate_capabilities(request)
        fingerprint = key({"model": self.model.model_identity, "request": request})
        self.identity = fingerprint
        owner = str(uuid4())
        row = self.storage.reserve(
            fingerprint, self.max_requests, owner, policy=options.policy, retry_failed=options.retry_failed
        )
        generation = row.generation
        cached = row.status == "complete"
        if cached:
            payload = json.loads(row.payload)
        else:
            if not self.storage.claim(fingerprint, owner):
                raise BatchInProgress("shared request already dispatched; explicit reconciliation required")
            exchanges = []
            try:
                result = await self.model.classify_many(
                    configs,
                    target,
                    training,
                    now=now,
                    event_sink=exchanges.append,
                    **({"share_supporting_questions": True} if self.share_supporting_questions else {}),
                )
                if set(result.answers) != set(configs):
                    raise ValueError("batch missing a classifier")
                from .flywheel import DecisionFlywheel

                for identifier, config in configs.items():
                    DecisionFlywheel._features(
                        config, ClassifiedAnswers(result.answers[identifier], result.model, None, result.latency_ms)
                    )
                payload = {"result": asdict(result), "exchanges": exchanges}
            except Exception:
                self.storage.settle(fingerprint, owner, "failed")
                raise
            # An uncertain publication must not be converted into a failed,
            # automatically retryable provider attempt.
            self.storage.settle(fingerprint, owner, "complete", json.dumps(payload, allow_nan=False))
        self.observer(
            {
                "kind": "shared-decision-batch",
                "request_fingerprint": fingerprint,
                "classifier_ids": list(configs),
                "cached": cached,
                "usage": None if cached else payload["result"]["usage"],
                "requests": self.requests,
                "generation": generation,
                "cache_policy": options.policy,
                "retry_failed": options.retry_failed,
                "output_shares": output_shares(request, identities),
            }
        )
        self.prepared_scope = (dict(configs), {cid: tuple(rows) for cid, rows in training.items()})
        for identifier, config in configs.items():
            child = key({"classifier": identifier, "request": config.request(target, training[identifier], now=now)})
            self.prepared[child] = (payload, identifier, fingerprint, generation)
        return payload, now

    def adapter(self, identifier):
        shared = self

        class Adapter:
            @property
            def capabilities(self):
                return getattr(shared.model, "capabilities", None)

            @property
            def model_identity(self):
                return shared.model.model_identity + ":shared-v1:" + shared.identity

            def request_fingerprint(self, config, target, training, *, now=None):
                child = key({"classifier": identifier, "request": config.request(target, training, now=now)})
                if child in shared.prepared and not shared.context_configs:
                    fingerprint = shared.prepared[child][2]
                else:
                    configs, pools = shared.scope(identifier, config, training)
                    request, _ = batch_request(configs, target, pools, now=now, share_questions=shared.share_supporting_questions)
                    fingerprint = key({"model": shared.model.model_identity, "request": request})
                return fingerprint

            def cache_identity(self, config, target, training, *, now=None):
                fingerprint = self.request_fingerprint(config, target, training, now=now)
                return (
                    shared.model.model_identity + ":shared-answer-v3:" + fingerprint + ":" + str(shared.generation(fingerprint))
                )

            def answer_dependency(self, config, target, training, *, now=None):
                fingerprint = self.request_fingerprint(config, target, training, now=now)
                return {"request_fingerprint": fingerprint, "generation": shared.generation(fingerprint)}

            def answer_dependency_current(self, dependency):
                fingerprint = dependency["request_fingerprint"]
                row = shared.storage.get(fingerprint, payload=False)
                return bool(row and row.status == "complete" and row.generation == dependency["generation"])

            def feature_context_identity(self, config, training):
                configs, pools = shared.scope(identifier, config, training)
                return feature_context_identity(shared.model.model_identity, configs, pools)

            async def classify(self, config, target, training, *, now=None, event_sink=None):
                return await self.classify_with_cache_options(
                    config, target, training, now=now, event_sink=event_sink, cache_options=CacheOptions()
                )

            async def classify_with_cache_options(self, config, target, training, *, now=None, event_sink=None, cache_options):
                child = key({"classifier": identifier, "request": config.request(target, training, now=now)})
                expected = self.cache_identity(config, target, training, now=now)
                prepared = shared.prepared.get(child)
                configs, pools = (
                    shared.prepared_scope
                    if prepared and not shared.context_configs
                    else shared.scope(identifier, config, training)
                )
                if getattr(shared.model, "capabilities", None) is not None:
                    request, _ = batch_request(configs, target, pools, now=now, share_questions=shared.share_supporting_questions)
                    shared.validate_capabilities(request)
                if (
                    cache_options.policy == "refresh"
                    or not prepared
                    or expected != shared.model.model_identity + ":shared-answer-v3:" + prepared[2] + ":" + str(prepared[3])
                ):
                    await shared.prepare(configs, target, pools, now=now, options=cache_options)
                payload, _, fingerprint, generation = shared.prepared[child]
                for exchange in payload["exchanges"]:
                    if event_sink:
                        event_sink(
                            {
                                **exchange,
                                "shared_request_fingerprint": fingerprint,
                                "shared_answer_generation": generation,
                                "usage": None,
                                "shared_exchange": True,
                            }
                        )
                raw = payload["result"]
                return ClassifiedAnswers(
                    {name: DecisionResult(**answer) for name, answer in raw["answers"][identifier].items()},
                    raw["model"],
                    None,
                    raw["latency_ms"],
                )

        return Adapter()
